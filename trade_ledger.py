"""Private SQLite audit trail. No credentials or brokerage account IDs are stored."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from version import VERSION

DB_PATH = Path.home() / ".local/share/tinvest-market/trading.sqlite3"
SECRET_KEYS = frozenset(("token", "authorization", "api_key", "key", "accountid", "account_id", "headers"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
 id TEXT PRIMARY KEY, utc TEXT NOT NULL, mode TEXT NOT NULL, provider TEXT,
 model TEXT, version TEXT NOT NULL, prompt_hash TEXT, context_json TEXT,
 decision_json TEXT, validation TEXT, status TEXT NOT NULL DEFAULT 'started');
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, utc TEXT NOT NULL, cycle_id TEXT, kind TEXT NOT NULL,
 details_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_time ON events(utc);
CREATE TABLE IF NOT EXISTS orders (
 client_id TEXT PRIMARY KEY, broker_id TEXT, cycle_id TEXT, uid TEXT NOT NULL,
 symbol TEXT NOT NULL, side TEXT NOT NULL, requested_lots INTEGER NOT NULL,
 reference_price TEXT, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL,
 status TEXT NOT NULL, filled_lots INTEGER NOT NULL DEFAULT 0,
 average_price TEXT, commission_rub TEXT);
CREATE TABLE IF NOT EXISTS fills (
 client_id TEXT NOT NULL REFERENCES orders(client_id), trade_id TEXT NOT NULL,
 utc TEXT, lots INTEGER, price TEXT, PRIMARY KEY(client_id,trade_id));
CREATE TABLE IF NOT EXISTS portfolio_samples (
 id INTEGER PRIMARY KEY, utc TEXT NOT NULL, equity_rub TEXT NOT NULL,
 cash_rub TEXT NOT NULL, positions_json TEXT NOT NULL, source TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS portfolio_time ON portfolio_samples(utc);
CREATE TABLE IF NOT EXISTS heartbeats (
 service TEXT PRIMARY KEY, utc TEXT NOT NULL, status TEXT NOT NULL, details_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls (
 id TEXT PRIMARY KEY, cycle_id TEXT, utc TEXT NOT NULL, day TEXT NOT NULL,
 model TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
 reserved_cost_usd TEXT, actual_cost_usd TEXT, input_tokens INTEGER,
 output_tokens INTEGER, error_code TEXT);
CREATE INDEX IF NOT EXISTS model_calls_day ON model_calls(day);
PRAGMA user_version=1;
"""


def stamp(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()
                if str(k).lower() not in SECRET_KEYS and not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
    return value


def encoded(value):
    return json.dumps(clean(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@contextmanager
def connection(path=None, readonly=False):
    path = Path(path or DB_PATH)
    if readonly:
        db = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=8)
        db.execute("PRAGMA query_only=ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite can open it with the process's wider umask.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        db = sqlite3.connect(path, timeout=8)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def event(kind, details=None, cycle_id=None, now=None):
    with connection() as db:
        db.execute("INSERT INTO events(utc,cycle_id,kind,details_json) VALUES(?,?,?,?)",
                   (stamp(now), cycle_id, kind, encoded(details or {})))


def heartbeat(service, status, details=None, now=None):
    with connection() as db:
        db.execute("INSERT INTO heartbeats VALUES(?,?,?,?) ON CONFLICT(service) DO UPDATE SET "
                   "utc=excluded.utc,status=excluded.status,details_json=excluded.details_json",
                   (service, stamp(now), status, encoded(details or {})))


def start_cycle(context, mode, provider, model, prompt="", now=None):
    cycle_id = str(uuid.uuid4())
    with connection() as db:
        db.execute("INSERT INTO cycles(id,utc,mode,provider,model,version,prompt_hash,context_json) "
                   "VALUES(?,?,?,?,?,?,?,?)", (cycle_id, stamp(now), mode, provider, model,
                    VERSION, hashlib.sha256(prompt.encode()).hexdigest(), encoded(context)))
    return cycle_id


def decision(cycle_id, value, validation):
    with connection() as db:
        db.execute("UPDATE cycles SET decision_json=?,validation=?,status=? WHERE id=?",
                   (encoded(value), validation or "approved", "decided", cycle_id))


def finish_cycle(cycle_id, status):
    with connection() as db:
        db.execute("UPDATE cycles SET status=? WHERE id=?", (status, cycle_id))


def portfolio(equity, cash, positions=None, source="supervisor", now=None):
    # Store explicitly selected position fields, never the broker response envelope.
    safe = [{k: p[k] for k in ("instrumentUid", "quantityLots", "averagePositionPrice", "currentPrice")
             if k in p} for p in positions or []]
    with connection() as db:
        db.execute("INSERT INTO portfolio_samples(utc,equity_rub,cash_rub,positions_json,source) "
                   "VALUES(?,?,?,?,?)", (stamp(now), str(equity), str(cash), encoded(safe), source))


def order_intent(client_id, spec, side, lots, price, cycle_id=None, now=None):
    with connection() as db:
        db.execute("INSERT INTO orders(client_id,cycle_id,uid,symbol,side,requested_lots,"
                   "reference_price,created_utc,updated_utc,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (client_id, cycle_id, spec["uid"], spec["ticker"], side, lots,
                    str(price), stamp(now), stamp(now), "intent"))


def order_state(client_id, response, now=None):
    # Import the numeric decoder lazily; the ledger itself has no broker client.
    from collector import val_quote
    with connection() as db:
        existing = db.execute("SELECT client_id FROM orders WHERE client_id=?", (client_id,)).fetchone()
        if not existing:
            event_data = {"client_id": client_id, "status": response.get("executionReportStatus")}
            db.execute("INSERT INTO events(utc,kind,details_json) VALUES(?,?,?)",
                       (stamp(now), "legacy_order_state", encoded(event_data)))
            return
        commission = response.get("executedCommission")
        fee = val_quote(commission) if isinstance(commission, dict) and commission.get("currency", "rub").lower() == "rub" else None
        db.execute("UPDATE orders SET broker_id=COALESCE(?,broker_id),updated_utc=?,status=?,"
                   "filled_lots=?,average_price=?,commission_rub=COALESCE(?,commission_rub) WHERE client_id=?",
                   (response.get("orderId"), stamp(now), response.get("executionReportStatus", "unknown"),
                    int(response.get("lotsExecuted", 0)), val_quote(response.get("executedOrderPrice")) or None,
                    fee, client_id))
        for stage in response.get("stages", []):
            if stage.get("tradeId"):
                db.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?)", (client_id,
                           str(stage["tradeId"]), stage.get("executionTime"),
                           int(stage.get("quantity", 0)), val_quote(stage.get("price"))))


def summary(path=None, limit=30):
    path = Path(path or DB_PATH)
    if not 1 <= limit <= 200:
        raise ValueError("Invalid ledger limit")
    if not path.is_file():
        return {"available": False, "cycles": [], "orders": [], "events": [], "portfolio": None, "heartbeats": []}
    with connection(path, readonly=True) as db:
        result = {"available": True}
        for name, table, order in (("cycles", "cycles", "utc"), ("orders", "orders", "created_utc"),
                                   ("events", "events", "id"), ("heartbeats", "heartbeats", "utc")):
            result[name] = [dict(r) for r in db.execute("SELECT * FROM " + table + " ORDER BY " + order + " DESC LIMIT ?", (limit,))]
        row = db.execute("SELECT * FROM portfolio_samples ORDER BY id DESC LIMIT 1").fetchone()
        result["portfolio"] = dict(row) if row else None
    # Do not return entire prompts/contexts on dashboard list endpoints.
    for cycle in result["cycles"]:
        cycle.pop("context_json", None)
    return result
