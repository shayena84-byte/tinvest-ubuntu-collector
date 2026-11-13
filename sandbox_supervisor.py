#!/usr/bin/env python3
"""Cloud-independent Sandbox order reconciliation and portfolio risk observation.

Default mode is observe: broker reads only. Optional protect mode can cancel
stale Sandbox orders and submit risk-reducing SELLs through the common guard.
Never reads an OpenAI key or requires an available model/billing account.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sandbox_autotrader as t
import trade_ledger as ledger
import risk_policy
from runtime_lock import single_run

CONFIG_PATH = Path.home() / ".config/tinvest-readonly/supervisor.json"
DEFAULT = {"mode": "observe", "stop_loss_fraction": 0.025, "trailing_stop_fraction": 0.025}


def config():
    cfg = {**DEFAULT, **t.load_json(CONFIG_PATH, {})}
    if set(cfg) != set(DEFAULT) or cfg["mode"] not in ("observe", "protect"):
        raise ValueError("Invalid supervisor configuration")
    for key in ("stop_loss_fraction", "trailing_stop_fraction"):
        if not 0 < float(cfg[key]) <= 0.10:
            raise ValueError("Invalid supervisor risk threshold")
    return cfg


@single_run
def run():
    cfg = config()
    now = datetime.now(timezone.utc)
    if t.c.TOKEN_PATH.name != "sandbox-token":
        raise t.c.APIError("Sandbox token required")
    # An operator can disable new entries without disabling order/risk observation.
    old = t.load_json(t.CFG_FILE, {})
    ref = old.get("account_ref")
    if not isinstance(ref, str) or not ref.startswith("tb_"):
        raise t.c.APIError("Missing Sandbox account alias")
    api = t.s.SandboxAPI(t.c.token_read())
    account_id = t.get_account(api, ref)
    state = t.load_json(t.STATE_FILE, {})
    state.pop("cycle_id", None)
    pending = t.reconcile_pending(api, account_id, state, now, cancel_stale=cfg["mode"] == "protect")
    portfolio = api.call("portfolio", {"accountId": account_id, "currency": "RUB"})
    positions = api.call("positions", {"accountId": account_id})
    equity, cash = t.d(portfolio.get("totalAmountPortfolio")), t.rub_cash(positions)
    if not equity.is_finite() or equity <= 0 or not cash.is_finite() or cash < 0:
        raise t.c.APIError("Unverified virtual equity/cash")
    state = t.initialize_day(state, now, equity)
    policy = risk_policy.limits(state.get("risk_policy", {}))
    ledger.portfolio(equity, cash, portfolio.get("positions", []), "supervisor", now)
    loss = equity < Decimal(state["start_equity"])*(1-Decimal(str(policy["max_daily_loss_fraction"])))
    flags = ["daily_loss_limit"] if loss else []
    candidates = []
    for symbol in sorted(t.TRADABLE):
        try:
            spec = t.find_share(api, symbol)
            lots = t.held_lots(portfolio, spec["uid"], spec["lot"])
            if lots <= 0:
                continue
            price = t.last_quote(api, spec["uid"], datetime.now(timezone.utc))
            entry = next((t.d(p.get("averagePositionPrice")) for p in portfolio.get("positions", [])
                          if p.get("instrumentUid") == spec["uid"]), Decimal(0))
            peak = max(t.d(state["peaks"].get(spec["uid"], 0)), price)
            state["peaks"][spec["uid"]] = str(peak)
            why = "daily_loss_limit" if loss else (
                "stop_loss" if entry > 0 and price <= entry*(1-Decimal(str(cfg["stop_loss_fraction"]))) else
                "trailing_stop" if price <= peak*(1-Decimal(str(cfg["trailing_stop_fraction"]))) else None)
            if why:
                flags.append(symbol+":"+why)
                candidates.append((spec, lots, price, why))
        except (t.c.APIError, ValueError, KeyError):
            # An unverified instrument never becomes a sell candidate.
            continue
    state["risk_policy"] = policy
    t.atomic_save(t.STATE_FILE, state)
    details = {"mode": cfg["mode"], "pending": pending, "risk_flags": flags,
               "equity_rub": str(equity), "cash_rub": str(cash)}
    ledger.heartbeat("supervisor", "risk_alert" if flags else "ok", details, now)
    if flags:
        ledger.event("risk_alert", details, now=now)
    if cfg["mode"] == "protect" and candidates and not pending:
        spec, lots, price, why = candidates[0]
        try:
            # All normal order-count/cooldown/quality/short-sale checks still apply.
            t.post_order(api, account_id, spec, "SELL", lots, price, "supervisor: "+why, state, now)
        except t.c.APIError as exc:
            ledger.event("protective_exit_blocked", {"symbol": spec["ticker"], "reason": str(exc)[:160]})
    print(json.dumps(details, ensure_ascii=False))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("run", "status"))
    action = p.parse_args().action
    try:
        if action == "status":
            print(json.dumps({"config": config(), "ledger": ledger.summary(limit=10)}, ensure_ascii=False, indent=2))
            return 0
        return run()
    except (t.c.APIError, ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as exc:
        ledger.heartbeat("supervisor", "error", {"error": type(exc).__name__})
        print("Sandbox supervisor:", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
