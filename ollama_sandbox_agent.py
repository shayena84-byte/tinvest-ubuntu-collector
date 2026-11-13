#!/usr/bin/env python3
"""Local Ollama decision agent, strictly for T-Invest SANDBOX virtual orders.

No OpenAI service, no production trading endpoint, no user account details sent
to the model. The model sees public market/technical metrics and virtual money
totals only. Model recommendations are untrusted; a separate independent
risk+quality gate decides whether to submit one SandboxService/PostSandboxOrder.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import URLError, HTTPError

import ai_sandbox_agent as ai
import sandbox_autotrader as t
import market_quality
import risk_policy
from runtime_lock import single_run

CONFIG = Path.home()/".config"/"tinvest-readonly"/"ollama-sandbox.json"
MARKET_DB = Path.home()/".local"/"share"/"tinvest-market"/"market.sqlite3"
EQUITY_LOG = Path.home()/".local"/"state"/"tinvest-sandbox-trader"/"virtual-equity.jsonl"
OLLAMA = "http://127.0.0.1:11434"
DEFAULT={
  "enabled":True,
  "model":"qwen3:4b",
  "minimum_confidence":0.70,
  "max_trade_fraction":0.10,
  "max_exposure_fraction":0.50,
  "minimum_cash_rub":850,
  "max_orders_per_day":3,
  "cooldown_minutes":20,
  "max_daily_loss_fraction":0.03,
  "watchlist":["SBER","MOEX","YDEX","ROSN","LKOH","NVTK"],
  "max_quote_age_seconds":900,
  "max_snapshot_age_seconds":600,
  "max_orderbook_spread_pct":0.8
}

def cfg():
    out={**DEFAULT,**t.load_json(CONFIG,{})}
    if set(out)!=set(DEFAULT):
        raise t.c.APIError("Неполная или неизвестная конфигурация local AI")
    if out["enabled"] is not True:
        raise t.c.APIError("Локальный агент выключен")
    if not isinstance(out["model"],str) or not out["model"] or len(out["model"])>100:
        raise t.c.APIError("Некорректная модель")
    if not isinstance(out["watchlist"],list) or len(out["watchlist"])>8 or not out["watchlist"]:
        raise t.c.APIError("Не задан список разрешённых бумаг")
    if not set(out["watchlist"]).issubset(t.TRADABLE):
        raise t.c.APIError("Запрещённые тикеры")
    if not 0.6<=float(out["minimum_confidence"])<=0.99:
        raise t.c.APIError("Ошибка параметра minimum_confidence")
    if not 0<float(out["max_trade_fraction"])<=0.50:
        raise t.c.APIError("Лимит позиции превышает безопасный")
    if not 0<float(out["max_exposure_fraction"])<=0.90:
        raise t.c.APIError("Превышена экспозиция")
    if not 1<=int(out["max_orders_per_day"])<=12:
        raise t.c.APIError("Превышен лимит заявок")
    return risk_policy.limits(out)

def ollama_request(path,body=None,timeout=120):
    """Always direct local loopback, explicitly disable environment proxy values."""
    if path not in ("/api/tags","/api/chat"):
        raise t.c.APIError("Ограниченный список Ollama API")
    url=OLLAMA+path
    headers={"Content-Type":"application/json","Accept":"application/json"}
    if body is None:
        req=Request(url,headers=headers,method="GET")
    else:
        req=Request(url,data=json.dumps(body,ensure_ascii=False).encode("utf-8"),
                    headers=headers,method="POST")
    try:
        with build_opener(ProxyHandler({})).open(req,timeout=timeout) as rsp:
            return json.load(rsp)
    except HTTPError as exc:
        raise t.c.APIError("Локальная модель: HTTP "+str(exc.code)) from None
    except (URLError,TimeoutError,json.JSONDecodeError) as exc:
        raise t.c.APIError("Нет корректного ответа локальной модели: "+type(exc).__name__) from None

def doctor():
    """Model installed and responding. This does not access brokerage."""
    model=cfg()["model"]
    data=ollama_request("/api/tags",timeout=15)
    names={x.get("name","") for x in data.get("models",[])}
    if model not in names:
        raise t.c.APIError("Модель "+model+" не загружена в Ollama: выполните ollama pull "+model)
    probe={
      "model":model,"stream":False,"think":False,
      "messages":[{"role":"system","content":"Верните ровно JSON-объект по формату."},
                  {"role":"user","content":"Ответьте HOLD, NONE, 0 лотов, confidence 0.9 и объясните, что тест завершён."}],
      "format":ai.RESPONSE_SCHEMA,
      "options":{"temperature":0.1,"num_ctx":4096}
    }
    result=ollama_request("/api/chat",probe,timeout=160)
    parsed=parse_ollama(result)
    if parsed["action"]!="HOLD":
        raise t.c.APIError("Тест модели не вернул HOLD")
    print("Ollama модель доступна:",model)
    print("Структурированный ответ модели проверен; тестовые заявки не отправлялись.")
    return 0

def parse_ollama(response):
    if not isinstance(response,dict):
        raise t.c.APIError("Пустой ответ модели")
    content=response.get("message",{}).get("content")
    if not isinstance(content,str) or len(content)>10000:
        raise t.c.APIError("Нет JSON решения в ответе Ollama")
    fake={"status":"completed","output":[{"type":"message",
          "content":[{"type":"output_text","text":content}]}]}
    return ai.decode_response(fake)

def local_decide(context,model):
    from decision_providers import OllamaProvider
    return OllamaProvider().decide(model, context)


def gate_by_quality(context,config,now,db_path=MARKET_DB):
    """Allow only actively monitored names with latest good source/book.
    Both crossed/stale books and missing timestamps fail closed.
    """
    if not db_path.is_file():
        raise t.c.APIError("Нет БД рыночных данных. Market Recorder должен работать.")
    uri="file:"+str(db_path.resolve())+"?mode=ro"
    with sqlite3.connect(uri,uri=True,timeout=8) as db:
        permitted=[]
        for row in context["tradable"]:
            uid=row["uid"]
            snap=db.execute("""SELECT collected_utc,quality_status,spread_pct,
                             last_price,book_time_utc,best_bid,best_ask
                             FROM snapshots WHERE uid=?
                             ORDER BY collected_utc DESC LIMIT 1""",(uid,)).fetchone()
            if not snap:
                ai.log("quality_rejected",symbol=row["symbol"],reason="no_snapshot");continue
            collected,status,spread,price,book_time,bid,ask=snap
            valid, reason = market_quality.book_check({"collected_utc":collected,"book_time_utc":book_time,
                 "quality_status":status,"best_bid":bid,"best_ask":ask}, now,
                 config["max_snapshot_age_seconds"], config["max_orderbook_spread_pct"])
            if not valid:
                ai.log("quality_rejected",symbol=row["symbol"],reason=reason);continue
            fresh, freshness_reason = market_quality.freshness(
                collected, book_time, now, config["max_snapshot_age_seconds"],
                config["max_snapshot_age_seconds"])
            if not fresh or status!="valid":
                ai.log("quality_rejected",symbol=row["symbol"],reason=freshness_reason if not fresh else "invalid_book");continue
            if spread is None or spread>config["max_orderbook_spread_pct"]:
                ai.log("quality_rejected",symbol=row["symbol"],reason="spread_too_wide");continue
            try:
                quote=Decimal(str(row["price_rub"]))
                book_mid=(Decimal(bid)+Decimal(ask))/2
                # A suspiciously dislocated last trade means no buy or sell.
                if quote<=0 or abs(quote-book_mid)/book_mid>Decimal("0.02"):
                    ai.log("quality_rejected",symbol=row["symbol"],reason="price_mismatch");continue
            except (ValueError,ArithmeticError,TypeError):
                ai.log("quality_rejected",symbol=row["symbol"],reason="invalid_quote");continue
            row["book_bid"]=str(bid)
            row["book_ask"]=str(ask)
            row["book_spread_pct"]=spread
            row["book_collected_utc"]=collected
            permitted.append(row)
    context["tradable"]=permitted
    if not permitted:
        raise t.c.APIError("Нет бумаг с проверенными актуальными котировками: HOLD")
    return context

def equity_event(equity,cash,now):
    # Virtual financial totals only. Never save identifiers or tokens.
    EQUITY_LOG.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(EQUITY_LOG,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,"a",encoding="utf-8") as f:
        f.write(json.dumps({"utc":now.isoformat(),
          "virtual_equity_rub":str(equity),"virtual_cash_rub":str(cash)})+"\n")

def history():
    if not EQUITY_LOG.exists():
        return {"records":0,"latest":None,"first":None,"max_drawdown_pct":None}
    try:
        lines=EQUITY_LOG.read_text("utf-8").splitlines()
        records=[json.loads(x) for x in lines[-15000:] if x.strip()]
        totals=[Decimal(row["virtual_equity_rub"]) for row in records]
    except (OSError,ValueError,KeyError):
        return {"records":0,"latest":None,"first":None,"max_drawdown_pct":None}
    if not totals:return {"records":0,"latest":None,"first":None,"max_drawdown_pct":None}
    high=totals[0]
    worst=Decimal(0)
    for value in totals:
        high=max(high,value)
        if high>0:worst=min(worst,(value/high-1)*100)
    return {"records":len(totals),"first":str(totals[0]),
            "latest":str(totals[-1]),
            "return_pct":str((totals[-1]/totals[0]-1)*100) if totals[0]>0 else None,
            "max_drawdown_pct":str(worst)}

def run(dry_run=False):
    import openai_quality_trader
    return openai_quality_trader.one_cycle(dry_run=dry_run, provider="ollama")


def main():
    p=argparse.ArgumentParser(description="Ollama local AI for Sandbox ONLY")
    p.add_argument("action",choices=["doctor","dry-run","run","status"])
    args=p.parse_args()
    try:
        if args.action=="doctor":return doctor()
        if args.action=="status":
            print(json.dumps({"config":{k:v for k,v in cfg().items() if k!="enabled"},
                              "equity":history(),
                              "last_decisions":ai.load_decisions()},
                             ensure_ascii=False,indent=2));return 0
        return run(dry_run=args.action=="dry-run")
    except (t.c.APIError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        ai.log("local_error",reason=type(exc).__name__+": "+str(exc)[:180])
        return 1

if __name__=="__main__":
    sys.exit(main())
