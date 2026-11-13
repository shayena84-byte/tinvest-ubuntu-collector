#!/usr/bin/env python3
"""Causal, offline execution scenarios from five-minute books and logged decisions.

No broker/model imports, network requests or database writes. These are coarse
scenarios, not tick backtests: queue priority, intrabar moves and hidden depth
are unknown. Bonds require nominal/interest data and are deliberately excluded.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo
import market_quality
import risk_policy

DB = Path.home()/".local/share/tinvest-market/market.sqlite3"
LEDGER = Path.home()/".local/share/tinvest-market/trading.sqlite3"
LIMITATIONS = [
    "Five-minute observations cannot reproduce intrabar execution or queue priority.",
    "Orders use only books observed after the configured latency; no future data in signals.",
    "Displayed depth, configured fees/slippage and partial fills are scenario assumptions.",
    "Bonds and instruments without verified lot size/RUB quote units are excluded.",
    "Recorded mode replays logged approved live decisions; it does not regenerate model answers.",
    "No coupons, dividends, tax, funding flows or guarantees of future performance."]


def load_frames(path, start, end):
    with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        columns = {r[1] for r in db.execute("PRAGMA table_info(instruments)")}
        lot = "i.lot" if "lot" in columns else "NULL AS lot"
        currency = "i.currency" if "currency" in columns else "NULL AS currency"
        rows = [dict(r) for r in db.execute(
            "SELECT s.*,i.ticker,i.board,i.kind,"+currency+","+lot+
            " FROM snapshots s JOIN instruments i ON i.uid=s.uid "
            "WHERE s.collected_utc>=? AND s.collected_utc<=? "
            "ORDER BY s.collected_utc,s.uid LIMIT 250001", (start.isoformat(), end.isoformat()))]
        if len(rows) > 250000:
            raise ValueError("Too many frames; use a shorter window")
        levels = defaultdict(lambda: {"bid": [], "ask": []})
        for r in db.execute("SELECT b.snapshot_id,b.side,b.price,b.lots FROM book_levels b "
                            "JOIN snapshots s ON s.id=b.snapshot_id WHERE s.collected_utc>=? "
                            "AND s.collected_utc<=? ORDER BY b.snapshot_id,b.side,b.level",
                            (start.isoformat(), end.isoformat())):
            levels[r["snapshot_id"]][r["side"]].append((Decimal(r["price"]), int(r["lots"])))
    for row in rows:
        row["time"] = market_quality.utc(row["collected_utc"])
        row["levels"] = levels[row["id"]]
    return rows


def load_decisions(path, start, end):
    if not path or not Path(path).is_file():
        return []
    result = []
    with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        for row in db.execute("SELECT utc,context_json,decision_json FROM cycles WHERE mode='run' "
                              "AND validation='approved' AND decision_json IS NOT NULL "
                              "AND utc>=? AND utc<=? ORDER BY utc", (start.isoformat(), end.isoformat())):
            decision = json.loads(row["decision_json"])
            context = json.loads(row["context_json"])
            inst = next((r for r in context.get("tradable", []) if r["symbol"] == decision["symbol"]), None)
            if inst and decision["action"] in ("BUY", "SELL"):
                result.append({"time": market_quality.utc(row["utc"]), "uid": inst["uid"], "decision": decision})
    return result


def execute_levels(levels, lots, limit_price, side, slippage_bps=0):
    """Consume only executable displayed lots; preserve the decision's price limit."""
    remaining, notional = lots, Decimal(0)
    factor = Decimal(1)+(Decimal(str(slippage_bps))/10000)*(1 if side == "BUY" else -1)
    for price, available in levels:
        price *= factor
        if (side == "BUY" and price > limit_price) or (side == "SELL" and price < limit_price):
            break
        qty = min(remaining, max(0, available))
        notional += price*qty
        remaining -= qty
        if not remaining:
            break
    filled = lots-remaining
    return filled, notional/filled if filled else None


def replay(db_path=DB, ledger_path=LEDGER, hours=168, strategy="recorded", initial_cash=100000,
           fee_bps=5, slippage_bps=0, latency_seconds=1, now=None):
    if strategy not in ("recorded", "ema", "buy-hold") or not 1 <= hours <= 2160:
        raise ValueError("Invalid strategy/window")
    cash = Decimal(str(initial_cash))
    if not cash.is_finite() or cash <= 0 or not 0 <= fee_bps <= 100 or not 0 <= slippage_bps <= 100 or not 0 < latency_seconds <= 1800:
        raise ValueError("Invalid execution scenario")
    initial = cash
    now = now or datetime.now(timezone.utc)
    start = now-timedelta(hours=hours)
    frames = load_frames(db_path, start, now)
    recorded = load_decisions(ledger_path, start, now) if strategy == "recorded" else []
    events = [(r["time"], 0, r) for r in frames]+[(r["time"], 1, r) for r in recorded]
    events.sort(key=lambda r: (r[0], r[1]))
    latest, held, histories = {}, defaultdict(int), defaultdict(list)
    state = {"orders_today": 0}
    fills, curve, rejected, exclusions = [], [], defaultdict(int), set()
    pending = None
    fee_total = Decimal(0)
    eligible = 0

    def equity():
        return cash+sum((Decimal(latest[uid]["best_bid"])*latest[uid]["lot"]*qty
                         for uid, qty in held.items() if qty > 0 and uid in latest), Decimal(0))

    def submit(uid, decision, when):
        nonlocal pending
        row = latest.get(uid)
        if pending:
            rejected["pending_order"] += 1
            return
        if not row or not market_quality.book_check(row, when)[0]:
            rejected["no_fresh_book"] += 1
            return
        eq = equity()
        inst = {"symbol": row["ticker"], "uid": uid, "lot": row["lot"],
                "price_rub": str((Decimal(row["best_bid"])+Decimal(row["best_ask"]))/2),
                "book_ask": row["best_ask"], "owned_lots": held[uid]}
        ctx = {"tradable": [inst], "equity_rub": str(eq), "cash_rub": str(cash),
               "invested_rub_estimate": str(eq-cash)}
        trade, reason = risk_policy.validate(decision, ctx, {}, state, when)
        if not trade:
            rejected[reason] += 1
            return
        limit = Decimal(row["best_ask"] if trade["action"] == "BUY" else row["best_bid"])
        # Explicit slippage allowance remains bounded by the risk policy at submission.
        factor = Decimal(str(slippage_bps))/10000
        limit *= 1+factor if trade["action"] == "BUY" else 1-factor
        if trade["action"] == "BUY" and limit*row["lot"]*trade["lots"]*(1+Decimal(str(fee_bps))/10000) > min(eq*Decimal(".10"),cash-850):
            rejected["scenario_cost_cap"] += 1
            return
        pending = {"uid": uid, "side": trade["action"], "remaining": trade["lots"],
                   "limit": limit, "due": when+timedelta(seconds=latency_seconds),
                   "expires": when+timedelta(minutes=30), "submitted": when}
        state["orders_today"] += 1
        state["last_order_utc"] = when.isoformat()

    for when, kind, row in events:
        day = when.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
        if state.get("day") != day:
            state.update(day=day, start_equity=str(equity()), orders_today=0, last_order_utc=None)
        if pending and when >= pending["expires"]:
            rejected["unfilled_expired"] += pending["remaining"]
            pending = None
        if kind == 1:
            submit(row["uid"], row["decision"], when)
            continue
        if row["kind"] != "share" or str(row.get("currency", "")).lower() != "rub" or not row.get("lot") or row["lot"] < 1:
            exclusions.add(row["ticker"]+"@"+row["board"]+":unsupported_units_or_lot")
            continue
        if not market_quality.book_check(row, when)[0]:
            rejected["bad_snapshot"] += 1
            continue
        eligible += 1
        uid = row["uid"]
        latest[uid] = row
        if pending and pending["uid"] == uid and when >= pending["due"]:
            side = pending["side"]
            levels = row["levels"]["ask" if side == "BUY" else "bid"]
            qty, price = execute_levels(levels, pending["remaining"], pending["limit"], side, slippage_bps)
            if qty:
                notional = price*row["lot"]*qty
                fee = notional*Decimal(str(fee_bps))/10000
                if side == "BUY" and notional+fee > cash or side == "SELL" and qty > held[uid]:
                    rejected["fill_cash_or_position"] += 1
                    pending = None
                else:
                    cash += (-notional if side == "BUY" else notional)-fee
                    held[uid] += qty if side == "BUY" else -qty
                    fee_total += fee
                    pending["remaining"] -= qty
                    fills.append({"utc": when.isoformat(), "signal_utc": pending["submitted"].isoformat(),
                                  "symbol": row["ticker"], "side": side, "lots": qty,
                                  "price_rub": str(price), "fee_rub": str(fee),
                                  "unfilled_lots": pending["remaining"]})
                    if pending["remaining"] == 0:
                        pending = None
        mid = (Decimal(row["best_bid"])+Decimal(row["best_ask"]))/2
        histories[uid].append(float(mid))
        if strategy != "recorded" and not pending:
            action = None
            if strategy == "buy-hold" and held[uid] == 0:
                action = "BUY"
            if strategy == "ema" and len(histories[uid]) >= 30:
                # A documented control strategy, not a substitute for model decisions.
                values = histories[uid][-100:]
                def ema(span):
                    value = sum(values[:span])/span
                    for item in values[span:]:
                        value += 2/(span+1)*(item-value)
                    return value
                if held[uid] == 0 and ema(9) > ema(21): action = "BUY"
                if held[uid] > 0 and ema(9) < ema(21): action = "SELL"
            if action:
                lots = held[uid] if action == "SELL" else int((equity()*Decimal(".10"))/(Decimal(row["best_ask"])*row["lot"]*Decimal("1.002")))
                submit(uid, {"action": action, "symbol": row["ticker"], "lots": lots, "confidence": 1.0}, when)
        curve.append({"utc": when.isoformat(), "equity_rub": str(equity()), "cash_rub": str(cash)})
    high, worst = initial, Decimal(0)
    for point in curve:
        value = Decimal(point["equity_rub"])
        high = max(high, value)
        worst = min(worst, (value/high-1)*100)
    final = equity()
    return {"strategy": strategy, "from_utc": start.isoformat(), "to_utc": now.isoformat(),
            "status": "no_eligible_data" if not eligible else "no_recorded_decisions" if strategy == "recorded" and not recorded else "scenario_complete",
            "initial_cash_rub": str(initial), "final_equity_rub": str(final),
            "return_pct": str((final/initial-1)*100), "max_drawdown_pct": str(worst),
            "fees_rub": str(fee_total), "eligible_snapshots": eligible, "recorded_decisions": len(recorded),
            "fills": fills, "equity_curve": curve, "rejections": dict(rejected), "excluded": sorted(exclusions),
            "pending_lots_at_end": pending["remaining"] if pending else 0,
            "parameters": {"fee_bps": fee_bps, "slippage_bps": slippage_bps, "latency_seconds": latency_seconds},
            "limitations": LIMITATIONS, "network_requests": 0, "orders_submitted": 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--ledger", type=Path, default=LEDGER)
    p.add_argument("--hours", type=int, default=168)
    p.add_argument("--strategy", choices=("recorded", "ema", "buy-hold", "compare"), default="compare")
    p.add_argument("--cash", type=Decimal, default=Decimal(100000))
    p.add_argument("--fee-bps", type=float, default=5)
    p.add_argument("--slippage-bps", type=float, default=0)
    p.add_argument("--latency-seconds", type=int, default=1)
    args = p.parse_args()
    strategies = ("recorded", "ema", "buy-hold") if args.strategy == "compare" else (args.strategy,)
    result = [replay(args.db, args.ledger, args.hours, name, args.cash, args.fee_bps,
                     args.slippage_bps, args.latency_seconds) for name in strategies]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
