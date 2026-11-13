"""Atomic accounting for ALL billable model calls, including dry-run and failures."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo
import trade_ledger as ledger
from risk_policy import MAX_API_CALLS_DAY

LEGACY_STATE_PATH = Path.home() / ".local/state/tinvest-sandbox-trader/state.json"
CONFIG_PATH = Path.home() / ".config/tinvest-readonly/model-budget.json"
TZ = ZoneInfo("Europe/Moscow")
DEFAULT = {"max_calls_per_day": 24, "min_interval_minutes": 30,
           "max_input_tokens": 24000, "max_output_tokens": 2400,
           "max_cost_usd_per_day": None, "input_usd_per_million": None,
           "output_usd_per_million": None, "priced_model": None}


class BudgetBlocked(Exception):
    pass


def config(path=None):
    path = Path(path or CONFIG_PATH)
    out = dict(DEFAULT)
    if path.is_file():
        values = json.loads(path.read_text("utf-8"))
        if not isinstance(values, dict) or set(values)-set(DEFAULT):
            raise ValueError("Invalid model-budget.json")
        out.update(values)
    for name, low, high in (("max_calls_per_day", 1, 24), ("min_interval_minutes", 0, 1440),
                             ("max_input_tokens", 1, 24000), ("max_output_tokens", 1, 2400)):
        value = out[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("Invalid budget setting: " + name)
    for key in ("max_cost_usd_per_day", "input_usd_per_million", "output_usd_per_million"):
        if out[key] is not None:
            value = Decimal(str(out[key]))
            if not value.is_finite() or value < 0:
                raise ValueError("Invalid cost setting: " + key)
    return out


def quote(cfg, model, input_tokens, output_tokens):
    if cfg["priced_model"] != model or any(cfg[k] is None for k in ("input_usd_per_million", "output_usd_per_million")):
        return None
    return ((Decimal(str(cfg["input_usd_per_million"]))*input_tokens +
             Decimal(str(cfg["output_usd_per_million"]))*output_tokens) / Decimal(1000000))


def import_legacy_usage(db, day, now):
    # Preserve today's already-consumed v1.10 allowance on the first v1.11 call.
    if db.execute("SELECT 1 FROM model_calls WHERE day=? LIMIT 1", (day,)).fetchone():
        return
    try:
        state = json.loads(LEGACY_STATE_PATH.read_text("utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError, TypeError):
        raise BudgetBlocked("legacy_usage_unverified") from None
    if not isinstance(state, dict):
        raise BudgetBlocked("legacy_usage_unverified")
    if state.get("ai_day") != day:
        return
    count = state.get("ai_calls_today", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise BudgetBlocked("legacy_usage_unverified")
    for index in range(min(count, MAX_API_CALLS_DAY)):
        db.execute("INSERT OR IGNORE INTO model_calls(id,utc,day,model,mode,status) VALUES(?,?,?,?,?,?)",
                   ("legacy:"+day+":"+str(index),ledger.stamp(now),day,"legacy","run","imported_unknown_cost"))


def reserve(model, mode, input_bytes, cycle_id=None, now=None, cfg=None):
    cfg = cfg or config()
    now = now or datetime.now(timezone.utc)
    # UTF-8 byte count is a conservative input-token bound, including instructions/schema.
    if input_bytes > cfg["max_input_tokens"]:
        raise BudgetBlocked("input_budget_exceeded")
    reserved = quote(cfg, model, input_bytes, cfg["max_output_tokens"])
    if cfg["max_cost_usd_per_day"] is not None and reserved is None:
        raise BudgetBlocked("cost_rates_unverified_for_model")
    day = now.astimezone(TZ).date().isoformat()
    call_id = str(uuid.uuid4())
    with ledger.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        import_legacy_usage(db, day, now)
    with ledger.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT utc,reserved_cost_usd,actual_cost_usd FROM model_calls WHERE day=?", (day,)).fetchall()
        if len(rows) >= min(cfg["max_calls_per_day"], MAX_API_CALLS_DAY):
            raise BudgetBlocked("daily_call_budget_reached")
        latest = db.execute("SELECT utc FROM model_calls ORDER BY utc DESC LIMIT 1").fetchone()
        if latest and (now-datetime.fromisoformat(latest["utc"])).total_seconds() < cfg["min_interval_minutes"]*60:
            raise BudgetBlocked("minimum_call_interval")
        if cfg["max_cost_usd_per_day"] is not None and any(r["actual_cost_usd"] is None and r["reserved_cost_usd"] is None for r in rows):
            raise BudgetBlocked("historical_cost_unverified")
        # Failures/unknown responses keep the full reservation, so retries cannot evade a money cap.
        spent = sum((Decimal(r["actual_cost_usd"] or r["reserved_cost_usd"] or "0") for r in rows), Decimal(0))
        if cfg["max_cost_usd_per_day"] is not None and spent + reserved > Decimal(str(cfg["max_cost_usd_per_day"])):
            raise BudgetBlocked("daily_cost_budget_reached")
        db.execute("INSERT INTO model_calls(id,cycle_id,utc,day,model,mode,status,reserved_cost_usd) "
                   "VALUES(?,?,?,?,?,?,?,?)", (call_id, cycle_id, ledger.stamp(now), day, model, mode,
                    "reserved", str(reserved) if reserved is not None else None))
    return call_id


def complete(call_id, model, usage=None, error_code=None, cfg=None):
    cfg = cfg or config()
    usage = usage or {}
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    cost = quote(cfg, model, inp, out) if isinstance(inp, int) and isinstance(out, int) and inp >= 0 and out >= 0 else None
    with ledger.connection() as db:
        db.execute("UPDATE model_calls SET status=?,input_tokens=?,output_tokens=?,actual_cost_usd=?,error_code=? WHERE id=?",
                   ("failed" if error_code else "completed", inp, out,
                    str(cost) if cost is not None else None, error_code, call_id))


def status(path=None, now=None):
    cfg = config()
    path = Path(path or ledger.DB_PATH)
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(TZ).date().isoformat()
    rows = []
    if path.is_file():
        with ledger.connection(path, readonly=True) as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM model_calls WHERE day=?", (day,))]
    legacy_calls = 0
    if not rows and LEGACY_STATE_PATH.is_file():
        state = json.loads(LEGACY_STATE_PATH.read_text("utf-8"))
        if not isinstance(state,dict):
            raise ValueError("Legacy usage unverified")
        if state.get("ai_day") == day:
            value = state.get("ai_calls_today", 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Legacy usage unverified")
            legacy_calls = min(value, MAX_API_CALLS_DAY)
    count = len(rows)+legacy_calls
    known = [r for r in rows if r["actual_cost_usd"] is not None]
    return {"day_moscow": day, "calls": count, "remaining_calls": max(0, cfg["max_calls_per_day"]-count),
            "max_calls": cfg["max_calls_per_day"], "min_interval_minutes": cfg["min_interval_minutes"],
            "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
            "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
            "priced_calls": len(known), "actual_cost_usd": str(sum((Decimal(r["actual_cost_usd"]) for r in known), Decimal(0))) if known else None,
            "cost_limit_usd": cfg["max_cost_usd_per_day"],
            "note": "Unknown pricing is not treated as zero cost; failed requests count toward the call cap."}
