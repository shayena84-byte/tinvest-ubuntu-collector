"""Offline regression tests for the SQLite market historian, never calling real APIs."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import market_recorder as m


class TestMarketRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.dbpath=Path(self.tmp.name)/"snapshots.sqlite3"
        self.now=datetime(2026,11,12,11,56,30,tzinfo=timezone.utc)
        self.inst={"uid":"correct-bond-uid","ticker":"RU000A1092T7","isin":"RU000A1092T7",
                   "board":"TQCB","name":"Омега БО-01","kind":"bond","currency":"rub"}
        self.book={"instrumentUid":"correct-bond-uid",
                   "time":"2026-11-12T11:55:00Z",
                   "bids":[{"price":{"units":"45","nano":210000000},"quantity":7},
                           {"price":{"units":"45","nano":200000000},"quantity":10}],
                   "asks":[{"price":{"units":"45","nano":940000000},"quantity":76},
                           {"price":{"units":"45","nano":950000000},"quantity":12}]}
        self.last={"lastPrices":[{"instrumentUid":"correct-bond-uid",
                   "price":{"units":"45","nano":860000000},
                   "time":"2026-11-12T10:56:53Z"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_slot_boundaries(self):
        self.assertEqual(m.slot_5min(self.now),"2026-11-12T11:55:00+00:00")
        self.assertEqual(m.slot_5min(self.now+timedelta(minutes=4)),"2026-11-12T12:00:00+00:00")

    def test_normalization_and_spread_omega(self):
        out=m.normalize_payload(self.inst,self.last,self.book,10,self.now)
        self.assertEqual(out["last_price"],"45.86")
        self.assertEqual(out["best_bid"],"45.21")
        self.assertEqual(out["best_ask"],"45.94")
        self.assertEqual(out["bids_total"],17)
        self.assertEqual(out["asks_total"],88)
        self.assertAlmostEqual(out["spread_pct"],1.602,places=2)
        self.assertEqual(out["book_stale_seconds"],90)
        self.assertEqual(out["last_stale_seconds"],3577)

    def test_orderbook_levels_are_sorted_and_truncated(self):
        rows=[{"price":{"units":"40","nano":0},"quantity":1},
              {"price":{"units":"41","nano":0},"quantity":3},
              {"price":{"units":"39","nano":0},"quantity":5}]
        bids=m.clean_levels(rows,True,2)
        asks=m.clean_levels(rows,False,2)
        self.assertEqual(bids,[(Decimal("41"),3),(Decimal("40"),1)])
        self.assertEqual(asks,[(Decimal("39"),5),(Decimal("40"),1)])

    def test_missing_book_timestamp_is_not_fresh(self):
        book={**self.book, "time":None, "bids":[], "asks":[]}
        out=m.normalize_payload(self.inst,self.last,book,10,self.now)
        self.assertIsNone(out["book_stale_seconds"])
        self.assertIsNone(out["spread_pct"])
        self.assertEqual(out["bids_total"],0)

    def test_fail_closed_mismatched_uid(self):
        with self.assertRaises(m.RecorderError):
            m.normalize_payload(self.inst,self.last,{**self.book,"instrumentUid":"other"},10,self.now)
        with self.assertRaises(m.RecorderError):
            m.normalize_payload(self.inst,{"lastPrices":[{"instrumentUid":"other","price":{"units":"10"}}]},self.book,10,self.now)

    def test_sqlite_idempotent_slot_and_levels(self):
        db=m.connect(self.dbpath)
        try:
            out=m.normalize_payload(self.inst,self.last,self.book,10,self.now)
            first=m.store_snapshot(db,self.inst,out,self.now)
            again=m.store_snapshot(db,self.inst,out,self.now+timedelta(minutes=1))
            self.assertEqual(first,again)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM book_levels").fetchone()[0],4)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],m.SCHEMA_VERSION)
            self.assertEqual(self.dbpath.stat().st_mode & 0o777,0o600)
            levels=db.execute("SELECT side,level,price,lots FROM book_levels WHERE snapshot_id=? "
                              "ORDER BY side,level",(first,)).fetchall()
            self.assertEqual(len(levels),4)
        finally:
            db.close()

    def test_rest_orderbook_ts_is_read_and_validated(self):
        book = {**self.book, "orderbookTs":"2026-11-12T11:55:00Z"}
        book.pop("time", None)
        out=m.normalize_payload(self.inst,self.last,book,10,self.now)
        self.assertEqual(out["book_stale_seconds"],90)
        self.assertEqual(out["quality_status"],"valid")
        self.assertEqual(out["quality_reason"],"ok")

    def test_crossed_book_is_invalid_and_anomaly_persisted(self):
        book={**self.book,"orderbookTs":"2026-11-12T11:55:00Z",
              "bids":[{"price":{"units":"46","nano":0},"quantity":9}]}
        out=m.normalize_payload(self.inst,self.last,book,10,self.now)
        self.assertEqual(out["quality_status"],"invalid")
        self.assertEqual(out["quality_reason"],"crossed_or_locked_book")
        db=m.connect(self.dbpath)
        try:
            sid=m.store_snapshot(db,self.inst,out,self.now)
            item=db.execute("SELECT status,reason FROM anomalies WHERE snapshot_id=?",
                            (sid,)).fetchone()
            self.assertEqual(tuple(item),("invalid","crossed_or_locked_book"))
            self.assertEqual(db.execute("SELECT quality_status FROM snapshots").fetchone()[0],"invalid")
        finally: db.close()

    def test_missing_book_time_is_unverified(self):
        no_time={k:v for k,v in self.book.items() if k!="time"}
        out=m.normalize_payload(self.inst,self.last,no_time,10,self.now)
        self.assertEqual(out["quality_status"],"unverified")
        self.assertEqual(out["quality_reason"],"orderbook_timestamp_missing")

    def test_stale_and_future_orderbook(self):
        for ts,quality in [
            ("2026-11-12T11:30:00Z","stale"),
            ("2026-11-12T12:10:00Z","unverified")
        ]:
            book={**self.book,"orderbookTs":ts}
            out=m.normalize_payload(self.inst,self.last,book,10,self.now)
            self.assertEqual(out["quality_status"],quality)

    def test_legacy_v1_migration_preserves_snapshots(self):
        # Simulate original v1 DB (before new quality columns).
        import market_recorder as rec
        db=sqlite3.connect(self.dbpath)
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
           CREATE TABLE instruments(uid TEXT PRIMARY KEY, ticker TEXT NOT NULL,
              isin TEXT NOT NULL, board TEXT NOT NULL, name TEXT NOT NULL,
              kind TEXT NOT NULL, currency TEXT, first_seen_utc TEXT NOT NULL,
              last_seen_utc TEXT NOT NULL, UNIQUE(uid,board));
           CREATE TABLE snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,
              uid TEXT NOT NULL REFERENCES instruments(uid),
              slot_utc TEXT NOT NULL, collected_utc TEXT NOT NULL,
              last_price TEXT,last_trade_utc TEXT,book_time_utc TEXT,best_bid TEXT,
              best_ask TEXT, spread_pct REAL, bid_top_lots INTEGER DEFAULT 0,
              ask_top_lots INTEGER DEFAULT 0,bids_total INTEGER DEFAULT 0,
              asks_total INTEGER DEFAULT 0, book_stale_seconds INTEGER,
              last_stale_seconds INTEGER, UNIQUE(uid,slot_utc));
           CREATE TABLE book_levels(snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
              side TEXT NOT NULL, level INTEGER NOT NULL, price TEXT NOT NULL,
              lots INTEGER NOT NULL,PRIMARY KEY(snapshot_id,side,level));
           CREATE TABLE fetch_errors(symbol TEXT NOT NULL,board TEXT NOT NULL,
              slot_utc TEXT NOT NULL,collected_utc TEXT NOT NULL,
              error_type TEXT NOT NULL,safe_message TEXT NOT NULL,
              PRIMARY KEY(symbol,board,slot_utc));
           PRAGMA user_version=1;
        """)
        dt="2026-11-12T11:55:00+00:00"
        with db:
            db.execute("INSERT INTO instruments VALUES(?,?,?,?,?,?,?,?,?)",
                       ("uid","SBER","RU0009029540","TQBR","Sber","share","rub",dt,dt))
            db.execute("""INSERT INTO snapshots(uid,slot_utc,collected_utc,
               best_bid,best_ask) VALUES(?,?,?,?,?)""",
               ("uid",dt,dt,"299.78","299.63"))
        db.close()
        migrated=rec.connect(self.dbpath)
        try:
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0],m.SCHEMA_VERSION)
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],1)
            self.assertEqual(migrated.execute("SELECT quality_status FROM snapshots").fetchone()[0],"invalid")
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0],1)
        finally:
            migrated.close()

    def test_repeated_snapshot_replaces_old_anomaly(self):
        db=m.connect(self.dbpath)
        try:
            first=m.normalize_payload(self.inst,self.last,
                     {**self.book,"bids":[{"price":{"units":"46","nano":0},"quantity":1}]},
                     10,self.now)
            m.store_snapshot(db,self.inst,first,self.now)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0],1)
            second=m.normalize_payload(self.inst,self.last,self.book,10,self.now)
            m.store_snapshot(db,self.inst,second,self.now)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0],0)
        finally: db.close()

    def test_failed_collection_preserves_valid_snapshots(self):
        db=m.connect(self.dbpath)
        try:
            out=m.normalize_payload(self.inst,self.last,self.book,10,self.now)
            m.store_snapshot(db,self.inst,out,self.now)
            m.store_failure(db,{"symbol":"RU000A1092T7","board":"TQCB"},self.now,m.RecorderError("NETWORK"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fetch_errors").fetchone()[0],1)
            m.store_snapshot(db,self.inst,out,self.now)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fetch_errors").fetchone()[0],0)
        finally:
            db.close()

    def test_read_only_api_blocks_trading(self):
        class StubSandbox(m.sandbox_module.SandboxAPI):
            def __init__(self):
                self.base=m.sandbox_module.SANDBOX_BASE
            def call(self,method,payload):
                return {}
        read=m.ReadOnlySandboxMarket(StubSandbox())
        self.assertEqual(read.call("last",{}),{})
        for operation in ("trader_post","sandbox_payin","trader_cancel","accounts","portfolio"):
            with self.assertRaises(m.RecorderError):
                read.call(operation,{})

    def test_sandbox_only_client(self):
        class PretendProduction:
            base="https://invest-public-api.tbank.ru"
        with self.assertRaises(m.RecorderError):
            m.ReadOnlySandboxMarket(PretendProduction())

    def test_resolve_and_api_calls(self):
        class Stub:
            def __init__(self,outer): self.outer=outer; self.calls=[]
            def call(self,method,body):
                self.calls.append((method,body))
                if method=="find":
                    return {"instruments":[
                        {"ticker":"RU000A1092T7","isin":"RU000A1092T7",
                         "classCode":"PTOB","uid":"incorrect"},
                        {"ticker":"RU000A1092T7","isin":"RU000A1092T7",
                         "classCode":"TQCB","uid":"correct-bond-uid"}]}
                if method=="instrument":
                    return {"instrument":{"uid":body["id"],"ticker":"RU000A1092T7",
                                "isin":"RU000A1092T7","classCode":"TQCB",
                                "name":"Омега БО-01","instrumentType":"bond"}}
                if method=="last": return self.outer.last
                if method=="book": return self.outer.book
                raise ValueError(method)
        api=Stub(self)
        spec={"symbol":"RU000A1092T7","board":"TQCB","kind":"bond"}
        inst,row=m.read_one(api,spec,10,self.now)
        self.assertEqual(inst["uid"],"correct-bond-uid")
        self.assertEqual(row["last_price"],"45.86")
        self.assertEqual(api.calls[-1][0],"book")

    def test_record_one_failed_instrument_without_token_leak(self):
        class Bad:
            def call(self,method,body):
                raise m.c.APIError("Service unavailable")
        cfg={"depth":10,"instruments":[{"symbol":"SBER","board":"TQBR","kind":"share"}]}
        rc=m.collect(api=Bad(),db_path=self.dbpath,config=cfg,now=self.now)
        self.assertEqual(rc,1)
        db=m.connect(self.dbpath)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fetch_errors").fetchone()[0],1)
        finally:
            db.close()

    def test_config_validation(self):
        config={"depth":10,"instruments":[{"symbol":"SBER","board":"TQBR","kind":"share"}]}
        import json
        path=Path(self.tmp.name)/"config.json"
        path.write_text(json.dumps(config))
        with patch.object(m,"CONFIG_PATH",path):
            self.assertEqual(m.load_config(),config)
            path.write_text(json.dumps({"depth":100,"instruments":config["instruments"]}))
            with self.assertRaises(m.RecorderError):
                m.load_config()
            path.write_text(json.dumps({"depth":10,"instruments":
                  [{"symbol":"SBER","board":"TQBR","kind":"share"},
                   {"symbol":"SBER","board":"TQBR","kind":"share"}]}))
            with self.assertRaises(m.RecorderError):
                m.load_config()

if __name__=="__main__": unittest.main()
