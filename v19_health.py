#!/usr/bin/env python3
"""Read-only quality/coverage monitor for T-Invest market SQLite.

Does not access broker accounts, call OpenAI, or place any orders.
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
import market_quality

DB = Path.home() / ".local/share/tinvest-market/market.sqlite3"
CONFIG = Path.home() / ".config/tinvest-readonly/market-recorder.json"

def report(db_path=DB, config_path=CONFIG, now=None, max_age_minutes=12):
    now = now or datetime.now(timezone.utc)
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    instruments = cfg["instruments"]
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        items=[]
        for spec in instruments:
            uid = market_quality.resolve_uid(db, spec["symbol"], spec["board"])
            columns = {r[1] for r in db.execute("PRAGMA table_info(snapshots)")}
            book_column = "book_time_utc" if "book_time_utc" in columns else "NULL AS book_time_utc"
            row = db.execute(
                "SELECT collected_utc,quality_status,quality_reason,spread_pct,"+book_column+
                " FROM snapshots WHERE uid=? ORDER BY collected_utc DESC LIMIT 1", (uid,)).fetchone()
            if row is None:
                items.append({"symbol":spec["symbol"],"board":spec["board"],
                              "healthy":False,"reason":"no_snapshot"})
                continue
            fresh, reason = market_quality.freshness(row["collected_utc"], row["book_time_utc"],
                         now, max_age_minutes*60, max_age_minutes*60)
            healthy = fresh and row["quality_status"] == "valid"
            if fresh and not healthy:
                reason = row["quality_reason"]
            try:
                age = round(market_quality.age_seconds(row["collected_utc"], now)/60, 1)
            except (ValueError, TypeError):
                age = None
            items.append({"symbol":spec["symbol"],"board":spec["board"],"uid":uid,
                          "healthy":healthy,"reason":"ok" if healthy else reason,
                          "age_minutes":age,"spread_pct":row["spread_pct"]})
    return {"checked_utc":now.isoformat(),"configured":len(instruments),
            "healthy":sum(x["healthy"] for x in items),"instruments":items}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--max-age-minutes",type=int,default=12)
    args=p.parse_args()
    if not 1<=args.max_age_minutes<=120:
        p.error("max age must be between 1 and 120 minutes")
    result=report(max_age_minutes=args.max_age_minutes)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["configured"]==result["healthy"] else 2

if __name__=="__main__":
    raise SystemExit(main())
