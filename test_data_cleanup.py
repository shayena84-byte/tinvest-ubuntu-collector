#!/usr/bin/env python3
"""Remove only the two precisely identified v1.11.0 mocked dry-run fixtures.

Never delete actual orders, model calls, positions, quota state or market data.
The default command previews candidates without modifying the database.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

DB = Path.home()/".local/share/tinvest-market/trading.sqlite3"
CONTEXT = {"tradable":[{"symbol":"SBER","uid":"UID","lot":10,"price_rub":"300","owned_lots":0}],
           "equity_rub":"10000","cash_rub":"10000","invested_rub_estimate":"0"}
DECISIONS = (
    {"action":"HOLD","symbol":"NONE","lots":0,"confidence":.9,
     "reason":"Наблюдение без сделки","risks":"Возможна волатильность","horizon_minutes":30},
    {"action":"HOLD","symbol":"NONE","lots":0,"confidence":.95,
     "reason":"Наблюдения недостаточно сильны для покупки",
     "risks":"Рынок может двигаться против модели","horizon_minutes":30},
)


def cleanup(path=None, apply=False, backup=False):
    path = Path(path or DB)
    result = {"candidates":[],"removed":0,"applied":apply}
    if not path.is_file():
        return result
    uri = path.resolve().as_uri()+("?mode=rw" if apply else "?mode=ro")
    db = sqlite3.connect(uri, uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"cycles","orders","model_calls","events"}.issubset(tables):
            result["skipped"] = "unrecognized_schema"
            return result
        if apply:
            db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT id,context_json,decision_json FROM cycles WHERE version='1.11.0' "
                              "AND provider='openai' AND model='gpt-6.1-sol' AND mode='dry-run' "
                              "AND validation='HOLD' AND status='hold'"):
            try:
                is_fixture = json.loads(row["context_json"]) == CONTEXT and json.loads(row["decision_json"]) in DECISIONS
            except (TypeError, ValueError):
                is_fixture = False
            if not is_fixture:
                continue
            # A billable call/order/event linkage always wins over fixture matching.
            if any(db.execute("SELECT 1 FROM "+table+" WHERE cycle_id=? LIMIT 1",(row["id"],)).fetchone()
                   for table in ("orders","model_calls","events")):
                continue
            result["candidates"].append(row["id"])
        if apply and result["candidates"]:
            if backup:
                # Use a second read connection for a consistent pre-cleanup backup.
                target = path.with_name(path.name+".before-fixture-clean-"+
                                        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+".bak")
                fd = os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                os.close(fd)
                with sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True) as source:
                    with sqlite3.connect(target) as destination:
                        source.backup(destination)
                result["backup"] = str(target)
            db.executemany("DELETE FROM cycles WHERE id=?",((i,) for i in result["candidates"]))
            result["removed"] = len(result["candidates"])
        db.commit()
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=DB)
    parser.add_argument("--apply",action="store_true")
    args = parser.parse_args()
    print(json.dumps(cleanup(args.db,apply=args.apply,backup=args.apply),ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
