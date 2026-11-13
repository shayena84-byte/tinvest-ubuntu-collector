#!/usr/bin/env python3
"""AI discretionary decision maker for T-Invest SANDBOX only.

Every invocation: read sandbox portfolio and market candles -> ask OpenAI Responses
for a NEW reasoned BUY/SELL/HOLD decision -> independently validate risk limits ->
optionally route virtual order to SandboxService/PostSandboxOrder.

There is no OpenAI tool that can place an order. The model returns JSON data only.
Credentials are NEVER sent to the model or GitHub.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import getpass
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import sandbox_autotrader as t

c = t.c
CFG_DIR = Path.home() / ".config" / "tinvest-readonly"
OPENAI_KEY_FILE = CFG_DIR / "openai-api-key"
AI_CFG_FILE = CFG_DIR / "ai-agent.json"
STATE_DIR = Path.home() / ".local" / "state" / "tinvest-sandbox-trader"
LOG_FILE = STATE_DIR / "ai-decisions.jsonl"

DEFAULT_AI = {
    "enabled": True,
    "model": "gpt-6.1-sol",
    "watchlist": ["SBER", "MOEX", "YDEX", "ROSN", "LKOH", "NVTK", "T", "VTBR"],
    "minimum_confidence": 0.65,
    "max_ai_calls_per_day": 110,
    "max_trade_fraction": 0.42,
    "max_exposure_fraction": 0.82,
    "minimum_cash_rub": 850,
    "max_orders_per_day": 8,
    "cooldown_minutes": 20,
    "max_daily_loss_fraction": 0.08
}

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "symbol": {"type": "string"},
        "lots": {"type": "integer"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "risks": {"type": "string"},
        "horizon_minutes": {"type": "integer"}
    },
    "required": ["action", "symbol", "lots", "confidence", "reason", "risks", "horizon_minutes"]
}
SYSTEM = """Вы — автономный аналитик для ИСКЛЮЧИТЕЛЬНО ВИРТУАЛЬНОЙ биржевой песочницы Т-Банка.
Каждый вызов — новый анализ на основе измеренных данных. Самостоятельно выбирайте актив,
время, направление и число лотов. Вы не обязаны торговать, HOLD — полноценное решение.
Цель: максимизация совокупного виртуального капитала с контролем крупных просадок.
Оценивайте относительную динамику бумаг, RSI, EMA, импульс, риск и капитал по текущим ценам.
Не подменяйте реальный анализ одной механической формулой или заранее заданным сигналом.
Используйте только доказанные входные цены; не выдумывайте новости, стаканы и прогнозы.
Если источников/истории недостаточно, уверенность низкая или условия невыгодны — HOLD.
Никаких операций на реальном счёте. Комиссии, волатильность и ограничения лотов важны.
Выдавайте СТРОГО JSON по схеме. Для HOLD symbol="NONE", lots=0.
Для BUY или SELL указывайте только инструмент из списка tradable в снимке.
В каждом решении укажите короткий конкретный тезис и отдельное объяснение рисков.
"""

def private_json(path, obj):
    t.atomic_save(path, obj)

def setup_key():
    CFG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = getpass.getpass("OpenAI API key (ввод скрыт): ").strip()
    if not key.startswith("sk-") or len(key) < 24 or any(ch.isspace() for ch in key):
        raise c.APIError("Некорректный формат API-ключа")
    c.private_write(OPENAI_KEY_FILE, (key+"\n").encode("utf-8"))
    print("Ключ сохранён локально. Никогда не отправляйте его в GitHub или чат.")

def read_key():
    if not OPENAI_KEY_FILE.is_file():
        raise c.APIError("Не найден API-ключ. Выполните: python3 ai_sandbox_agent.py setup")
    k=OPENAI_KEY_FILE.read_text(encoding="utf-8").strip()
    if not k.startswith("sk-"):
        raise c.APIError("Неверный API-ключ")
    return k

def ai_config():
    cfg={**DEFAULT_AI, **t.load_json(AI_CFG_FILE,{})}
    if set(cfg) - set(DEFAULT_AI):
        raise c.APIError("Некорректный файл ai-agent.json")
    if not cfg["enabled"]:
        raise c.APIError("AI-агент остановлен в конфигурации")
    if not isinstance(cfg["watchlist"],list) or not cfg["watchlist"] or len(cfg["watchlist"])>12:
        raise c.APIError("Некорректный список инструментов")
    if not set(cfg["watchlist"]).issubset(t.TRADABLE):
        raise c.APIError("Торговая вселенная содержит запрещённые инструменты")
    if not (isinstance(cfg["model"],str) and cfg["model"].startswith("gpt-")):
        raise c.APIError("Неподдерживаемая модель")
    for k in ("minimum_confidence","max_trade_fraction","max_exposure_fraction","max_daily_loss_fraction"):
        if not 0<float(cfg[k])<1:
            raise c.APIError("Некорректный параметр "+k)
    if not 0 < int(cfg["max_ai_calls_per_day"]) <= 120:
        raise c.APIError("Неверный лимит API-запросов")
    if not 0 < int(cfg["max_orders_per_day"]) <= 20:
        raise c.APIError("Неверный лимит заявок")
    return cfg

def log(kind, **items):
    STATE_DIR.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(LOG_FILE,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,"a",encoding="utf-8") as f:
        f.write(json.dumps({"utc":datetime.now(timezone.utc).isoformat(),"type":kind,**items},
                          ensure_ascii=False)+"\n")
    print(kind, json.dumps(items,ensure_ascii=False)[:2000])

def load_decisions(n=7):
    try:
        lines=LOG_FILE.read_text("utf-8").splitlines()[-250:]
    except FileNotFoundError:
        return []
    results=[]
    for line in reversed(lines):
        try:
            obj=json.loads(line)
            if obj.get("type") in ("ai_decision","execution"):
                results.append({"utc":obj.get("utc"),"action":obj.get("action"),
                                "symbol":obj.get("symbol"),"lots":obj.get("lots"),
                                "reason":str(obj.get("reason",""))[:230],
                                "result":obj.get("result","")})
        except json.JSONDecodeError:
            continue
        if len(results)>=n: break
    return list(reversed(results))

def decode_response(raw):
    if raw.get("status")!="completed":
        raise c.APIError("Ответ модели не завершён")
    text=[]
    for item in raw.get("output",[]):
        if item.get("type")=="message":
            for content in item.get("content",[]):
                if content.get("type")=="output_text":
                    text.append(content.get("text",""))
    if len(text)!=1:
        raise c.APIError("Модель не вернула единственный структурированный ответ")
    obj=json.loads(text[0])
    if not isinstance(obj,dict) or set(obj)!=set(RESPONSE_SCHEMA["required"]):
        raise c.APIError("Неполный JSON от модели")
    if obj["action"] not in ("BUY","SELL","HOLD"):
        raise c.APIError("Неизвестное действие")
    if not isinstance(obj["lots"],int) or isinstance(obj["lots"],bool) or not 0<=obj["lots"]<=100000:
        raise c.APIError("Необычное количество лотов")
    if not isinstance(obj["confidence"],(float,int)) or not 0<=obj["confidence"]<=1:
        raise c.APIError("Некорректная уверенность")
    if not isinstance(obj["reason"],str) or not isinstance(obj["risks"],str) or not 15<=len(obj["reason"])<=1200:
        raise c.APIError("Нет осмысленного объяснения")
    if not 0<=obj["horizon_minutes"]<=30000:
        raise c.APIError("Некорректный торговый горизонт")
    return obj

def safe_api_error(exc):
    """Extract only the safe error.type / error.code fields from OpenAI error JSON."""
    import re
    status = getattr(exc, "code", "?")
    result = f"HTTP {status}"
    try:
        parsed = json.loads(exc.read(4096).decode("utf-8", "replace"))
        if not isinstance(parsed, dict):
            return result
        err = parsed.get("error") or {}
        if not isinstance(err, dict):
            return result
        fields = []
        for name in ("type", "code"):
            value = err.get(name)
            if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", value):
                fields.append(f"{name}={value}")
        if fields:
            result += " [" + ", ".join(fields) + "]"
    except (ValueError, UnicodeError, TypeError, OSError):
        pass
    return result


def diagnose_api():
    """Read-only GET /v1/models/{model}; does not create a completion or order."""
    from urllib.parse import quote
    model = ai_config()["model"]
    key = read_key()
    url = "https://api.openai.com/v1/models/" + quote(model, safe="")
    req = Request(url, headers={"Authorization": "Bearer " + key}, method="GET")
    try:
        with urlopen(req, timeout=25) as resp:
            body = json.load(resp)
        accessible = body.get("id") == model
        print("OpenAI API: HTTP 200, model accessible =", accessible)
        print("Запрос не создаёт торговых заявок или генераций модели.")
        return 0 if accessible else 1
    except HTTPError as exc:
        print("OpenAI API:", safe_api_error(exc))
        print("HTTP 403 требует проверки кода ошибки, прав API-проекта и региона сервера.")
        return 1
    except (URLError, TimeoutError) as exc:
        print("OpenAI API: network/TLS error =", type(exc).__name__)
        return 1


def ask_openai(key, model, snapshot):
    payload={
        "model":model,
        "instructions":SYSTEM,
        "input":[{"role":"user","content":json.dumps(snapshot,ensure_ascii=False,separators=(",",":"))}],
        "reasoning":{"effort":"medium"},
        "text":{"format":{"type":"json_schema","name":"sandbox_trade_decision","strict":True,
                         "schema":RESPONSE_SCHEMA}},
        "max_output_tokens":2400,
        "store":False
    }
    req=Request("https://api.openai.com/v1/responses",
                data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),
                headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},
                method="POST")
    try:
        with urlopen(req,timeout=85) as r:
            out=json.load(r)
    except HTTPError as e:
        # Only show safe machine-readable API error type/code; never print a token,
        # raw body, request headers, messages containing personal information.
        raise c.APIError("Ошибка OpenAI API: " + safe_api_error(e)) from None
    except (TimeoutError, URLError) as e:
        raise c.APIError("Не удалось получить ответ OpenAI API: "+type(e).__name__) from None
    decision=decode_response(out)
    usage=out.get("usage",{})
    return decision,{"input_tokens":usage.get("input_tokens"),"output_tokens":usage.get("output_tokens")}

def init_ai_state(st,now):
    today=now.astimezone(t.TZ).date().isoformat()
    if st.get("ai_day")!=today:
        st["ai_day"]=today
        st["ai_calls_today"]=0
    return st

def build_context(api,account_id,ai_cfg,now):
    portfolio=api.call("portfolio",{"accountId":account_id,"currency":"RUB"})
    positions=api.call("positions",{"accountId":account_id})
    equity=t.d(portfolio.get("totalAmountPortfolio"))
    cash=t.rub_cash(positions)
    if equity<=0 or cash<0:
        raise c.APIError("Не удалось прочитать достоверную стоимость портфеля и деньги")
    tradable=[]
    positions_by_uid={p.get("instrumentUid"):p for p in portfolio.get("positions",[])}
    for sym in ai_cfg["watchlist"]:
        try:
            sp=t.find_share(api,sym)
            if not t.exchange_open(api,sp["uid"]):
                continue
            sp["last"]=t.last_quote(api,sp["uid"],now)
            closes=t.candles(api,sp["uid"],now)
            info=t.indicators(closes)
            lots=t.held_lots(portfolio,sp["uid"],sp["lot"])
            meta=positions_by_uid.get(sp["uid"],{})
            tradable.append({
                "symbol":sym,"uid":sp["uid"],"lot":sp["lot"],"price_rub":str(sp["last"]),
                "owned_lots":lots,"average_buy_price_rub":str(t.d(meta.get("averagePositionPrice"))),
                "ema9":round(info["ema9"],4),"ema21":round(info["ema21"],4),
                "rsi14":round(info["rsi14"],2),"momentum_30m_pct":round(info["momentum_30m"]*100,3),
                "last_5m_closes":[round(x,3) for x in closes[-24:]],
                "change_2h_pct":round((closes[-1]/closes[-24]-1)*100,3)
            })
        except (c.APIError,ValueError,KeyError,ZeroDivisionError) as e:
            log("market_skipped",symbol=sym,error=str(e)[:130])
    if not tradable:
        raise c.APIError("Нет пригодных для сделок инструментов с актуальными данными")
    invested=max(t.d(0),equity-cash)
    result={
        "timestamp_utc":now.isoformat(),"session":"T-Invest Sandbox (simulation only)",
        "equity_rub":str(equity),"cash_rub":str(cash),
        "invested_rub_estimate":str(invested),
        "tradable":tradable,
        "recent_decisions":load_decisions(),
        "risk_rules":{
            "max_trade_fraction":ai_cfg["max_trade_fraction"],
            "max_exposure_fraction":ai_cfg["max_exposure_fraction"],
            "min_cash_rub":ai_cfg["minimum_cash_rub"],
            "max_orders_per_day":ai_cfg["max_orders_per_day"],
            "note":"All prices and lots are from the T-Invest Sandbox market feed. No guarantee of execution."
        },
        "news":"No verified news feed connected; do not invent fundamental developments."
    }
    return result,portfolio,positions

def validate(decision,context,ai_cfg,st,now):
    symbol=decision["symbol"].upper().strip()
    action=decision["action"]
    if action=="HOLD":
        if symbol!="NONE" or decision["lots"]!=0:
            return None,"Invalid HOLD"
        return None,"HOLD"
    if decision["confidence"] < ai_cfg["minimum_confidence"]:
        return None,"Confidence below threshold"
    candidates={a["symbol"]:a for a in context["tradable"]}
    if symbol not in candidates:
        return None,"Non-tradable symbol"
    instrument=candidates[symbol]
    qty=decision["lots"]
    if qty<1:
        return None,"Zero lots"
    if st.get("orders_today",0)>=ai_cfg["max_orders_per_day"]:
        return None,"Order count limit"
    if t.order_recent(st,now,ai_cfg["cooldown_minutes"]):
        return None,"Cooldown active"
    equity=Decimal(context["equity_rub"])
    cash=Decimal(context["cash_rub"])
    invested=Decimal(context["invested_rub_estimate"])
    price=Decimal(instrument["price_rub"])
    lot_cost=price*instrument["lot"]*Decimal("1.002")
    notional=price*instrument["lot"]*qty
    if action=="BUY":
        if instrument["owned_lots"]>0:
            return None,"Already held; prevent unintended averaging and concentration"
        start=t.d(st.get("start_equity","0"))
        if start and equity<start*(1-Decimal(str(ai_cfg["max_daily_loss_fraction"]))):
            return None,"Daily loss limit"
        max_amount=min(Decimal(str(ai_cfg["max_trade_fraction"]))*equity,
                       Decimal(str(ai_cfg["max_exposure_fraction"]))*equity-invested,
                       cash-Decimal(str(ai_cfg["minimum_cash_rub"])))
        if max_amount<=0 or lot_cost*qty>max_amount:
            return None,"Cash, single-trade, or total exposure cap"
    else:
        if instrument["owned_lots"]<qty:
            return None,"Cannot sell more lots than held"
    return {"action":action,"symbol":symbol,"lots":qty,"instrument":instrument},None

def run(dry_run=False):
    now=datetime.now(timezone.utc)
    cfg=ai_config()
    if not t.market_hours(now):
        log("closed_market")
        return
    old_cfg=t.config()
    if c.TOKEN_PATH.name!="sandbox-token":
        raise c.APIError("Отсутствует изоляция Sandbox-токена")
    if not isinstance(old_cfg["account_ref"],str):
        raise c.APIError("Некорректная учётная запись песочницы")
    key=read_key()
    api=t.s.SandboxAPI(c.token_read())
    account_id=t.get_account(api,old_cfg["account_ref"])
    st=init_ai_state(t.load_json(t.STATE_FILE,{}),now)
    # Existing unverified orders block new trades.
    if t.reconcile_pending(api,account_id,st,now):
        log("pending_order_skip")
        return
    open_orders=api.call("trader_orders",{"accountId":account_id}).get("orders",[])
    if open_orders:
        log("active_order_skip",count=len(open_orders))
        return
    if st["ai_calls_today"]>=cfg["max_ai_calls_per_day"]:
        log("api_daily_budget_reached")
        return
    ctx,_,_=build_context(api,account_id,cfg,now)
    st=t.initialize_day(st,now,Decimal(ctx["equity_rub"]))
    t.atomic_save(t.STATE_FILE,st)
    # Persist count BEFORE API call; failures count, avoiding runaway bill.
    st["ai_calls_today"]+=1
    t.atomic_save(t.STATE_FILE,st)
    decision,usage=ask_openai(key,cfg["model"],ctx)
    log("ai_decision",action=decision["action"],symbol=decision["symbol"],
        lots=decision["lots"],confidence=decision["confidence"],
        reason=decision["reason"],risks=decision["risks"],usage=usage,dry_run=dry_run)
    trade,error=validate(decision,ctx,cfg,st,now)
    if trade is None:
        log("decision_not_executed",result=error)
        return
    if dry_run:
        log("simulation_only",action=trade["action"],symbol=trade["symbol"],lots=trade["lots"])
        return
    # Recheck holdings and cash immediately before submission.
    new_portfolio=api.call("portfolio",{"accountId":account_id,"currency":"RUB"})
    new_positions=api.call("positions",{"accountId":account_id})
    equity_new=t.d(new_portfolio.get("totalAmountPortfolio"))
    cash_new=t.rub_cash(new_positions)
    if equity_new<=0 or abs(equity_new-Decimal(ctx["equity_rub"]))>equity_new*Decimal(".03"):
        log("portfolio_changed_abort")
        return
    inst=trade["instrument"]
    current_lots=t.held_lots(new_portfolio,inst["uid"],inst["lot"])
    if trade["action"]=="SELL" and current_lots<trade["lots"]:
        log("position_changed_abort")
        return
    if trade["action"]=="BUY":
        if current_lots>0 or cash_new<Decimal(ctx["cash_rub"])*Decimal(".95"):
            log("cash_changed_abort")
            return
    spec={"ticker":trade["symbol"],"uid":inst["uid"],"lot":inst["lot"]}
    t.post_order(api,account_id,spec,trade["action"],trade["lots"],
                 Decimal(inst["price_rub"]),"ai: "+decision["reason"][:100],st,now)
    log("execution",action=trade["action"],symbol=trade["symbol"],
        lots=trade["lots"],result="Sandbox order submitted")

def main():
    import argparse
    p=argparse.ArgumentParser(description="GPT-driven decision agent for T-Invest Sandbox only")
    p.add_argument("action",choices=["setup","run","dry-run","status","diagnose"])
    args=p.parse_args()
    try:
        if args.action=="setup":
            setup_key()
        elif args.action=="diagnose":
            return diagnose_api()
        elif args.action=="status":
            ai=ai_config()
            state=t.load_json(t.STATE_FILE,{})
            print(json.dumps({"model":ai["model"],"watchlist":ai["watchlist"],
              "ai_calls_today":state.get("ai_calls_today",0),
              "orders_today":state.get("orders_today",0),
              "last_decisions":load_decisions(6)},ensure_ascii=False,indent=2))
        else:
            run(dry_run=args.action=="dry-run")
        return 0
    except Exception as ex:
        log("error",reason=f"{type(ex).__name__}: {str(ex)[:160]}")
        return 1

if __name__=="__main__":
    sys.exit(main())
