#!/usr/bin/env python3
"""Offline market-data audit. Read-only SQLite, no T-Invest/OpenAI network calls.

Gap counts refer ONLY to intervals between the first and last observed
snapshot for each instrument. They are not exchange-calendar completeness.
No performance or profitability claims are inferred from quotation changes.
"""
from __future__ import annotations
import argparse
import csv
import io
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import statistics

DB = Path.home() / ".local/share/tinvest-market/market.sqlite3"
CFG = Path.home() / ".config/tinvest-readonly/market-recorder.json"

def parse_utc(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("naive timestamp")
    return dt.astimezone(timezone.utc)

def examine(db_path=DB, config_path=CFG, hours=24, now=None,
            max_spread_pct=0.20, gap_minutes=5):
    if not 1 <= hours <= 24*90:
        raise ValueError("hours out of range")
    if not 0 < max_spread_pct <= 100:
        raise ValueError("max spread out of range")
    if gap_minutes != 5:
        raise ValueError("only five-minute collection slots supported")
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        output = []
        for spec in config["instruments"]:
            # Resolve exact trading board and ISIN (if present); ticker in DB
            # may differ from the configured security identifier.
            rows = [dict(r) for r in db.execute("""
                SELECT s.slot_utc, s.collected_utc, s.quality_status, s.quality_reason,
                       s.spread_pct, s.book_stale_seconds, s.best_bid, s.best_ask,
                       s.bids_total, s.asks_total
                FROM snapshots s JOIN instruments i ON i.uid=s.uid
                WHERE i.board=? AND (i.ticker=? OR i.isin=?)
                  AND s.collected_utc>=? AND s.collected_utc<=?
                ORDER BY s.slot_utc, s.collected_utc
            """, (spec["board"],spec["symbol"],spec["symbol"],
                  start.isoformat(), now.isoformat()))]
            # Collapse re-collections within a single five-minute slot.
            slots = {}
            for row in rows:
                slots[row["slot_utc"]] = row
            unique = [slots[key] for key in sorted(slots, key=parse_utc)]
            counter = Counter(row["quality_status"] for row in unique)
            valid = [r for r in unique if r["quality_status"] == "valid"]
            spread = [r["spread_pct"] for r in valid
                      if isinstance(r["spread_pct"], (float,int))
                      and 0 <= r["spread_pct"] <= 100]
            wide = sum(r["spread_pct"] is not None and
                       r["spread_pct"] > max_spread_pct for r in valid)
            anomalies = Counter(r["quality_reason"] for r in unique
                                if r["quality_status"] != "valid")
            missing = 0
            gaps = []
            for left, right in zip(unique, unique[1:]):
                seconds = (parse_utc(right["slot_utc"]) -
                           parse_utc(left["slot_utc"])).total_seconds()
                gap = max(0, round(seconds/300)-1)
                if gap:
                    missing += gap
                    gaps.append({"after_slot_utc":left["slot_utc"],
                                 "before_slot_utc":right["slot_utc"],
                                 "missing_slots":gap})
            count=len(unique)
            output.append({
                "symbol":spec["symbol"],"board":spec["board"],
                "snapshots":count,
                "quality_counts":{k:counter[k] for k in ("valid","stale","unverified","invalid")},
                "valid_pct":round(100*len(valid)/count,2) if count else None,
                "median_valid_spread_pct":round(statistics.median(spread),5) if spread else None,
                "wide_spread_valid_count":wide,
                "missing_slots_between_observations":missing,
                "gaps":gaps[:20], "anomaly_reasons":dict(anomalies),
                "last_snapshot_utc":unique[-1]["collected_utc"] if count else None,
                "last_status":unique[-1]["quality_status"] if count else "no_snapshot"
            })
    return {"as_of_utc":now.isoformat(), "window_hours":hours,
            "spread_threshold_pct":max_spread_pct,
            "gap_scope":"between_observed_slots_only_not_exchange_schedule",
            "configured":len(output),
            "with_snapshots":sum(x["snapshots"]>0 for x in output),
            "instruments":output,
            "disclaimer":"Market-data quality only; no trading returns, backtest, or order execution.",
            "network_requests":0}

def csv_text(result):
    buf=io.StringIO()
    writer=csv.writer(buf)
    writer.writerow(["symbol","board","snapshots","valid_pct","median_valid_spread_pct",
                     "wide_spread_valid_count","missing_slots_between_observations",
                     "last_snapshot_utc","last_status"])
    for r in result["instruments"]:
        writer.writerow([r.get(k) for k in
            ("symbol","board","snapshots","valid_pct","median_valid_spread_pct",
             "wide_spread_valid_count","missing_slots_between_observations",
             "last_snapshot_utc","last_status")])
    return buf.getvalue()

def main():
    parser=argparse.ArgumentParser(description="Read-only offline audit of T-Invest SQLite")
    parser.add_argument("--hours",type=int,default=24)
    parser.add_argument("--max-spread-pct",type=float,default=0.20)
    parser.add_argument("--format",choices=("json","csv"),default="json")
    args=parser.parse_args()
    result=examine(hours=args.hours,max_spread_pct=args.max_spread_pct)
    if args.format=="csv":
        print(csv_text(result),end="")
    else:
        print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
