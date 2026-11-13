"""Behavioural regressions: source time, money/call caps, recovery, install and replay."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from io import BytesIO
from types import SimpleNamespace
import market_dashboard
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import ai_sandbox_agent as ai
import build_release
from decision_providers import OpenAIProvider, FixtureProvider
import install_project
import market_quality as quality
import market_recorder as recorder
import model_budget as budget
import offline_replay
import openai_quality_trader as trader
import risk_policy
import runtime_lock
import sandbox_autotrader as t
import sandbox_supervisor as supervisor
import system_status
import trade_ledger as ledger
import v19_guard as guard
import v19_health as health

SOURCE = Path(__file__).resolve().parents[1]
HOLD = {"action":"HOLD","symbol":"NONE","lots":0,"confidence":0.9,
        "reason":"Недостаточно подтверждений для покупки","risks":"Недостаточная история","horizon_minutes":30}


def quote(number):
    value = Decimal(str(number))
    return {"units":str(int(value)),"nano":int((value-int(value))*1000000000)}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for obj, name, path in ((ledger,"DB_PATH","ledger.sqlite3"), (budget,"CONFIG_PATH","budget.json"),
                                (guard,"BLOCK_PATH","block.json"), (budget,"LEGACY_STATE_PATH","old-state.json"), (runtime_lock,"LOCK_PATH","engine.lock"),
                                (t,"STATE_FILE","state.json"), (t,"LOG_FILE","orders.jsonl"),
                                (ai,"LOG_FILE","ai.jsonl"), (ai,"STATE_DIR","logs")):
            self.stack.enter_context(patch.object(obj, name, self.root/path))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def market(self, points=((0,"99.9","100",100),), symbol="SBER", isin="RU0009029540", kind="share", lot=1):
        path = self.root/"market.sqlite3"
        inst = {"uid":"UID","ticker":symbol,"isin":isin,"board":"TQBR" if kind=="share" else "TQCB",
                "name":"test","kind":kind,"currency":"rub","lot":lot}
        with recorder.connect(path) as db:
            for minute, bid, ask, volume in points:
                when = self.now+timedelta(minutes=minute)
                book = {"instrumentUid":"UID","orderbookTs":when.isoformat(),
                        "bids":[{"price":quote(bid),"quantity":volume}],
                        "asks":[{"price":quote(ask),"quantity":volume}]}
                value = recorder.normalize_payload(inst,{},book,10,when)
                recorder.store_snapshot(db,inst,value,when)
        db.close()
        return path


class QualityTests(Isolated):
    def test_fresh_storage_does_not_refresh_eighteen_minute_old_book(self):
        collected = self.now-timedelta(minutes=9)
        book = self.now-timedelta(minutes=18)
        self.assertEqual(quality.freshness(collected.isoformat(),book.isoformat(),self.now), (False,"old_orderbook"))

    def test_future_and_missing_source_time_fail_closed(self):
        for stamp in (None,(self.now+timedelta(seconds=1)).isoformat(),"2026-11-13T05:00:00"):
            self.assertFalse(quality.freshness(self.now.isoformat(),stamp,self.now)[0])

    def test_health_resolves_real_ticker_from_isin(self):
        path = self.market(symbol="OMEGA",isin="RU000A1092T7",kind="bond")
        cfg = self.root/"market.json"
        cfg.write_text(json.dumps({"instruments":[{"symbol":"RU000A1092T7","board":"TQCB"}]}))
        self.assertEqual(health.report(path,cfg,now=self.now)["healthy"],1)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE snapshots SET book_time_utc=?",((self.now-timedelta(minutes=18)).isoformat(),))
        result = health.report(path,cfg,now=self.now)
        self.assertEqual(result["healthy"],0)
        self.assertEqual(result["instruments"][0]["reason"],"old_orderbook")

    def test_ambiguous_uid_is_never_selected(self):
        path = self.market()
        with recorder.connect(path) as db:
            db.execute("INSERT INTO instruments SELECT 'UID2',ticker,isin,board,name,kind,currency,lot,first_seen_utc,last_seen_utc FROM instruments")
            with self.assertRaisesRegex(ValueError,"ambiguous"):
                quality.resolve_uid(db,"SBER","TQBR")


class BudgetTests(Isolated):
    def cfg(self, **values):
        cfg = {**budget.DEFAULT,"min_interval_minutes":0,**values}
        budget.CONFIG_PATH.write_text(json.dumps(cfg))
        return cfg

    def test_twenty_fourth_dry_run_is_counted_and_twenty_fifth_is_blocked(self):
        cfg = self.cfg()
        for _ in range(23):budget.reserve("gpt-test","run",100,cfg=cfg)
        with patch.object(ai,"_request_openai",return_value=(HOLD,{"input_tokens":100,"output_tokens":20})) as request:
            OpenAIProvider().decide("FAKE","gpt-test",{"_mode":"dry-run"})
            with self.assertRaisesRegex(budget.BudgetBlocked,"daily_call"):
                OpenAIProvider().decide("FAKE","gpt-test",{"_mode":"dry-run"})
        self.assertEqual(request.call_count,1)
        self.assertEqual(budget.status()["calls"],24)
        with ledger.connection(readonly=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM model_calls WHERE mode='dry-run'").fetchone()[0],1)

    def test_failed_generation_reserves_budget_and_opens_quota_circuit(self):
        self.cfg()
        with patch.object(ai,"_request_openai",side_effect=ai.c.APIError("HTTP 429 code=credit_balance_exhausted")) as request:
            with self.assertRaises(ai.c.APIError):OpenAIProvider().decide("FAKE","gpt-test",{})
            with self.assertRaises(budget.BudgetBlocked):OpenAIProvider().decide("FAKE","gpt-test",{})
        self.assertEqual(request.call_count,1)
        self.assertEqual(budget.status()["calls"],1)
        self.assertTrue(guard.is_blocked())

    def test_atomic_limit_under_parallel_manual_requests(self):
        cfg = self.cfg(max_calls_per_day=1)
        with ledger.connection():pass
        def reserve():
            try:
                budget.reserve("gpt-test","dry-run",100,cfg=cfg)
                return "reserved"
            except budget.BudgetBlocked:return "blocked"
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _:reserve(),range(2)))
        self.assertCountEqual(result,["reserved","blocked"])

    def test_monetary_cap_requires_matching_rates_and_retains_failed_reservation(self):
        cfg = self.cfg(max_cost_usd_per_day="0.01")
        with self.assertRaisesRegex(budget.BudgetBlocked,"rates_unverified"):
            budget.reserve("gpt-test","run",100,cfg=cfg)
        cfg.update(priced_model="gpt-test",input_usd_per_million="1",output_usd_per_million="4")
        call = budget.reserve("gpt-test","run",100,cfg=cfg)
        budget.complete(call,"gpt-test",error_code="TimeoutError",cfg=cfg)
        with self.assertRaisesRegex(budget.BudgetBlocked,"daily_cost"):
            budget.reserve("gpt-test","run",100,cfg=cfg)

    def test_upgrade_imports_already_consumed_daily_allowance(self):
        cfg = self.cfg()
        day = self.now.astimezone(budget.TZ).date().isoformat()
        budget.LEGACY_STATE_PATH.write_text(json.dumps({"ai_day":day,"ai_calls_today":24}))
        self.assertEqual(budget.status()["remaining_calls"],0)
        with self.assertRaisesRegex(budget.BudgetBlocked,"daily_call"):
            budget.reserve("gpt-test","run",100,now=self.now,cfg=cfg)
        self.assertEqual(budget.status()["calls"],24)

    def test_interval_survives_day_boundary(self):
        cfg = self.cfg(min_interval_minutes=30)
        before = datetime(2026,11,13,20,59,tzinfo=timezone.utc)
        budget.reserve("gpt-test","run",100,now=before,cfg=cfg)
        with self.assertRaisesRegex(budget.BudgetBlocked,"interval"):
            budget.reserve("gpt-test","run",100,now=before+timedelta(minutes=2),cfg=cfg)


class FakeSandbox(t.s.SandboxAPI):
    def __init__(self, ask="100", timeout=False):
        self.base = t.s.SANDBOX_BASE
        self.ask, self.timeout, self.calls = ask, timeout, []

    def call(self, method, body=None, **options):
        self.calls.append((method,body,options))
        if method == "instrument":
            return {"instrument":{"ticker":"SBER","classCode":"TQBR","uid":"UID","lot":10,
                                  "apiTradeAvailableFlag":True,"instrumentType":"share","currency":"rub"}}
        if method == "book":
            return {"instrumentUid":"UID","orderbookTs":datetime.now(timezone.utc).isoformat(),
                    "bids":[{"price":quote(Decimal(self.ask)-Decimal(".1")),"quantity":100}],
                    "asks":[{"price":quote(self.ask),"quantity":100}]}
        if method == "portfolio":return {"totalAmountPortfolio":quote(100000),"positions":[]}
        if method == "positions":return {"money":[{"currency":"rub",**quote(100000)}]}
        if method == "trader_orders":return {"orders":[]}
        if method == "trader_status":
            return {"instrumentUid":"UID","tradingStatus":"SECURITY_TRADING_STATUS_NORMAL_TRADING",
                    "apiTradeAvailableFlag":True,"marketOrderAvailableFlag":True}
        if method == "trader_post" and self.timeout:raise t.c.APIError("Network timeout")
        if method in ("trader_post","trader_orderstate"):
            return {"orderId":"EXCHANGE_ID","executionReportStatus":"EXECUTION_REPORT_STATUS_FILL",
                    "lotsExecuted":9,"executedOrderPrice":quote(self.ask),
                    "executedCommission":{"currency":"rub",**quote("4.5")},
                    "stages":[{"tradeId":"TRADE1","quantity":9,"price":quote(self.ask)}]}
        raise AssertionError("Unexpected broker method: "+method)


class ExecutionTests(Isolated):
    def state(self):return t.initialize_day({},self.now,Decimal(100000))
    def spec(self):return {"ticker":"SBER","uid":"UID","lot":10}

    def test_order_has_price_limit_and_separate_ids_with_idempotent_fills(self):
        api, state = FakeSandbox(), self.state()
        t.post_order(api,"PRIVATE_ACCOUNT",self.spec(),"BUY",9,Decimal(99),"test",state,self.now)
        body = next(body for method,body,_ in api.calls if method == "trader_post")
        self.assertEqual(body["orderType"],"ORDER_TYPE_LIMIT")
        self.assertEqual(body["price"],quote(100))
        self.assertEqual(state["pending"]["client_id"],body["orderId"])
        self.assertEqual(state["pending"]["broker_id"],"EXCHANGE_ID")
        self.assertFalse(t.reconcile_pending(api,"PRIVATE_ACCOUNT",state,self.now))
        self.assertEqual(ledger.summary()["cycles"][0]["status"],"filled")
        with ledger.connection(readonly=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM fills").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT commission_rub FROM orders").fetchone()[0],"4.5")
        self.assertNotIn("PRIVATE_ACCOUNT",ledger.DB_PATH.read_bytes().decode("latin1"))

    def test_live_ask_price_cannot_bypass_ten_percent_cap(self):
        api = FakeSandbox(ask="101")
        with self.assertRaisesRegex(t.c.APIError,"cap"):
            t.post_order(api,"PRIVATE_ACCOUNT",self.spec(),"BUY",10,Decimal(99),"test",self.state(),self.now)
        self.assertFalse(any(method == "trader_post" for method,_,_ in api.calls))

    def test_timeout_recovers_using_request_id_without_duplicate_submission(self):
        api, state = FakeSandbox(timeout=True), self.state()
        with self.assertRaises(t.c.APIError):
            t.post_order(api,"PRIVATE_ACCOUNT",self.spec(),"BUY",9,Decimal(99),"test",state,self.now)
        client_id = state["pending"]["client_id"]
        self.assertIsNone(state["pending"]["broker_id"])
        self.assertFalse(t.reconcile_pending(api,"PRIVATE_ACCOUNT",state,self.now))
        request = next(body for method,body,_ in api.calls if method == "trader_orderstate")
        self.assertEqual(request["orderId"],client_id)
        self.assertEqual(request["orderIdType"],"ORDER_ID_TYPE_REQUEST")
        self.assertEqual(sum(method == "trader_post" for method,_,_ in api.calls),1)

    def test_cooldown_is_preserved_across_moscow_midnight(self):
        now=datetime(2026,11,13,21,1,tzinfo=timezone.utc)
        previous=(now-timedelta(minutes=2)).isoformat()
        state=t.initialize_day({"day":"2026-11-13","last_order_utc":previous},now,Decimal(100000))
        self.assertEqual(state["last_order_utc"],previous)
        self.assertTrue(t.order_recent(state,now,20))

    def test_cloud_block_does_not_prevent_pending_reconciliation(self):
        guard.block_quota()
        with patch.object(trader,"config",return_value={"watchlist":["SBER"]}), \
             patch.object(t,"config",return_value={"account_ref":"tb_test"}), \
             patch.object(t.s,"SandboxAPI",return_value=FakeSandbox()), \
             patch.object(t.c,"token_read",return_value="FAKE"), \
             patch.object(t,"get_account",return_value="PRIVATE_ACCOUNT"), \
             patch.object(t,"reconcile_pending",return_value=False) as reconcile, \
             patch.object(ai,"read_key",side_effect=AssertionError("Cloud key must not be read")), \
             patch.object(ai,"ask_openai") as model:
            self.assertEqual(trader.one_cycle(),0)
        reconcile.assert_called_once()
        model.assert_not_called()

    def test_observer_works_with_disabled_trader_and_never_posts_or_cancels(self):
        state = self.state()
        t.atomic_save(t.STATE_FILE,state)
        cfg = self.root/"trader.json"
        cfg.write_text(json.dumps({"enabled":False,"account_ref":"tb_test"}))
        api = FakeSandbox()
        guard.block_quota()
        with patch.object(t,"CFG_FILE",cfg), patch.object(supervisor,"CONFIG_PATH",self.root/"observe.json"), \
             patch.object(t.s,"SandboxAPI",return_value=api), patch.object(t.c,"token_read",return_value="FAKE"), \
             patch.object(t,"get_account",return_value="PRIVATE_ACCOUNT"), \
             patch.object(t,"find_share",return_value=self.spec()):
            self.assertEqual(supervisor.run(),0)
        self.assertFalse(any(method in ("trader_post","trader_cancel") for method,_,_ in api.calls))
        self.assertEqual(ledger.summary()["portfolio"]["equity_rub"],"100000")


class InstallAndReplayTests(Isolated):
    def test_package_is_reproducible_and_installs_every_dependency(self):
        first, second = self.root/"first.zip", self.root/"second.zip"
        build_release.build(SOURCE,first,"a"*40)
        build_release.build(SOURCE,second,"a"*40)
        self.assertEqual(first.read_bytes(),second.read_bytes())
        with self.assertRaises(FileExistsError):build_release.build(SOURCE,first,"a"*40)
        with zipfile.ZipFile(first) as archive:archive.extractall(self.root/"unpacked")
        source = self.root/"unpacked/tinvest_ubuntu_collector"
        destination = self.root/"installed"
        destination.mkdir()
        (destination/"custom.txt").write_text("preserve user file")
        result = install_project.install_tree(source,destination)
        self.assertEqual((destination/"custom.txt").read_text(),"preserve user file")
        self.assertTrue((destination/"v19_guard.py").is_file())
        self.assertTrue((Path(result["backup"])/"code/custom.txt").is_file())
        (source/"v19_guard.py").write_text("corrupted")
        with self.assertRaisesRegex(ValueError,"checksum"):
            install_project.install_tree(source,destination)

    def test_install_rolls_back_when_destination_import_fails(self):
        destination = self.root/"installed"
        destination.mkdir()
        (destination/"original.txt").write_text("original")
        with patch.object(install_project,"verify_installed",side_effect=[None,RuntimeError("failed")]):
            with self.assertRaises(RuntimeError):install_project.install_tree(SOURCE,destination)
        self.assertEqual((destination/"original.txt").read_text(),"original")
        self.assertFalse((destination/"openai_quality_trader.py").exists())

    def test_service_configuration_preserves_proxy_and_overrides(self):
        conf, units = self.root/"config", self.root/"units"
        conf.mkdir();units.mkdir()
        proxy = conf/"proxychains.conf"
        proxy.write_text("strict_chain\n[ProxyList]\nsocks5 192.168.202.50 1080\n")
        original = proxy.read_bytes()
        existing = units/"tinvest-sandbox-openai.service"
        existing.write_text("preserved existing unit")
        override = units/"tinvest-sandbox-openai.service.d/override.conf"
        override.parent.mkdir();override.write_text("preserved custom override")
        with patch.object(install_project.shutil,"which",return_value="/usr/bin/proxychains4"):
            install_project.configure_services(self.root/"installed",conf,units)
        self.assertEqual(proxy.read_bytes(),original)
        self.assertEqual(existing.read_text(),"preserved existing unit")
        self.assertEqual(override.read_text(),"preserved custom override")
        self.assertIn("proxychains4",(units/"tinvest-sandbox-supervisor.service").read_text())
        self.assertFalse((units/"timers.target.wants").exists())

    def test_consistent_sqlite_backup_precedes_install(self):
        market = self.market()
        before = hashlib.sha256(market.read_bytes()).hexdigest()
        result = install_project.install_tree(SOURCE,self.root/"installed",databases=(market,))
        backup = Path(result["backup"])/("0-"+market.name)
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(db.execute("SELECT count(*) FROM snapshots").fetchone()[0],1)
        self.assertEqual(hashlib.sha256(market.read_bytes()).hexdigest(),before)

    def test_http_status_endpoints_are_read_only_and_refuse_post(self):
        market = self.market()
        ledger.heartbeat("recorder","ok",now=self.now)
        class Wire:
            def __init__(self,request):self.reader=BytesIO(request);self.output=bytearray()
            def makefile(self,*args):return self.reader
            def sendall(self,data):self.output.extend(data)
        def request(method,url):
            wire=Wire((method+" "+url+" HTTP/1.0\r\nHost: localhost\r\n\r\n").encode())
            with patch.object(system_status,"CONFIG_DIR",self.root/"config"), \
                 patch.object(market_dashboard.LocalHandler,"log_message"):
                market_dashboard.LocalHandler(wire,("127.0.0.1",1),SimpleNamespace(database_path=market))
            return bytes(wire.output)
        before=hashlib.sha256(ledger.DB_PATH.read_bytes()).hexdigest()
        for url in ("/api/system","/api/trading","/api/market?hours=24","/api/book?symbol=SBER&board=TQBR"):
            response=request("GET",url)
            self.assertIn(b"200 OK",response.split(b"\r\n",1)[0])
            json.loads(response.split(b"\r\n\r\n",1)[1])
            self.assertIn(b"Content-Security-Policy:",response)
        self.assertIn(b"405 Method Not Allowed",request("POST","/api/trading"))
        self.assertEqual(hashlib.sha256(ledger.DB_PATH.read_bytes()).hexdigest(),before)

    def test_manual_concurrent_engine_is_rejected(self):
        with runtime_lock.engine_lock():
            with self.assertRaises(RuntimeError):
                with runtime_lock.engine_lock():pass

    def test_replay_does_not_fill_at_signal_time_or_use_future_price(self):
        path = self.market(((0,"99.9","100",100),(5,"199.9","200",100)))
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = offline_replay.replay(path,None,hours=1,strategy="buy-hold",now=self.now+timedelta(minutes=5))
        self.assertEqual(result["fills"],[])
        self.assertEqual(result["pending_lots_at_end"],99)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
        self.assertEqual(result["network_requests"],0)

    def test_replay_models_partial_depth_and_commission_after_latency(self):
        path = self.market(((0,"99.9","100",100),(5,"99.9","100",2)))
        result = offline_replay.replay(path,None,hours=1,strategy="buy-hold",now=self.now+timedelta(minutes=5))
        self.assertEqual(len(result["fills"]),1)
        fill = result["fills"][0]
        self.assertGreater(fill["utc"],fill["signal_utc"])
        self.assertEqual(fill["lots"],2)
        self.assertEqual(Decimal(fill["fee_rub"]),Decimal("0.1"))
        self.assertEqual(result["pending_lots_at_end"],97)

    def test_recorded_replay_reports_absent_decisions_and_unknown_lot(self):
        path = self.market(lot=None)
        result = offline_replay.replay(path,None,hours=1,strategy="recorded",now=self.now)
        self.assertEqual(result["status"],"no_eligible_data")
        self.assertTrue(result["excluded"])

    def test_offline_fixture_never_accesses_network(self):
        path = self.root/"hold.json"
        path.write_text(json.dumps(HOLD))
        with patch("socket.socket.connect",side_effect=AssertionError("Network forbidden")):
            self.assertEqual(FixtureProvider(path).decide({}),HOLD)

    def test_status_does_not_read_or_expose_credential_contents(self):
        conf = self.root/"config"
        conf.mkdir()
        (conf/"openai-api-key").write_text("sk-PRIVATESECRET123456")
        ledger.heartbeat("supervisor","ok",now=self.now)
        with patch.object(system_status,"CONFIG_DIR",conf), \
             patch("socket.socket.connect",side_effect=AssertionError("Network forbidden")):
            output = json.dumps(system_status.report(self.root/"absent.db",now=self.now))
        self.assertNotIn("PRIVATESECRET",output)
        self.assertIn('"openai": true',output)


if __name__ == "__main__":
    unittest.main()
