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
            row = db.execute(
                """SELECT s.collected_utc, s.quality_status, s.quality_reason,
                          s.spread_pct, s.book_stale_seconds
                   FROM snapshots s JOIN instruments i ON i.uid=s.uid
                   WHERE i.ticker=? AND i.board=?
                   ORDER BY s.collected_utc DESC LIMIT 1""",
                (spec["symbol"],spec["board"])).fetchone()
            if row is None:
                items.append({"symbol":spec["symbol"],"board":spec["board"],
                              "healthy":False,"reason":"no_snapshot"})
                continue
            stamp = datetime.fromisoformat(row["collected_utc"].replace("Z","+00:00"))
            age = max(0,(now-stamp).total_seconds()/60)
            healthy = (row["quality_status"]=="valid" and age<=max_age_minutes
                       and row["book_stale_seconds"] is not None
                       and row["book_stale_seconds"]<=max_age_minutes*60)
            items.append({"symbol":spec["symbol"],"board":spec["board"],
                "healthy":healthy,
                "reason":"ok" if healthy else ("old_snapshot" if age>max_age_minutes
                      else row["quality_reason"] if row["quality_status"]!="valid"
                      else "old_orderbook"),
                "age_minutes":round(age,1),"spread_pct":row["spread_pct"]})
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
