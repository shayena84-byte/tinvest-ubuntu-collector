#!/usr/bin/env python3
"""Autonomous 5-minute Sandbox equity trader; never connects to a real brokerage account.

Reads the user's existing isolated Sandbox token from sandbox.py.
No method in this module can place a production order.
Only explicit virtual PostSandboxOrder and CancelSandboxOrder calls are allowed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, time, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import uuid
from zoneinfo import ZoneInfo

import sandbox as s
import risk_policy
import trade_ledger as ledger
from runtime_lock import single_run

c = s.c
TZ = ZoneInfo("Europe/Moscow")
CFG_FILE = Path.home() / ".config/tinvest-readonly/autotrader.json"
STATE_DIR = Path.home() / ".local/state/tinvest-sandbox-trader"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "decisions.jsonl"
TRADABLE = frozenset(["SBER", "MOEX", "YDEX", "ROSN", "LKOH", "NVTK", "T", "VTBR"])
# It is intentional that only SandboxService can place orders.
c.METHODS.update({
    "trader_candles": "MarketDataService/GetCandles",
    "trader_status": "MarketDataService/GetTradingStatus",
    "trader_orders": "SandboxService/GetSandboxOrders",
    "trader_orderstate": "SandboxService/GetSandboxOrderState",
    "trader_post": "SandboxService/PostSandboxOrder",
    "trader_cancel": "SandboxService/CancelSandboxOrder",
})

DEFAULT = {
    "account_ref": "",
    "enabled": True,
    "symbols": ["SBER", "MOEX", "YDEX"],
    "max_position_fraction": 0.10,
    "max_gross_fraction": 0.50,
    "min_cash_rub": 850,
    "max_orders_per_day": 3,
    "daily_loss_limit": 0.03,
    "stop_loss": 0.025,
    "take_profit": 0.045,
    "trailing_stop": 0.025,
    "cooldown_minutes": 20,
}

def d(x):
    if isinstance(x, dict):
        x = c.val_quote(x)
    if x in (None, ""):
        return Decimal("0")
    return Decimal(str(x))

def load_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback

def atomic_save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def log_event(kind, **kwargs):
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.chmod(0o700)
    item = {"utc": datetime.now(timezone.utc).isoformat(), "event": kind, **kwargs}
    fd = os.open(LOG_FILE, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    ledger.event(kind, kwargs)
    print(json.dumps(item, ensure_ascii=False))

def config():
    o = load_json(CFG_FILE, None)
    if o is None:
        raise c.APIError("Не настроен Sandbox-трейдер. Выполните install_autotrader.sh.")
    if set(o) - set(DEFAULT):
        raise c.APIError("Неизвестные ключи конфигурации")
    cfg = {**DEFAULT, **o}
    if cfg["enabled"] is not True:
        raise c.APIError("Торговля приостановлена (enabled=false)")
    if not str(cfg["account_ref"]).startswith("tb_"):
        raise c.APIError("Счёт должен быть задан псевдонимом tb_... из Sandbox accounts")
    if not cfg["symbols"] or not set(cfg["symbols"]).issubset(TRADABLE):
        raise c.APIError("Торговая вселенная содержит запрещённые инструменты")
    for key in ("max_position_fraction","max_gross_fraction","daily_loss_limit","stop_loss","take_profit","trailing_stop"):
        if not 0 < float(cfg[key]) < 1:
            raise c.APIError("Неверный риск-параметр: " + key)
    if not 0 < float(cfg["max_position_fraction"]) <= float(cfg["max_gross_fraction"]) <= 0.90:
        raise c.APIError("Доля капитала в инструментах должна быть <=90%")
    if not 1 <= int(cfg["max_orders_per_day"]) <= 24:
        raise c.APIError("Неверный лимит числа заявок")
    return risk_policy.classic_limits(cfg)

def market_hours(now):
    local = now.astimezone(TZ)
    return local.weekday() < 5 and time(10, 10) <= local.time() <= time(18, 35)

def get_account(api, ref):
    salt = c.salt_read()
    found = [a for a in api.call("accounts", {"status": "ACCOUNT_STATUS_OPEN"}).get("accounts", [])
             if c.sha_ref(str(a["id"]), salt) == ref]
    if len(found) != 1:
        raise c.APIError("Псевдоним счёта не найден среди виртуальных счетов Sandbox")
    return str(found[0]["id"])

def find_share(api, ticker):
    """Resolve a specific Moscow Exchange common share by ticker + board.

    Never pick an arbitrary UID from FindInstrument's repeated ticker results.
    """
    ticker = str(ticker).strip().upper()
    if ticker not in TRADABLE:
        raise c.APIError(f"{ticker}: инструмент не входит в разрешённую торговую вселенную")
    detailed = api.call("instrument", {
        "idType": "INSTRUMENT_ID_TYPE_TICKER",
        "classCode": "TQBR",
        "id": ticker,
    }).get("instrument") or {}
    if (str(detailed.get("ticker", "")).upper() != ticker
            or str(detailed.get("classCode", "")).upper() != "TQBR"):
        raise c.APIError(f"{ticker}: API не подтвердил тикер и торговую доску TQBR")
    uid = detailed.get("uid")
    if not uid:
        raise c.APIError(f"{ticker}: не получен UID")
    if int(detailed.get("lot") or 0) < 1 or not detailed.get("apiTradeAvailableFlag", False):
        raise c.APIError(f"{ticker}: нет разрешения торговли через API")
    kind = str(detailed.get("instrumentType", "")).lower()
    if kind not in ("share", "shares", "instrument_type_share"):
        raise c.APIError(f"{ticker}: инструмент не подтверждён как акция")
    if str(detailed.get("currency", "")).lower() != "rub":
        raise c.APIError(f"{ticker}: неподходящая валюта")
    return {"ticker": ticker, "uid": uid, "lot": int(detailed["lot"]),
            "name": detailed.get("name", ticker)}


def exchange_open(api, uid):
    r=api.call("trader_status", {"instrumentId": uid})
    return (r.get("tradingStatus") == "SECURITY_TRADING_STATUS_NORMAL_TRADING"
            and bool(r.get("marketOrderAvailableFlag", False)))

def last_quote(api, uid, now):
    r=api.call("last", {"instrumentId":[uid], "lastPriceType":"LAST_PRICE_EXCHANGE"})
    rows=[p for p in r.get("lastPrices", []) if p.get("instrumentUid") == uid or p.get("figi") == uid]
    if not rows:
        # Some API responses lack instrumentUid for single instrument query.
        rows=r.get("lastPrices", []) if len(r.get("lastPrices", [])) == 1 else []
    if not rows:
        raise c.APIError("Не получена последняя биржевая цена")
    p=rows[0]
    price=d(p.get("price"))
    if price <= 0:
        raise c.APIError("Нулевая или отрицательная котировка")
    t=p.get("time")
    if not t:
        raise c.APIError("Биржевая котировка без времени")
    ts=datetime.fromisoformat(t.replace("Z","+00:00"))
    if ts.tzinfo is None or (now-ts).total_seconds() > 900 or (now-ts).total_seconds() < -120:
        raise c.APIError("Биржевая цена устарела или время ошибочно")
    return price

def candles(api, uid, now):
    # Request a conservative ~36h window, not the 7-day upper bound.
    # Avoid optional candleSourceType while checking Sandbox compatibility.
    # The 5-minute API interval supports at most one week and up to 2400 candles.
    frm=now-timedelta(hours=36)
    r=api.call("trader_candles", {"instrumentId": uid, "from": frm.isoformat(),
                  "to": now.isoformat(), "interval": "CANDLE_INTERVAL_5_MIN",
                  "limit": 400})
    rows=[]
    for x in r.get("candles",[]):
        if x.get("isComplete", False) and d(x.get("close")) > 0:
            rows.append((x.get("time",""), d(x["close"]), int(x.get("volume",0))))
    rows.sort(key=lambda x:x[0])
    if len(rows) < 35 or sum(x[2] for x in rows[-10:]) < 10:
        raise c.APIError("Недостаточная история или низкая торговая активность")
    last_ts=datetime.fromisoformat(rows[-1][0].replace("Z","+00:00"))
    if (now-last_ts).total_seconds() > 1800:
        raise c.APIError("Последняя свеча слишком старая")
    return [float(x[1]) for x in rows]

def ema(seq, span):
    alpha=2/(span+1)
    z=sum(seq[:span])/span
    for x in seq[span:]:
        z=alpha*x+(1-alpha)*z
    return z

def rsi(seq, n=14):
    dif=[seq[i]-seq[i-1] for i in range(len(seq)-n,len(seq))]
    up=sum(max(x,0) for x in dif)/n
    dn=sum(max(-x,0) for x in dif)/n
    return 100 if dn == 0 else 100-100/(1+up/dn)

def indicators(closes):
    return {
        "ema9": ema(closes,9), "ema21": ema(closes,21),
        "rsi14": rsi(closes), "momentum_30m": closes[-1]/closes[-7]-1
    }

def buy_signal(t):
    return t["ema9"]>t["ema21"] and 52 <= t["rsi14"] <= 76 and t["momentum_30m"] > 0.002

def sell_signal(t, last, entry, peak, cfg):
    if entry and last <= entry*(1-Decimal(str(cfg["stop_loss"]))):
        return "stop_loss"
    if entry and last >= entry*(1+Decimal(str(cfg["take_profit"]))):
        return "take_profit"
    if peak and last <= peak*(1-Decimal(str(cfg["trailing_stop"]))):
        return "trailing_stop"
    if t["ema9"] < t["ema21"] and t["rsi14"] < 48:
        return "trend_exit"
    return None

def rub_cash(positions):
    return sum((d(o) for o in positions.get("money",[]) if o.get("currency","").lower()=="rub"),Decimal(0))

def held_lots(portfolio, uid, lot):
    for p in portfolio.get("positions", []):
        if p.get("instrumentUid")==uid:
            lots=p.get("quantityLots")
            if lots is not None:
                return int(d(lots))
            return int(d(p.get("quantity")) // lot)
    return 0

def initialize_day(st, now, equity):
    day=now.astimezone(TZ).date().isoformat()
    if st.get("day")!=day:
        st.update({"day":day,"start_equity":str(equity),"orders_today":0})
    st.setdefault("last_order_utc",None)
    st.setdefault("entries",{})
    st.setdefault("peaks",{})
    return st

def order_recent(st, now, minutes):
    if not st.get("last_order_utc"):
        return False
    try:
        last=datetime.fromisoformat(st["last_order_utc"])
        return (now-last).total_seconds() < minutes*60
    except (ValueError,TypeError):
        return True

def reconcile_pending(api, account_id, st, now, cancel_stale=True):
    pending = st.get("pending")
    if not pending:
        return False
    client_id = pending.get("client_id") or pending["id"]
    broker_id = pending.get("broker_id")
    kind = "ORDER_ID_TYPE_EXCHANGE" if broker_id else "ORDER_ID_TYPE_REQUEST"
    ident = broker_id or client_id
    try:
        r = api.call("trader_orderstate", {"accountId": account_id,
                     "orderId": ident, "orderIdType": kind})
    except c.APIError:
        # v1.10 and earlier overwrote the client UUID; try the legacy exchange ID too.
        if "client_id" in pending:
            log_event("pending_unverified", ticker=pending.get("symbol"))
            ledger.event("pending_unverified", {"symbol": pending.get("symbol")})
            return True
        try:
            r = api.call("trader_orderstate", {"accountId": account_id,
                         "orderId": pending["id"], "orderIdType": "ORDER_ID_TYPE_EXCHANGE"})
        except c.APIError:
            log_event("pending_unverified", ticker=pending.get("symbol"))
            return True
    status = r.get("executionReportStatus", "")
    ledger.order_state(client_id, r, now)
    if r.get("orderId"):
        pending["broker_id"] = r["orderId"]
    if status in ("EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_CANCELLED",
                  "EXECUTION_REPORT_STATUS_REJECTED"):
        log_event("order_final", ticker=pending.get("symbol"), direction=pending.get("side"), status=status)
        st.pop("pending", None)
        atomic_save(STATE_FILE, st)
        return False
    if status in ("EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_PARTIALLYFILL"):
        if cancel_stale and (now-datetime.fromisoformat(pending["at"])).total_seconds() > 1800:
            try:
                api.call("trader_cancel", {"accountId": account_id,
                         "orderId": pending.get("broker_id") or client_id,
                         "orderIdType": "ORDER_ID_TYPE_EXCHANGE" if pending.get("broker_id") else "ORDER_ID_TYPE_REQUEST"}, tries=1)
                log_event("stale_order_cancel_requested", ticker=pending["symbol"])
                ledger.event("stale_order_cancel_requested", {"symbol": pending["symbol"]})
            except c.APIError:
                log_event("stale_order_cancel_failed", ticker=pending["symbol"])
        atomic_save(STATE_FILE, st)
        return True
    log_event("unknown_order_state", ticker=pending.get("symbol"), state=status)
    return True


def post_order(api, account_id, spec, side, lots, price, reason, st, now):
    # Final enforcement applies to all providers and historical entry points.
    if not isinstance(api, s.SandboxAPI) or api.base != s.SANDBOX_BASE:
        raise c.APIError("Торговля разрешена только в официальной песочнице")
    if isinstance(lots, bool) or not isinstance(lots, int) or lots < 1 or side not in ("BUY", "SELL"):
        raise c.APIError("Некорректное направление или количество лотов")
    if st.get("pending"):
        raise c.APIError("Previous order is not reconciled")
    verified = find_share(api, spec["ticker"])
    if spec["uid"] != verified["uid"] or spec["lot"] != verified["lot"]:
        raise c.APIError("Unverified instrument identity or lot size")
    from trading_session import instrument_open
    from market_recorder import normalize_payload
    from market_quality import book_check
    live_book = api.call("book", {"instrumentId": spec["uid"], "depth": 10})
    checked_at = datetime.now(timezone.utc)
    book = normalize_payload(verified, {}, live_book, 10, checked_at)
    book["collected_utc"] = checked_at.isoformat()
    allowed, why = book_check(book, checked_at)
    if not allowed:
        raise c.APIError("Final orderbook rejected: " + why)
    raw_port = api.call("portfolio", {"accountId": account_id, "currency": "RUB"})
    raw_pos = api.call("positions", {"accountId": account_id})
    equity, cash = d(raw_port.get("totalAmountPortfolio")), rub_cash(raw_pos)
    if not equity.is_finite() or not cash.is_finite() or equity <= 0 or cash < 0:
        raise c.APIError("Unverified portfolio")
    initialize_day(st, checked_at, equity)
    inst = {"symbol": spec["ticker"], "uid": spec["uid"], "lot": spec["lot"],
            "price_rub": str(price), "book_ask": book["best_ask"],
            "owned_lots": held_lots(raw_port, spec["uid"], spec["lot"])}
    context = {"tradable": [inst], "equity_rub": str(equity), "cash_rub": str(cash),
               "invested_rub_estimate": str(max(Decimal(0), equity-cash))}
    approved, why = risk_policy.validate({"action": side, "symbol": spec["ticker"],
                      "lots": lots, "confidence": 1.0}, context, st.get("risk_policy", {}), st, checked_at)
    if not approved:
        raise c.APIError("Final risk check: " + why)
    if api.call("trader_orders", {"accountId": account_id}).get("orders", []):
        raise c.APIError("Active Sandbox orders block a new submission")
    if not instrument_open(api, spec["uid"])[0]:
        raise c.APIError("Trading status changed before submission")
    allowed, why = book_check(book, datetime.now(timezone.utc))
    if not allowed:
        raise c.APIError("Orderbook expired before submission: " + why)
    # Bound the execution price by a displayed tick-valid quote. No market price drift.
    limit = Decimal(book["best_ask"] if side == "BUY" else book["best_bid"])
    oid = str(uuid.uuid4())
    cycle_id = st.get("cycle_id")
    if not cycle_id:
        context["technical"] = spec.get("indicators", {})
        context["book_time_utc"] = book["book_time_utc"]
        cycle_id = ledger.start_cycle(context,"run","supervisor" if reason.startswith("supervisor:") else "mechanical",None,now=checked_at)
        ledger.decision(cycle_id,{"action":side,"symbol":spec["ticker"],"lots":lots,
                                "confidence":1.0,"reason":reason},None)
        st["cycle_id"] = cycle_id
    ledger.order_intent(oid, spec, side, lots, limit, cycle_id, checked_at)
    st["pending"] = {"id": oid, "client_id": oid, "broker_id": None,
                     "symbol": spec["ticker"], "uid": spec["uid"], "side": side, "lots": lots,
                     "reference_price": str(limit), "at": checked_at.isoformat()}
    st["last_order_utc"] = checked_at.isoformat()
    st["orders_today"] += 1
    if side == "BUY":
        st["entries"][spec["uid"]] = str(limit)
        st["peaks"][spec["uid"]] = str(limit)
    atomic_save(STATE_FILE, st)
    units = int(limit)
    nanos = int((limit-Decimal(units))*Decimal(1000000000))
    body = {"accountId": account_id, "instrumentId": spec["uid"], "quantity": str(lots),
            "direction": "ORDER_DIRECTION_"+side, "orderType": "ORDER_TYPE_LIMIT",
            "price": {"units": str(units), "nano": nanos}, "orderId": oid, "confirmMarginTrade": False}
    try:
        response = api.call("trader_post", body, tries=1)
        st["pending"]["broker_id"] = response.get("orderId")
        atomic_save(STATE_FILE, st)
        ledger.order_state(oid, response)
        log_event("sandbox_order_submitted", ticker=spec["ticker"], side=side, lots=lots,
                  reference_price=str(limit), reason=reason,
                  status=response.get("executionReportStatus", "unknown"))
    except Exception as exc:
        ledger.finish_cycle(cycle_id, "outcome_unknown")
        log_event("sandbox_order_outcome_unknown", ticker=spec["ticker"], side=side,
                  error=type(exc).__name__)
        ledger.event("order_outcome_unknown", {"client_id": oid, "error": type(exc).__name__}, cycle_id)
        raise


@single_run
def tick(dry_run=False):
    now=datetime.now(timezone.utc)
    cfg=config()
    st=load_json(STATE_FILE,{})
    st.pop("cycle_id", None)
    st["risk_policy"] = risk_policy.limits({"max_trade_fraction":cfg["max_position_fraction"],
        "max_exposure_fraction":cfg["max_gross_fraction"],"max_daily_loss_fraction":cfg["daily_loss_limit"],
        "max_orders_per_day":cfg["max_orders_per_day"],"minimum_cash_rub":cfg["min_cash_rub"],
        "cooldown_minutes":cfg["cooldown_minutes"]})
    if not market_hours(now):
        log_event("market_closed")
        return 0
    if c.TOKEN_PATH.name != "sandbox-token":
        raise c.APIError("Торговля остановлена: используется не Sandbox-токен")
    api=s.SandboxAPI(c.token_read())
    account_id=get_account(api,cfg["account_ref"])
    raw_portfolio=api.call("portfolio",{"accountId":account_id,"currency":"RUB"})
    raw_positions=api.call("positions",{"accountId":account_id})
    equity=d(raw_portfolio.get("totalAmountPortfolio"))
    cash=rub_cash(raw_positions)
    if equity<=0:
        raise c.APIError("Не удалось получить положительную оценку виртуального капитала")
    st=initialize_day(st,now,equity)
    atomic_save(STATE_FILE,st)
    if dry_run and st.get("pending"):
        log_event("dry_run_pending_skip")
        return 0
    if not dry_run and reconcile_pending(api,account_id,st,now):
        log_event("waiting_order",equity=str(equity))
        return 0
    open_orders=api.call("trader_orders",{"accountId":account_id}).get("orders",[])
    if open_orders:
        log_event("skip_unfilled_orders",active_count=len(open_orders))
        return 0
    specs=[]
    for name in cfg["symbols"]:
        try:
            sp=find_share(api,name)
            if not exchange_open(api,sp["uid"]):
                continue
            sp["last"]=last_quote(api,sp["uid"],now)
            sp["indicators"]=indicators(candles(api,sp["uid"],now))
            sp["lots"]=held_lots(raw_portfolio,sp["uid"],sp["lot"])
            specs.append(sp)
        except (c.APIError,ValueError,KeyError) as e:
            log_event("instrument_skipped",ticker=name,reason=str(e)[:130])
    if not specs:
        log_event("no_tradeable_instruments",equity=str(equity))
        return 0
    gross=Decimal(0)
    held_specs=[]
    for sp in specs:
        if sp["lots"]>0:
            gross+=sp["last"]*sp["lot"]*sp["lots"]
            held_specs.append(sp)
            uid=sp["uid"]
            if uid not in st["entries"]:
                avg=next((p.get("averagePositionPrice") for p in raw_portfolio.get("positions",[]) if p.get("instrumentUid")==uid),None)
                st["entries"][uid]=str(d(avg) or sp["last"])
            st["peaks"][uid]=str(max(d(st["peaks"].get(uid,0)),sp["last"]))
    atomic_save(STATE_FILE,st)
    if order_recent(st,now,int(cfg["cooldown_minutes"])):
        log_event("cooldown",equity=str(equity),cash=str(cash))
        return 0
    if st["orders_today"]>=int(cfg["max_orders_per_day"]):
        log_event("daily_order_limit",equity=str(equity))
        return 0
    # Sell positions before considering buys, even if the daily loss limit has been reached.
    for sp in held_specs:
        uid=sp["uid"]
        why=sell_signal(sp["indicators"],sp["last"],d(st["entries"][uid]),
                        d(st["peaks"][uid]),cfg)
        if why:
            log_event("signal_sell",ticker=sp["ticker"],reason=why,lots=sp["lots"])
            if not dry_run:
                post_order(api,account_id,sp,"SELL",sp["lots"],sp["last"],why,st,now)
            return 0
    if equity<d(st["start_equity"])*(1-Decimal(str(cfg["daily_loss_limit"]))):
        log_event("daily_loss_pause_buy",equity=str(equity),baseline=st["start_equity"])
        return 0
    # Limit both cash at risk and total invested exposure. No shorts, no leverage.
    max_new=min(
        (equity*Decimal(str(cfg["max_position_fraction"]))),
        (equity*Decimal(str(cfg["max_gross_fraction"])) - gross),
        (cash-Decimal(str(cfg["min_cash_rub"])))
    )
    if max_new<=0:
        log_event("cash_or_exposure_limit",equity=str(equity),cash=str(cash),gross=str(gross))
        return 0
    candidates=[sp for sp in specs if sp["lots"]==0 and buy_signal(sp["indicators"])]
    candidates.sort(key=lambda x:x["indicators"]["momentum_30m"],reverse=True)
    for sp in candidates:
        cost_per_lot=sp["last"]*sp["lot"]*Decimal("1.001")
        qty=int(max_new//cost_per_lot)
        if qty<=0:
            continue
        log_event("signal_buy",ticker=sp["ticker"],lots=qty,price=str(sp["last"]),
                  momentum_30m=round(sp["indicators"]["momentum_30m"],5),
                  rsi=round(sp["indicators"]["rsi14"],1),dry_run=dry_run)
        if not dry_run:
            post_order(api,account_id,sp,"BUY",qty,sp["last"],"ema_rsi_momentum",st,now)
        return 0
    log_event("no_signal",equity=str(equity),cash=str(cash),positions=len(held_specs))
    return 0

def main():
    p=argparse.ArgumentParser(description="Sandbox-only autonomous equity bot")
    p.add_argument("action",choices=("run","dry-run","status"))
    args=p.parse_args()
    try:
        if args.action=="status":
            st=load_json(STATE_FILE,{})
            print(json.dumps({k:v for k,v in st.items() if k in
                  ("day","start_equity","orders_today","last_order_utc","pending","entries")},
                  ensure_ascii=False,indent=2))
            return 0
        return tick(dry_run=args.action=="dry-run")
    except Exception as e:
        log_event("error",reason=f"{type(e).__name__}: {str(e)[:180]}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
