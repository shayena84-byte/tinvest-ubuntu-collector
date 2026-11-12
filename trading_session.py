#!/usr/bin/env python3
"""Live trading-session checks for T-Invest Sandbox market orders.

A real trading status per exact instrument UID is the ONLY authority for session
availability. Do not infer an open market from weekday, exchange time or candles.
No orders, account IDs or keys are sent from this module.
"""
from __future__ import annotations

import sandbox_autotrader as trader

NORMAL = "SECURITY_TRADING_STATUS_NORMAL_TRADING"


def status_reason(response, uid):
    """Return (allowed, reason) from GetTradingStatus response.

    Fail closed when flags are absent, non-boolean, or UID mismatches. An
    opening/closing auction, break, or dealer mode does not qualify as NORMAL.
    """
    if not isinstance(response, dict):
        return False, "invalid_status_response"
    if response.get("instrumentUid") != uid:
        return False, "instrument_uid_unverified"
    state = response.get("tradingStatus")
    if state != NORMAL:
        if isinstance(state, str) and state.startswith("SECURITY_TRADING_STATUS_"):
            return False, "trading_status_" + state.removeprefix("SECURITY_TRADING_STATUS_").lower()
        return False, "unknown_trading_status"
    if response.get("apiTradeAvailableFlag") is not True:
        return False, "api_trading_not_available"
    if response.get("marketOrderAvailableFlag") is not True:
        return False, "market_orders_not_available"
    return True, "normal_trading_verified"


def instrument_open(api, uid):
    if not isinstance(uid, str) or not uid:
        raise trader.c.APIError("No verified instrument UID for trading status")
    response = api.call("trader_status", {"instrumentId": uid})
    return status_reason(response, uid)


def open_watchlist(api, symbols):
    """Probe per-share status; return eligible symbols and safe diagnostic lines.

    Only TQBR and ticker-validated shares from find_share() can be admitted.
    A single unavailable instrument does not halt checks for other shares.
    The caller must recheck the selected UID immediately before placing orders.
    """
    permitted = []
    diagnostics = []
    for symbol in symbols:
        try:
            inst = trader.find_share(api, symbol)
            allowed, reason = instrument_open(api, inst["uid"])
        except (trader.c.APIError, ValueError, KeyError, TypeError):
            allowed, reason = False, "status_probe_error"
        diagnostics.append({"symbol": symbol, "allowed": allowed, "reason": reason})
        if allowed:
            permitted.append(symbol)
    return permitted, diagnostics
