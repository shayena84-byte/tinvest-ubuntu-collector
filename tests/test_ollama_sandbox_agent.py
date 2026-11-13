"""Offline tests for the local AI recommendation path and risk-quality gates."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ollama_sandbox_agent as a
import market_recorder as m


class TestLocalSandboxAgent(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        for obj,name,relative in ((a.ai,"STATE_DIR","state"),(a.ai,"LOG_FILE","ai.jsonl")):
            isolated=patch.object(obj,name,Path(self.temp.name)/relative)
            isolated.start();self.addCleanup(isolated.stop)
        self.db=Path(self.temp.name)/"market.sqlite3"
        self.now=datetime(2026,11,12,11,55,45,tzinfo=timezone.utc)
        self.inst={"uid":"uid-sber","ticker":"SBER","isin":"RU0009029540",
                   "board":"TQBR","name":"Sberbank","kind":"share","currency":"rub"}
        self.last={"lastPrices":[{"instrumentUid":"uid-sber",
                  "time":"2026-11-12T11:54:00Z",
                  "price":{"units":"298","nano":100000000}}]}
    def tearDown(self):
        self.temp.cleanup()
    def snapshot(self,bid=298.09,ask=298.11,stamp="2026-11-12T11:55:00Z"):
        def q(n):
            from decimal import Decimal
            v=Decimal(str(n));n=int(v)
            return {"units":str(n),"nano":int((v-n)*Decimal("1000000000"))}
        book={"instrumentUid":"uid-sber","orderbookTs":stamp,
              "bids":[{"price":q(bid),"quantity":10}],
              "asks":[{"price":q(ask),"quantity":10}]}
        row=m.normalize_payload(self.inst,self.last,book,10,self.now)
        with m.connect(self.db) as db:
            m.store_snapshot(db,self.inst,row,self.now)
        return row
    def context(self):
        return {"equity_rub":"10000","cash_rub":"10000",
                "invested_rub_estimate":"0",
                "tradable":[{"uid":"uid-sber","symbol":"SBER","price_rub":"298.10",
                             "owned_lots":0,"lot":10}]}
    def test_ollama_api_loopback_only(self):
        self.assertEqual(a.OLLAMA,"http://127.0.0.1:11434")
        with self.assertRaises(a.t.c.APIError):
            a.ollama_request("/api/delete",{})
    def test_ollama_json_schema_parser(self):
        decision={"action":"HOLD","symbol":"NONE","lots":0,
                  "confidence":.90,"reason":"Недостаточно статистически подтверждённых сигналов",
                  "risks":"Возможно упущение возможности роста цены","horizon_minutes":30}
        out=a.parse_ollama({"message":{"content":json.dumps(decision,ensure_ascii=False)}})
        self.assertEqual(out,decision)
        with self.assertRaises(Exception):
            a.parse_ollama({"message":{"content":"not json"}})
    def test_good_book_allows_only_approved(self):
        self.snapshot()
        ctx=a.gate_by_quality(self.context(),a.DEFAULT,self.now,self.db)
        self.assertEqual(len(ctx["tradable"]),1)
        self.assertAlmostEqual(ctx["tradable"][0]["book_spread_pct"],.0067,places=3)
    def test_crossed_book_rejected(self):
        self.snapshot(bid=298.21,ask=298.11)
        with self.assertRaises(a.t.c.APIError):
            a.gate_by_quality(self.context(),a.DEFAULT,self.now,self.db)
    def test_stale_book_rejected(self):
        self.snapshot(stamp="2026-11-12T11:10:00Z")
        with self.assertRaises(a.t.c.APIError):
            a.gate_by_quality(self.context(),a.DEFAULT,self.now,self.db)
    def test_spread_too_wide_rejected(self):
        self.snapshot(bid=290,ask=298)
        with self.assertRaises(a.t.c.APIError):
            a.gate_by_quality(self.context(),a.DEFAULT,self.now,self.db)
    def test_quote_mismatch_rejected(self):
        self.snapshot()
        ctx=self.context()
        ctx["tradable"][0]["price_rub"]="350"
        with self.assertRaises(a.t.c.APIError):
            a.gate_by_quality(ctx,a.DEFAULT,self.now,self.db)
    def test_local_provider_uses_common_sandbox_engine(self):
        with patch("openai_quality_trader.one_cycle",return_value=0) as engine:
            self.assertEqual(a.run(dry_run=True),0)
            engine.assert_called_once_with(dry_run=True,provider="ollama")

if __name__=="__main__":unittest.main()
