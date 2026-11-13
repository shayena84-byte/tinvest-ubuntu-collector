"""Offline v1.10 audit test fixtures: no network, orders, or DB mutation."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from offline_market_audit import examine,csv_text

class OfflineAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/"market.sqlite3"
        self.conf=Path(self.tmp.name)/"config.json"
        self.now=datetime(2026,11,13,12,0,tzinfo=timezone.utc)
        self.conf.write_text(json.dumps({"depth":10,"instruments":[
            {"symbol":"SBER","board":"TQBR","kind":"share"},
            {"symbol":"RU000A1092T7","board":"TQCB","kind":"bond"},
            {"symbol":"VTBR","board":"TQBR","kind":"share"}]}),encoding="utf-8")
        with sqlite3.connect(self.db) as db:
            db.executescript("""CREATE TABLE instruments(uid TEXT,ticker TEXT,isin TEXT,board TEXT);
                CREATE TABLE snapshots(uid TEXT,slot_utc TEXT,collected_utc TEXT,
                quality_status TEXT,quality_reason TEXT,spread_pct REAL,
                book_stale_seconds INTEGER,best_bid TEXT,best_ask TEXT,
                bids_total INTEGER,asks_total INTEGER);""")
            db.executemany("INSERT INTO instruments VALUES (?,?,?,?)",[
                ("uid1","SBER","RU0009029540","TQBR"),
                ("uid2","OMEGA","RU000A1092T7","TQCB")])
            def write(uid,minutes,status,spread,reason="ok"):
                stamp=(self.now-timedelta(minutes=minutes)).isoformat()
                db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (uid,stamp,stamp,status,reason,spread,0,"100","101",100,200))
            write("uid1",15,"valid",0.01)
            write("uid1",5,"valid",0.03)
            write("uid2",5,"unverified",None,"timestamp_missing")
    def test_gap_median_isin_missing(self):
        with sqlite3.connect(self.db) as db:
            before=db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        result=examine(self.db,self.conf,hours=1,now=self.now,max_spread_pct=0.02)
        a,b,c=result["instruments"]
        self.assertEqual(a["missing_slots_between_observations"],1)
        self.assertEqual(a["wide_spread_valid_count"],1)
        self.assertEqual(a["median_valid_spread_pct"],0.02)
        self.assertEqual(b["snapshots"],1) # resolved by ISIN, not ticker
        self.assertEqual(b["quality_counts"]["unverified"],1)
        self.assertEqual(c["last_status"],"no_snapshot")
        self.assertEqual(result["network_requests"],0)
        self.assertIn("SBER",csv_text(result))
        with sqlite3.connect(self.db) as db:
            after=db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        self.assertEqual(before,after)
    def test_empty_window_and_bounds(self):
        result=examine(self.db,self.conf,hours=1,now=self.now+timedelta(days=3))
        self.assertEqual(result["with_snapshots"],0)
        with self.assertRaises(ValueError):
            examine(self.db,self.conf,hours=0)
if __name__=="__main__":
    unittest.main()
