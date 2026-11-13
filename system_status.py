"""Offline, read-only dashboard status; no HTTP probes or credential contents."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import trade_ledger as ledger
import model_budget
import v19_health
import v19_guard
from version import VERSION

CONFIG_DIR = Path.home() / ".config/tinvest-readonly"


def report(db_path=None, ledger_path=None, now=None):
    now = now or datetime.now(timezone.utc)
    db_path = Path(db_path or v19_health.DB)
    result = {"version": VERSION, "generated_utc": now.isoformat(), "mode": "Sandbox",
              "network_requests": 0, "openai_block": v19_guard.block_status(),
              "credentials_present": {"sandbox": (CONFIG_DIR/"sandbox-token").is_file(),
                                      "openai": (CONFIG_DIR/"openai-api-key").is_file()},
              "proxy_configured": (CONFIG_DIR/"proxychains.conf").is_file()}
    try:
        result["budget"] = model_budget.status(ledger_path, now)
    except (ValueError, OSError, sqlite3.Error):
        result["budget"] = {"error": "budget_unavailable"}
    try:
        result["market"] = v19_health.report(db_path, CONFIG_DIR/"market-recorder.json", now=now)
    except (ValueError, OSError, sqlite3.Error, KeyError):
        result["market"] = {"error": "market_unavailable"}
    try:
        data = ledger.summary(ledger_path, limit=30)
        for item in data["heartbeats"]:
            age = (now-datetime.fromisoformat(item["utc"])).total_seconds()
            item["age_seconds"] = round(age)
            item["recent"] = 0 <= age <= (720 if item["service"] == "recorder" else 180)
        result["services"] = data["heartbeats"]
        result["portfolio"] = data["portfolio"]
        result["recent_decisions"] = data["cycles"][:10]
        result["recent_events"] = data["events"][:15]
        result["orders"] = data["orders"][:15]
    except (ValueError, OSError, sqlite3.Error):
        result.update(services=[], portfolio=None, recent_decisions=[], recent_events=[], orders=[])
    result["note"] = "Service status is based on recorded observations, not a live connectivity probe."
    return result


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, indent=2))
