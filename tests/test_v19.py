"""Offline regression tests for v1.9.0 quota circuit and SQLite monitor."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import v19_guard as guard
import v19_health as health

class TestGuard(unittest.TestCase):
    def test_quota_block_and_explicit_reset(self):
        with tempfile.TemporaryDirectory() as d, patch.object(guard,"BLOCK_PATH",Path(d)/"block.json"):
            self.assertFalse(guard.is_blocked())
            self.assertTrue(guard.is_quota_error("HTTP 429 [type=insufficient_quota]"))
            self.assertTrue(guard.is_quota_error("code=credit_balance_exhausted"))
            self.assertFalse(guard.is_quota_error("rate_limit_exceeded"))
            guard.block_quota()
            self.assertTrue(guard.is_blocked())
            self.assertEqual(guard.block_status()["reason"],"insufficient_quota")
            guard.reset_quota()
            self.assertFalse(guard.is_blocked())
    def test_invalid_breaker_fail_closed(self):
        with tempfile.TemporaryDirectory() as d, patch.object(guard,"BLOCK_PATH",Path(d)/"block.json"):
            guard.BLOCK_PATH.write_text("{",encoding="utf-8")
            self.assertTrue(guard.is_blocked())

class TestHealth(unittest.TestCase):
    def test_missing_and_fresh_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/"market.db"
            config=Path(d)/"cfg.json"
            config.write_text(json.dumps({"instruments":[
                {"symbol":"SBER","board":"TQBR"},
                {"symbol":"VTBR","board":"TQBR"}]}),encoding="utf-8")
            now=datetime(2026,11,12,18,30,tzinfo=timezone.utc)
            with sqlite3.connect(db) as conn:
                conn.executescript("""CREATE TABLE instruments(uid TEXT,ticker TEXT,board TEXT);
                 CREATE TABLE snapshots(uid TEXT,collected_utc TEXT,quality_status TEXT,
                   quality_reason TEXT,spread_pct REAL,book_stale_seconds INTEGER,book_time_utc TEXT);""")
                conn.execute("INSERT INTO instruments VALUES ('a','SBER','TQBR')")
                conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                             ("a",now.isoformat(),"valid","ok",0.01,0,now.isoformat()))
            result=health.report(db,config,now=now)
            self.assertEqual(result["healthy"],1)
            self.assertEqual(result["configured"],2)
            self.assertEqual(result["instruments"][1]["reason"],"no_snapshot")

if __name__=="__main__":
    unittest.main()
