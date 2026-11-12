"""Read-only local market dashboard and data-quality integration tests."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import market_dashboard as dashboard
import market_recorder as m


class TestMarketDashboard(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.db=Path(self.temp.name)/"local.sqlite3"
        self.now=datetime(2026,11,12,16,0,tzinfo=timezone.utc)
        self.inst={"uid":"uid-sber","ticker":"SBER","isin":"RU0009029540","board":"TQBR",
                   "name":"Сбербанк","kind":"share","currency":"rub"}
        self.last={"lastPrices":[{"instrumentUid":"uid-sber","price":{"units":"299","nano":780000000},
                   "time":"2026-11-12T15:59:00Z"}]}

    def tearDown(self):
        self.temp.cleanup()

    def book(self,bid,ask,stamp="2026-11-12T15:59:00Z"):
        def money(price):
            from decimal import Decimal
            p=Decimal(str(price))
            i=int(p)
            n=int((p-i)*Decimal("1000000000"))
            return {"units":str(i),"nano":n}
        return {"instrumentUid":"uid-sber","orderbookTs":stamp,
                "bids":[{"price":money(bid),"quantity":100}],
                "asks":[{"price":money(ask),"quantity":80}]}

    def record(self,bid,ask,dt,stamp="2026-11-12T15:59:00Z"):
        payload=m.normalize_payload(self.inst,self.last,self.book(bid,ask,stamp),10,dt)
        with m.connect(self.db) as db:
            m.store_snapshot(db,self.inst,payload,dt)
        return payload

    def test_quality_only_valid_in_spread_summary(self):
        self.record("299.60","299.61",self.now-timedelta(minutes=5))
        self.record("299.78","299.63",self.now)
        result=dashboard.dashboard_data(self.db,hours=24,now=self.now+timedelta(seconds=10))
        self.assertEqual(len(result["summary"]),1)
        entry=result["summary"][0]
        self.assertEqual(entry["measurements"],2)
        self.assertEqual(entry["statuses"]["valid"],1)
        self.assertEqual(entry["statuses"]["invalid"],1)
        self.assertAlmostEqual(entry["median_valid_spread_pct"],0.0033,places=3)
        self.assertEqual(entry["last"]["quality_status"],"invalid")
        self.assertEqual(len(result["anomalies"]),1)
        self.assertEqual(result["anomalies"][0]["reason"],"crossed_or_locked_book")

    def test_unverified_excluded_from_analytics(self):
        self.record("299.60","299.61",self.now,stamp=None)
        # 'book' helper emits a null TS, so the snapshot remains unverified.
        result=dashboard.dashboard_data(self.db,hours=24,now=self.now)
        entry=result["summary"][0]
        self.assertEqual(entry["statuses"]["unverified"],1)
        self.assertIsNone(entry["median_valid_spread_pct"])
        self.assertIsNone(entry["validated_price_change_pct"])

    def test_database_ro_mode(self):
        self.record("299.60","299.61",self.now)
        with dashboard.db_readonly(self.db) as conn:
            with self.assertRaises(Exception):
                conn.execute("DELETE FROM snapshots")

    def test_hours_validation(self):
        self.record("299.60","299.61",self.now)
        with self.assertRaises(ValueError):
            dashboard.dashboard_data(self.db,hours=999,now=self.now)

    def test_json_contains_no_credentials_or_account_ids(self):
        self.record("299.60","299.61",self.now)
        result=dashboard.dashboard_data(self.db,hours=24,now=self.now)
        data=json.dumps(result,ensure_ascii=False)
        self.assertIn("SBER",data)
        self.assertNotIn("sandbox-token",data)
        self.assertNotIn("accountId",data)
        self.assertNotIn("Bearer",data)

    def test_html_and_js_exist_and_are_dependency_free(self):
        html=(Path(__file__).resolve().parents[1]/"dashboard.html").read_text("utf-8")
        js=(Path(__file__).resolve().parents[1]/"dashboard.js").read_text("utf-8")
        self.assertIn('src="/dashboard.js"',html)
        self.assertIn("/api/market?",js)
        self.assertNotIn("https://cdn",html)
        self.assertNotIn("https://cdn",js)
        self.assertNotIn("POST",js)

if __name__=="__main__":
    unittest.main()
