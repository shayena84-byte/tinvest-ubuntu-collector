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
    cfg = ai.ai_config()
    cfg["max_trade_fraction"] = min(float(cfg["max_trade_fraction"]), MAX_TRADE_FRACTION)
    cfg["max_exposure_fraction"] = min(float(cfg["max_exposure_fraction"]), MAX_EXPOSURE_FRACTION)
    cfg["max_daily_loss_fraction"] = min(float(cfg["max_daily_loss_fraction"]), MAX_DAILY_LOSS_FRACTION)
    cfg["max_orders_per_day"] = min(int(cfg["max_orders_per_day"]), MAX_ORDERS_DAY)
    cfg["max_ai_calls_per_day"] = min(int(cfg["max_ai_calls_per_day"]), MAX_API_CALLS_DAY)
    cfg["minimum_confidence"] = max(float(cfg["minimum_confidence"]), REQUIRED_CONFIDENCE)
    cfg["max_snapshot_age_seconds"] = MAX_BOOK_AGE
    cfg["max_orderbook_spread_pct"] = MAX_SPREAD_PCT
    # Only shares watched by our market recorder can pass the quality gate.
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
        "daily_ai_calls": state.get("ai_calls_today", 0),
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


def one_cycle(dry_run=False):
    now = datetime.now(timezone.utc)
    cfg = config()
    if guard.is_blocked():
        ai.log("openai_quota_circuit_open", dry_run=dry_run)
        return 0
    # Trading availability is a per-instrument live broker status, not a
    # Moscow-time window. This deliberately supports any verified evening or
    # special session while remaining fail-closed on weekends and holidays.
    if c.TOKEN_PATH.name != "sandbox-token":
        raise c.APIError("Production token rejected: Sandbox only")
    old_cfg = t.config()
    account_ref = old_cfg["account_ref"]
    if not isinstance(account_ref, str) or not account_ref.startswith("tb_"):
        raise c.APIError("Не настроен виртуальный Sandbox-счёт")
    key = ai.read_key()  # existing private file; never echoed or sent to T-Invest
    api = t.s.SandboxAPI(c.token_read())
    open_symbols, market_checks = session.open_watchlist(api, cfg["watchlist"])
    ai.log("openai_trading_status", checks=market_checks, dry_run=dry_run)
    if not open_symbols:
        ai.log("openai_market_closed", reason="no_verified_open_instruments",
               dry_run=dry_run)
        return 0
    # Restrict both the technical-data builder and model to currently open
    # instruments. Its own exchange_open() remains a second independent check.
    cfg["watchlist"] = open_symbols
    account_id = t.get_account(api, account_ref)
    st = ai.init_ai_state(t.load_json(t.STATE_FILE, {}), now)

    # Dry-run is strictly read-only. Existing order reconciliation can cancel
    # stale orders and must not be invoked in dry-run mode.
    if dry_run:
        if st.get("pending"):
            ai.log("openai_dry_run_skipped", reason="pending_order")
            return 0
    else:
        if t.reconcile_pending(api, account_id, st, now):
            ai.log("openai_pending_skip")
            return 0
    open_orders = api.call("trader_orders", {"accountId": account_id}).get("orders", [])
    if open_orders:
        ai.log("openai_active_orders_skip", count=len(open_orders))
        return 0
    if st.get("ai_calls_today", 0) >= cfg["max_ai_calls_per_day"]:
        ai.log("openai_api_daily_budget_reached")
        return 0

    # Build dynamic technical context from T-Invest Sandbox market feed.
    context, portfolio, positions = ai.build_context(api, account_id, cfg, now)
    # Exclude stale/crossed/unknown/too-wide books from the model's choices.
    _quality(cfg, context, now)
    equity = Decimal(context["equity_rub"])
    cash = Decimal(context["cash_rub"])
    if not dry_run:
        # Daily risk state is persisted before any virtual trade.
        st = t.initialize_day(st, now, equity)
        t.atomic_save(t.STATE_FILE, st)
        shared.equity_event(equity, cash, now)
        # Count BEFORE requesting the OpenAI API, even if the network request fails.
        st["ai_calls_today"] = int(st.get("ai_calls_today", 0)) + 1
        t.atomic_save(t.STATE_FILE, st)

    # One cloud model decision, no tool access or brokerage credentials.
    try:
        decision, usage = ai.ask_openai(key, cfg["model"], context)
    except c.APIError as exc:
        if guard.is_quota_error(str(exc)):
            guard.block_quota()
            ai.log("openai_quota_circuit_open", dry_run=dry_run)
        raise
    ai.log("ai_decision", provider="openai", action=decision["action"],
           symbol=decision["symbol"], lots=decision["lots"],
           confidence=decision["confidence"], reason=decision["reason"],
           risks=decision["risks"], usage=usage, dry_run=dry_run)
    trade, why = ai.validate(decision, context, cfg, st, now)
    if trade is None:
        ai.log("openai_no_order", reason=why)
        return 0
    if dry_run:
        ai.log("openai_dry_run_only", action=trade["action"],
               symbol=trade["symbol"], lots=trade["lots"])
        return 0

    # Re-fetch virtual portfolio and verify the local quality snapshot again.
    new_port = api.call("portfolio", {"accountId": account_id, "currency": "RUB"})
    new_pos = api.call("positions", {"accountId": account_id})
    new_equity = t.d(new_port.get("totalAmountPortfolio"))
    new_cash = t.rub_cash(new_pos)
    if new_equity <= 0 or abs(new_equity-equity) > new_equity*Decimal(".02"):
        ai.log("openai_equity_changed_abort")
        return 0
    if api.call("trader_orders", {"accountId": account_id}).get("orders", []):
        ai.log("openai_order_race_abort")
        return 0
    instrument = trade["instrument"]
    # The market may have entered an auction/break/closed between model
    # response and trade execution. Re-read the trading status by exact UID.
    is_open, current_reason = session.instrument_open(api, instrument["uid"])
    if not is_open:
        ai.log("openai_market_changed_abort", symbol=trade["symbol"],
               reason=current_reason)
        return 0
    held = t.held_lots(new_port, instrument["uid"], instrument["lot"])
    if trade["action"] == "BUY" and (held > 0 or new_cash < cash*Decimal(".97")):
        ai.log("openai_buy_state_changed_abort")
        return 0
    if trade["action"] == "SELL" and held < trade["lots"]:
        ai.log("openai_sell_position_changed_abort")
        return 0
    _quality(cfg, {"tradable": [instrument]}, datetime.now(timezone.utc))
    # Check once more after the quality filter. A status-query failure is
    # fatal and leaves any virtual order unsent.
    is_open, current_reason = session.instrument_open(api, instrument["uid"])
    if not is_open:
        ai.log("openai_market_changed_abort", symbol=trade["symbol"],
               reason=current_reason)
        return 0

    # Independent final order size check. Recalculate affordability from new cash.
    price = Decimal(instrument["price_rub"])
    lots = int(trade["lots"])
    notion = price*int(instrument["lot"])*lots
    if trade["action"] == "BUY":
        max_buy = min(new_equity*Decimal(str(cfg["max_trade_fraction"])),
                      new_cash-Decimal(str(cfg["minimum_cash_rub"])))
        if notion*Decimal("1.002") > max_buy or max_buy <= 0:
            ai.log("openai_risk_limit_changed_abort")
            return 0
    spec = {"ticker": trade["symbol"], "uid": instrument["uid"],
            "lot": instrument["lot"]}
    t.post_order(api, account_id, spec, trade["action"], lots, price,
                 "openai: "+decision["reason"][:90],
                 st, datetime.now(timezone.utc))
    ai.log("execution", provider="openai", action=trade["action"],
           symbol=trade["symbol"], lots=lots,
           result="submitted_to_TInvest_Sandbox")
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
    except (c.APIError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        # Secrets are never logged. Broker/API errors already use sanitized messages.
        ai.log("openai_sandbox_error", reason=type(exc).__name__+": "+str(exc)[:180])
        return 1


if __name__ == "__main__":
    sys.exit(main())
