"""Cloud OpenAI provider tests: no Ollama dependence and no Sandbox orders in dry-run."""
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openai_quality_trader as trader


class TestOpenAIQualityTrader(unittest.TestCase):
    def test_config_preserves_existing_model_with_strict_risk_ceiling(self):
        orig={
          "enabled":True,"model":"gpt-6.1-sol","watchlist":["SBER"],
          "minimum_confidence":0.65,"max_trade_fraction":0.42,
          "max_exposure_fraction":0.82,"max_daily_loss_fraction":0.08,
          "max_orders_per_day":8,"max_ai_calls_per_day":110,
          "minimum_cash_rub":850,"cooldown_minutes":20
        }
        with patch.object(trader.ai,"ai_config",return_value=orig):
            cfg=trader.config()
        self.assertEqual(cfg["model"],"gpt-6.1-sol")
        self.assertEqual(cfg["max_trade_fraction"],0.10)
        self.assertEqual(cfg["max_exposure_fraction"],0.50)
        self.assertEqual(cfg["max_orders_per_day"],3)
        self.assertEqual(cfg["max_daily_loss_fraction"],0.03)
        self.assertEqual(cfg["minimum_confidence"],0.70)

    def test_doctor_delegates_existing_openai_key_and_model_access(self):
        with patch.object(trader.ai,"diagnose_api",return_value=0) as diag:
            self.assertEqual(trader.doctor(),0)
            diag.assert_called_once()

    def test_closed_instruments_do_not_trigger_model_or_account_lookup(self):
        with patch.object(trader,"config",return_value={"watchlist":["SBER"]}),\
             patch.object(trader.c,"TOKEN_PATH",Path("/tmp/sandbox-token")),\
             patch.object(trader.t,"config",return_value={"account_ref":"tb_test"}),\
             patch.object(trader.ai,"read_key",return_value="KEY"),\
             patch.object(trader.t.s,"SandboxAPI") as broker,\
             patch.object(trader.c,"token_read",return_value="FAKE_SANDBOX"),\
             patch.object(trader.session,"open_watchlist",
                          return_value=([],[{"symbol":"SBER","allowed":False,
                                              "reason":"trading_status_session_close"}])),\
             patch.object(trader.ai,"ask_openai") as model,\
             patch.object(trader.t,"post_order") as order,\
             patch.object(trader.t,"get_account") as account,\
             patch.object(trader.ai,"log") as logger:
            self.assertEqual(trader.one_cycle(dry_run=False),0)
            model.assert_not_called()
            order.assert_not_called()
            account.assert_not_called()
            logger.assert_any_call("openai_market_closed",
                                   reason="no_verified_open_instruments",dry_run=False)


    def test_model_hold_dry_run_never_submits_or_reconciles(self):
        fakeapi=MagicMock()
        fakeapi.call.return_value={"orders":[]}
        context={
            "tradable":[{"symbol":"SBER","uid":"UID","lot":10,"price_rub":"300",
                         "owned_lots":0}],
            "equity_rub":"10000","cash_rub":"10000",
            "invested_rub_estimate":"0"
        }
        cfg={
          "model":"gpt-6.1-sol","watchlist":["SBER"],
          "max_ai_calls_per_day":50,"max_trade_fraction":.35,
          "max_exposure_fraction":.8,"max_daily_loss_fraction":.07,
          "minimum_cash_rub":850,"max_orders_per_day":6,
          "minimum_confidence":.7,"cooldown_minutes":20,
          "max_snapshot_age_seconds":600,"max_orderbook_spread_pct":.8
        }
        decision={"action":"HOLD","symbol":"NONE","lots":0,
                  "confidence":.95,"reason":"Наблюдения недостаточно сильны для покупки",
                  "risks":"Рынок может двигаться против модели","horizon_minutes":30}
        with patch.object(trader,"config",return_value=cfg),\
             patch.object(trader.session,"open_watchlist",return_value=(["SBER"],[])),\
             patch.object(trader.c,"TOKEN_PATH",Path("/tmp/sandbox-token")) ,\
             patch.object(trader.ai,"read_key",return_value="FAKE_KEY"),\
             patch.object(trader.t,"config",return_value={"account_ref":"tb_test"}),\
             patch.object(trader.t.s,"SandboxAPI",return_value=fakeapi),\
             patch.object(trader.c,"token_read",return_value="FAKE_SANDBOX"),\
             patch.object(trader.t,"get_account",return_value="FAKE_ACCOUNT"),\
             patch.object(trader.t,"load_json",return_value={}),\
             patch.object(trader.ai,"build_context",return_value=(context,{},{})),\
             patch.object(trader,"_quality",side_effect=lambda cfg,ctx,now: ctx),\
             patch.object(trader.ai,"ask_openai",return_value=(decision,{"input_tokens":100})),\
             patch.object(trader.ai,"validate",return_value=(None,"HOLD")) as validate,\
             patch.object(trader.t,"reconcile_pending") as reconcile,\
             patch.object(trader.t,"post_order") as order,\
             patch.object(trader.t,"atomic_save") as save,\
             patch.object(trader.shared,"equity_event") as equity,\
             patch.object(trader.ai,"log"):
            self.assertEqual(trader.one_cycle(dry_run=True),0)
            reconcile.assert_not_called()
            order.assert_not_called()
            save.assert_not_called()
            equity.assert_not_called()
            validate.assert_called_once()
            self.assertEqual(fakeapi.call.call_args[0][0],"trader_orders")


    def test_after_1835_moscow_can_use_model_if_exchange_is_open(self):
        # 17:42 UTC = 20:42 MSK. A live normal-trading status is authoritative.
        fakeapi=MagicMock()
        fakeapi.call.return_value={"orders":[]}
        ctx={"tradable":[{"symbol":"SBER","uid":"UID","lot":10,"price_rub":"300",
                          "owned_lots":0}],
             "equity_rub":"10000","cash_rub":"10000","invested_rub_estimate":"0"}
        cfg={"model":"gpt-6.1-sol","watchlist":["SBER"],"max_ai_calls_per_day":50}
        fake_clock=datetime(2026,11,12,17,42,0,tzinfo=timezone.utc)
        with patch.object(trader,"datetime") as clock,\
             patch.object(trader,"config",return_value=cfg),\
             patch.object(trader.c,"TOKEN_PATH",Path("/tmp/sandbox-token")),\
             patch.object(trader.ai,"read_key",return_value="FAKE_KEY"),\
             patch.object(trader.t,"config",return_value={"account_ref":"tb_test"}),\
             patch.object(trader.t.s,"SandboxAPI",return_value=fakeapi),\
             patch.object(trader.c,"token_read",return_value="FAKE_SANDBOX"),\
             patch.object(trader.t,"get_account",return_value="ACCOUNT"),\
             patch.object(trader.t,"load_json",return_value={}),\
             patch.object(trader.session,"open_watchlist",return_value=(["SBER"],[])) as probe,\
             patch.object(trader.ai,"build_context",return_value=(ctx,{},{})),\
             patch.object(trader,"_quality",side_effect=lambda cfg,ctx,now:ctx),\
             patch.object(trader.ai,"ask_openai",
                          return_value=({"action":"HOLD","symbol":"NONE","lots":0,
                                         "confidence":.9,"reason":"Наблюдение без сделки",
                                         "risks":"Возможна волатильность","horizon_minutes":30},{})) as model,\
             patch.object(trader.ai,"validate",return_value=(None,"HOLD")),\
             patch.object(trader.ai,"log"),\
             patch.object(trader.t,"post_order") as orders:
            clock.now.return_value=fake_clock
            self.assertEqual(trader.one_cycle(dry_run=True),0)
            self.assertEqual(probe.call_count,1)
            model.assert_called_once()
            orders.assert_not_called()

    def test_market_status_rechecked_before_virtual_order(self):
        # Static smoke test: the selected instrument's UID is probed anew
        # after the model, and after the local book quality gate.
        import inspect
        source=inspect.getsource(trader.one_cycle)
        self.assertNotIn("t.market_hours",source)
        self.assertGreaterEqual(source.count("session.instrument_open("),2)

    def test_cloud_agent_does_not_require_ollama_installation(self):
        import inspect
        src=inspect.getsource(trader.one_cycle)
        self.assertIn("ai.ask_openai",src)
        self.assertNotIn("ollama_request",src)
        self.assertNotIn("ollama pull",src)


if __name__=="__main__":
    unittest.main()
