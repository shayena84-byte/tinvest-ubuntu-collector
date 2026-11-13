"""Shared hard ceilings for every Sandbox decision engine. No network access."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

MAX_TRADE_FRACTION = 0.10
MAX_EXPOSURE_FRACTION = 0.50
MAX_DAILY_LOSS_FRACTION = 0.03
MAX_ORDERS_DAY = 3
MAX_API_CALLS_DAY = 24
MIN_CONFIDENCE = 0.70


def limits(config):
    result = dict(config)
    for key, ceiling in (("max_trade_fraction", MAX_TRADE_FRACTION),
                         ("max_exposure_fraction", MAX_EXPOSURE_FRACTION),
                         ("max_daily_loss_fraction", MAX_DAILY_LOSS_FRACTION)):
        value = float(result.get(key, ceiling))
        if not 0 < value < 1:
            raise ValueError("Invalid risk setting: " + key)
        result[key] = min(value, ceiling)
    for key, ceiling in (("max_orders_per_day", MAX_ORDERS_DAY),
                         ("max_ai_calls_per_day", MAX_API_CALLS_DAY)):
        value = result.get(key, ceiling)
        if isinstance(value, bool) or int(value) != value or int(value) <= 0:
            raise ValueError("Invalid integer risk setting: " + key)
        result[key] = min(int(value), ceiling)
    confidence = float(result.get("minimum_confidence", MIN_CONFIDENCE))
    if not 0 < confidence <= 1:
        raise ValueError("Invalid minimum confidence")
    result["minimum_confidence"] = max(confidence, MIN_CONFIDENCE)
    result.setdefault("minimum_cash_rub", 850)
    result.setdefault("cooldown_minutes", 20)
    if float(result["minimum_cash_rub"]) < 0 or float(result["cooldown_minutes"]) < 0:
        raise ValueError("Negative cash reserve or cooldown")
    return result


def classic_limits(config):
    result = dict(config)
    translated = limits({"max_trade_fraction": result["max_position_fraction"],
                         "max_exposure_fraction": result["max_gross_fraction"],
                         "max_daily_loss_fraction": result["daily_loss_limit"],
                         "max_orders_per_day": result["max_orders_per_day"]})
    for old, new in (("max_position_fraction", "max_trade_fraction"),
                     ("max_gross_fraction", "max_exposure_fraction"),
                     ("daily_loss_limit", "max_daily_loss_fraction"),
                     ("max_orders_per_day", "max_orders_per_day")):
        result[old] = translated[new]
    return result


def validate(decision, context, config, state, now):
    cfg = limits(config)
    action = decision["action"]
    symbol = decision["symbol"].upper().strip()
    if action == "HOLD":
        return None, "HOLD" if symbol == "NONE" and decision["lots"] == 0 else "Invalid HOLD"
    if action not in ("BUY", "SELL"):
        return None, "Invalid action"
    if decision["confidence"] < cfg["minimum_confidence"]:
        return None, "Confidence below threshold"
    candidates = {row["symbol"]: row for row in context["tradable"]}
    if symbol not in candidates:
        return None, "Non-tradable symbol"
    inst = candidates[symbol]
    qty = decision["lots"]
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        return None, "Zero or invalid lots"
    if state.get("orders_today", 0) >= cfg["max_orders_per_day"]:
        return None, "Order count limit"
    if state.get("last_order_utc"):
        last = datetime.fromisoformat(state["last_order_utc"])
        if (now - last).total_seconds() < cfg["cooldown_minutes"] * 60:
            return None, "Cooldown active"
    equity = Decimal(context["equity_rub"])
    cash = Decimal(context["cash_rub"])
    invested = Decimal(context.get("invested_rub_estimate", str(max(Decimal(0), equity-cash))))
    # Use the less favourable of last price and displayed ask for buy affordability.
    price = Decimal(inst["price_rub"])
    if action == "BUY":
        if inst["owned_lots"] > 0:
            return None, "Already held; prevent unintended averaging and concentration"
        if state.get("start_equity") and equity < Decimal(state["start_equity"]) * (
                1-Decimal(str(cfg["max_daily_loss_fraction"]))):
            return None, "Daily loss limit"
        if "book_ask" in inst:
            price = max(price, Decimal(inst["book_ask"]))
        max_amount = min(Decimal(str(cfg["max_trade_fraction"]))*equity,
                         Decimal(str(cfg["max_exposure_fraction"]))*equity-invested,
                         cash-Decimal(str(cfg["minimum_cash_rub"])))
        if not price.is_finite() or price <= 0 or max_amount <= 0 or (
                price * int(inst["lot"]) * qty * Decimal("1.002") > max_amount):
            return None, "Cash, single-trade, or total exposure cap"
    elif inst["owned_lots"] < qty:
        return None, "Cannot sell more lots than held"
    return {"action": action, "symbol": symbol, "lots": qty, "instrument": inst}, None
