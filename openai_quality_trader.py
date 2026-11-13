#!/usr/bin/env python3
"""OpenAI Responses autonomous decision agent for T-Invest SANDBOX ONLY.

Uses the already stored OpenAI API key, existing model/AI risk configuration
and independent v1.6 SQLite quality filter. Does NOT require Ollama service.
Only sandbox endpoints are allowed for orders. No credentials are sent to the model.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

import ai_sandbox_agent as ai
import ollama_sandbox_agent as shared  # reusable risk/quality/equity routines; NO Ollama calls
import sandbox_autotrader as t
import trading_session as session
import v19_guard as guard
import risk_policy
import trade_ledger as ledger
import model_budget
from runtime_lock import single_run

c = t.c
MAX_TRADE_FRACTION = 0.10
MAX_EXPOSURE_FRACTION = 0.50
MAX_DAILY_LOSS_FRACTION = 0.03
MAX_ORDERS_DAY = 3
MAX_API_CALLS_DAY = 24
REQUIRED_CONFIDENCE = 0.70
MAX_BOOK_AGE = 600
MAX_SPREAD_PCT = 0.8


def config():
    """Use existing ~/.config/tinvest-readonly/ai-agent.json with strict ceilings."""
    cfg = risk_policy.limits(ai.ai_config())
    cfg["max_snapshot_age_seconds"] = MAX_BOOK_AGE
    cfg["max_orderbook_spread_pct"] = MAX_SPREAD_PCT
    return cfg


def doctor():
    """Check provider/model and credentials without generation or brokerage operations."""
    return ai.diagnose_api()


def status():
    state = t.load_json(t.STATE_FILE, {})
    cfg = config()
    info = {
        "provider": "OpenAI Responses API",
        "model": cfg["model"],
        "mode": "T-Invest Sandbox only",
        "risk_ceiling_per_trade": cfg["max_trade_fraction"],
        "openai_quota_block": guard.block_status(),
        "model_budget": model_budget.status(),
        "daily_orders": state.get("orders_today", 0),
        "virtual_equity": shared.history(),
        "recent_decisions": ai.load_decisions(10),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def _quality(cfg, context, now):
    return shared.gate_by_quality(context, cfg, now)


def session_status():
    """Read current per-instrument trading availability without calling OpenAI.

    Uses Sandbox MarketDataService/GetTradingStatus through exact TQBR UIDs.
    Safe to run at any time, including evening sessions and holidays.
    """
    if c.TOKEN_PATH.name != "sandbox-token":
        raise c.APIError("Production token rejected: Sandbox only")
    cfg = config()
    api = t.s.SandboxAPI(c.token_read())
    active, details = session.open_watchlist(api, cfg["watchlist"])
    for item in details:
        print(("%s OPEN" if item["allowed"] else "%s CLOSED") % item["symbol"],
              "-", item["reason"])
    print("Подтверждено доступных инструментов:", len(active))
    return 1 if details and all(x["reason"] == "status_probe_error" for x in details) else 0


@single_run
def one_cycle(dry_run=False, provider="openai"):
    if provider not in ("openai", "ollama"):
        raise ValueError("Unknown decision provider")
    now = datetime.now(timezone.utc)
    cfg = config() if provider == "openai" else risk_policy.limits(shared.cfg())
    if c.TOKEN_PATH.name != "sandbox-token":
        raise c.APIError("Production token rejected: Sandbox only")
    account_ref = t.config()["account_ref"]
    if not isinstance(account_ref, str) or not account_ref.startswith("tb_"):
        raise c.APIError("Не настроен виртуальный Sandbox-счёт")
    api = t.s.SandboxAPI(c.token_read())
    account_id = t.get_account(api, account_ref)
    st = ai.init_ai_state(t.load_json(t.STATE_FILE, {}), now)
    # Reconciliation precedes every cloud/session/budget check. Offline supervision
    # has its own command/timer too, and never requires an OpenAI key.
    if dry_run:
        if st.get("pending"):
            ai.log("openai_dry_run_skipped", reason="pending_order")
            return 0
    elif t.reconcile_pending(api, account_id, st, now):
        ledger.heartbeat("trader", "pending_order", now=now)
        return 0
    if api.call("trader_orders", {"accountId": account_id}).get("orders", []):
        ai.log("openai_active_orders_skip")
        return 0
    if provider == "openai" and guard.is_blocked():
        ai.log("openai_quota_circuit_open", dry_run=dry_run)
        ledger.heartbeat("trader", "provider_blocked", now=now)
        return 0
    open_symbols, market_checks = session.open_watchlist(api, cfg["watchlist"])
    ledger.heartbeat("market_status", "open" if open_symbols else "closed_or_unverified",
                     {"checks": market_checks}, now)
    ai.log("openai_trading_status", checks=market_checks, dry_run=dry_run)
    if not open_symbols:
        ai.log("openai_market_closed", reason="no_verified_open_instruments", dry_run=dry_run)
        return 0
    cfg["watchlist"] = open_symbols
    context, portfolio, positions = ai.build_context(api, account_id, cfg, now)
    _quality(cfg, context, datetime.now(timezone.utc))
    equity, cash = Decimal(context["equity_rub"]), Decimal(context["cash_rub"])
    if not dry_run:
        st = t.initialize_day(st, now, equity)
        st["risk_policy"] = {k: v for k, v in cfg.items() if k in risk_policy.limits({})}
        t.atomic_save(t.STATE_FILE, st)
        shared.equity_event(equity, cash, now)
        ledger.portfolio(equity, cash, portfolio.get("positions", []), "trader", now)
        if st.get("orders_today", 0) >= cfg["max_orders_per_day"] or t.order_recent(st, now, cfg["cooldown_minutes"]):
            ai.log("openai_precheck_skip", reason="order_limit_or_cooldown")
            ledger.heartbeat("trader", "risk_pause", now=now)
            return 0
    cycle_id = ledger.start_cycle(context, "dry-run" if dry_run else "run",
                                  provider, cfg["model"], ai.SYSTEM, now)
    context["_cycle_id"], context["_mode"] = cycle_id, "dry-run" if dry_run else "run"
    st["cycle_id"] = cycle_id
    try:
        # All providers share decision/risk interfaces. Budget reservation occurs
        # inside ask_openai, BEFORE a request, also for manual/dry-run callers.
        if provider == "openai":
            decision, usage = ai.ask_openai(ai.read_key(), cfg["model"], context)
        else:
            decision, usage = shared.local_decide(context, cfg["model"]), {}
    except model_budget.BudgetBlocked as exc:
        ledger.finish_cycle(cycle_id, "budget_blocked")
        ledger.heartbeat("trader", "budget_pause", {"reason": str(exc)})
        ai.log("openai_budget_pause", reason=str(exc))
        return 0
    except Exception:
        ledger.finish_cycle(cycle_id, "provider_error")
        raise
    ai.log("ai_decision", provider=provider, action=decision["action"], symbol=decision["symbol"],
           lots=decision["lots"], confidence=decision["confidence"], reason=decision["reason"],
           risks=decision["risks"], usage=usage, dry_run=dry_run, cycle_id=cycle_id)
    trade, why = ai.validate(decision, context, cfg, st, now)
    ledger.decision(cycle_id, decision, why)
    if trade is None:
        ledger.finish_cycle(cycle_id, "hold" if why == "HOLD" else "rejected")
        ledger.heartbeat("trader", "hold" if why == "HOLD" else "rejected", {"reason": why, "mode": context["_mode"]})
        ai.log("openai_no_order", reason=why)
        return 0
    if dry_run:
        ledger.finish_cycle(cycle_id, "dry_run")
        ai.log("openai_dry_run_only", action=trade["action"], symbol=trade["symbol"], lots=trade["lots"])
        return 0
    inst = trade["instrument"]
    is_open, reason = session.instrument_open(api, inst["uid"])
    if not is_open:
        ledger.finish_cycle(cycle_id, "market_changed")
        return 0
    _quality(cfg, {"tradable": [inst]}, datetime.now(timezone.utc))
    is_open, reason = session.instrument_open(api, inst["uid"])
    if not is_open:
        ledger.finish_cycle(cycle_id, "market_changed")
        return 0
    # post_order performs a fresh live book/portfolio/status/size check for ALL engines.
    t.post_order(api, account_id, {"ticker": trade["symbol"], "uid": inst["uid"], "lot": inst["lot"]},
                 trade["action"], trade["lots"], Decimal(inst["price_rub"]),
                 "openai: " + decision["reason"][:90], st, datetime.now(timezone.utc))
    ledger.heartbeat("trader", "submitted", {"symbol": trade["symbol"]})
    ai.log("execution", provider=provider, action=trade["action"], symbol=trade["symbol"],
           lots=trade["lots"], result="submitted_to_TInvest_Sandbox", cycle_id=cycle_id)
    return 0


def main():
    p = argparse.ArgumentParser(description="OpenAI API decision engine - T-Invest Sandbox ONLY")
    p.add_argument("action", choices=("doctor","status","session","dry-run","run","reset-api-block"))
    action = p.parse_args().action
    try:
        if action == "reset-api-block":
            guard.reset_quota()
            print("OpenAI quota circuit reset; this does not enable the trading timer.")
            return 0
        if action == "doctor": return doctor()
        if action == "status": return status()
        if action == "session": return session_status()
        return one_cycle(dry_run=(action == "dry-run"))
    except (c.APIError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError, model_budget.BudgetBlocked) as exc:
        # Secrets are never logged. Broker/API errors already use sanitized messages.
        ledger.heartbeat("trader", "error", {"error": type(exc).__name__})
        ai.log("openai_sandbox_error", reason=type(exc).__name__+": "+str(exc)[:180])
        return 1


if __name__ == "__main__":
    sys.exit(main())
