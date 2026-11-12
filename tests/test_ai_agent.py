import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_sandbox_agent as agent

class TestAIAgent(unittest.TestCase):
    def test_decode_strict_response(self):
        result={"action":"BUY","symbol":"SBER","lots":1,"confidence":.86,
                "reason":"Два независимых показателя подтверждают импульс спроса",
                "risks":"Высокая волатильность и неполная ликвидность",
                "horizon_minutes":120}
        response={"status":"completed","output":[{"type":"message",
                  "content":[{"type":"output_text","text":json.dumps(result)}]}]}
        self.assertEqual(agent.decode_response(response),result)
        result["lots"]=-1
        response["output"][0]["content"][0]["text"]=json.dumps(result)
        with self.assertRaises(agent.c.APIError):
            agent.decode_response(response)

    def test_no_model_trade_call(self):
        # Agent must not expose order placement tool to the LLM.
        import inspect
        source=inspect.getsource(agent.ask_openai)
        self.assertNotIn('"tools"',source)
        self.assertIn('json_schema',source)
        self.assertIn('"store":False',source)

    def test_cash_risk_limits(self):
        now=datetime(2026,11,12,9,tzinfo=timezone.utc)
        row={"symbol":"SBER","uid":"mock","lot":10,"price_rub":"300","owned_lots":0}
        ctx={"tradable":[row],"equity_rub":"10000","cash_rub":"10000","invested_rub_estimate":"0"}
        cfg={**agent.DEFAULT_AI}
        decision={"action":"BUY","symbol":"SBER","lots":2,"confidence":.90,
                  "reason":"Рост подтверждён повышенной торговой активностью",
                  "risks":"Риск отката после роста","horizon_minutes":60}
        t, err=agent.validate(decision,ctx,cfg,{"orders_today":0},now)
        self.assertIsNone(t)
        self.assertIn("cap",err)
        decision["lots"]=1
        t,err=agent.validate(decision,ctx,cfg,{"orders_today":0},now)
        self.assertIsNotNone(t)
        self.assertEqual(t["action"],"BUY")

    def test_no_short_selling(self):
        now=datetime(2026,11,12,9,tzinfo=timezone.utc)
        ctx={"tradable":[{"symbol":"SBER","uid":"x","lot":10,"price_rub":"300","owned_lots":0}],
             "equity_rub":"10000","cash_rub":"10000","invested_rub_estimate":"0"}
        a={"action":"SELL","symbol":"SBER","lots":1,"confidence":.9,
           "reason":"Предупредительный сигнал к продаже актива",
           "risks":"Риск слепого следования сигналу","horizon_minutes":120}
        trade,err=agent.validate(a,ctx,agent.DEFAULT_AI,{"orders_today":0},now)
        self.assertIsNone(trade)
        self.assertIn("Cannot sell",err)

    def test_hold_requires_no_lots(self):
        now=datetime(2026,11,12,9,tzinfo=timezone.utc)
        a={"action":"HOLD","symbol":"NONE","lots":0,"confidence":.9,
           "reason":"Отсутствуют надёжные ценовые сигналы",
           "risks":"Упущенная возможность","horizon_minutes":0}
        trade,why=agent.validate(a,{"tradable":[],"equity_rub":"10000","cash_rub":"10000"},agent.DEFAULT_AI,{},now)
        self.assertIsNone(trade)
        self.assertEqual(why,"HOLD")

    def test_api_daily_limit(self):
        with patch.object(agent,'read_key',return_value="sk-test"),patch.object(agent.t,'market_hours',return_value=False):
            self.assertIsNone(agent.run(dry_run=True))

if __name__=="__main__":
    unittest.main()
