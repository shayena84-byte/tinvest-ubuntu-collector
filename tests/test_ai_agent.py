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
        from io import BytesIO
        result={"action":"HOLD","symbol":"NONE","lots":0,"confidence":.9,
                "reason":"Недостаточно данных для входа в позицию","risks":"Нет подтверждения","horizon_minutes":30}
        raw={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":json.dumps(result)}]}],"usage":{}}
        def transport(request, timeout):
            body=json.loads(request.data)
            self.assertNotIn("tools",body)
            self.assertFalse(body["store"])
            self.assertEqual(body["text"]["format"]["type"],"json_schema")
            return BytesIO(json.dumps(raw).encode())
        with patch.object(agent,"urlopen",side_effect=transport):
            decision,_=agent._request_openai("FAKE","gpt-test",{})
        self.assertEqual(decision["action"],"HOLD")

    def test_cash_risk_limits(self):
        now=datetime(2026,11,12,9,tzinfo=timezone.utc)
        row={"symbol":"SBER","uid":"mock","lot":10,"price_rub":"300","owned_lots":0}
        ctx={"tradable":[row],"equity_rub":"40000","cash_rub":"40000","invested_rub_estimate":"0"}
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

    def test_openai_403_safe_diagnostics(self):
        from urllib.error import HTTPError
        from io import BytesIO
        body=b'{"error":{"type":"invalid_request_error","code":"unsupported_country_region_territory","message":"secret-user-contact@example.net"}}'
        e=HTTPError("https://api.openai.com/v1/responses",403,"Forbidden",{},BytesIO(body))
        output=agent.safe_api_error(e)
        self.assertIn("HTTP 403",output)
        self.assertIn("unsupported_country_region_territory",output)
        self.assertNotIn("secret-user-contact",output)

    def test_openai_403_invalid_body(self):
        from urllib.error import HTTPError
        from io import BytesIO
        e=HTTPError("https://api.openai.com/v1/responses",403,"Forbidden",{},BytesIO(b"unparseable HTML body"))
        self.assertEqual(agent.safe_api_error(e),"HTTP 403")

    def test_legacy_cli_uses_shared_engine(self):
        with patch("openai_quality_trader.one_cycle",return_value=0) as engine:
            self.assertEqual(agent.run(dry_run=True),0)
            engine.assert_called_once_with(dry_run=True)

if __name__=="__main__":
    unittest.main()
