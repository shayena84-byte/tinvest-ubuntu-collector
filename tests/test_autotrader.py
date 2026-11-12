import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sandbox_autotrader as t

class TestSandboxTrader(unittest.TestCase):
    def test_sandbox_only_order_route(self):
        self.assertEqual(t.c.METHODS["trader_post"],"SandboxService/PostSandboxOrder")
        self.assertEqual(t.c.METHODS["trader_cancel"],"SandboxService/CancelSandboxOrder")
        self.assertEqual(t.c.METHODS["trader_orders"],"SandboxService/GetSandboxOrders")
        self.assertIn("sandbox-invest-public-api.tbank.ru",t.s.SANDBOX_BASE)

    def test_find_share_uses_exact_ticker_board_uid(self):
        class Fake:
            def call(self,method,body):
                self.method,self.body=method,body
                return {"instrument":{"ticker":"SBER","classCode":"TQBR",
                            "uid":"correct","lot":10,"currency":"rub",
                            "instrumentType":"share","apiTradeAvailableFlag":True}}
        api=Fake()
        out=t.find_share(api,"SBER")
        self.assertEqual(out["uid"],"correct")
        self.assertEqual(out["lot"],10)
        self.assertEqual(api.body["classCode"],"TQBR")
        self.assertEqual(api.body["idType"],"INSTRUMENT_ID_TYPE_TICKER")

    def test_find_share_fails_closed_on_wrong_board(self):
        class Fake:
            def call(self,method,body):
                return {"instrument":{"ticker":"SBER","classCode":"SPBRU",
                            "uid":"wrong","lot":10,"currency":"rub",
                            "instrumentType":"share","apiTradeAvailableFlag":True}}
        with self.assertRaises(t.c.APIError):
            t.find_share(Fake(),"SBER")

    def test_trading_hours(self):
        # Mon 2026-11-09 09:00 Moscow = 06:00 UTC
        self.assertFalse(t.market_hours(datetime(2026,11,9,6,0,tzinfo=timezone.utc)))
        self.assertTrue(t.market_hours(datetime(2026,11,9,9,0,tzinfo=timezone.utc)))
        self.assertFalse(t.market_hours(datetime(2026,11,7,9,0,tzinfo=timezone.utc)))

    def test_indicators_and_signals(self):
        up=[100+i*.1 for i in range(55)]
        info=t.indicators(up)
        self.assertGreater(info["ema9"],info["ema21"])
        self.assertGreater(info["momentum_30m"],0)
        self.assertTrue(0<=info["rsi14"]<=100)
        self.assertEqual(t.sell_signal(info,Decimal("90"),Decimal("100"),Decimal("100"),
                    {"stop_loss":.025,"take_profit":.045,"trailing_stop":.025}),"stop_loss")
        self.assertEqual(t.sell_signal(info,Decimal("106"),Decimal("100"),Decimal("106"),
                    {"stop_loss":.025,"take_profit":.045,"trailing_stop":.025}),"take_profit")

    def test_position_and_cash(self):
        p={"positions":[{"instrumentUid":"abc","quantityLots":{"units":"2","nano":0},"quantity":{"units":"20","nano":0}}]}
        self.assertEqual(t.held_lots(p,"abc",10),2)
        self.assertEqual(t.rub_cash({"money":[{"currency":"rub","units":"1000","nano":0},{"currency":"usd","units":"50","nano":0}]}),Decimal("1000"))

    def test_market_order_hard_guard(self):
        prod=t.s._OriginalAPI("FAKE",base="https://invest-public-api.tbank.ru/rest/test")
        with self.assertRaises(t.c.APIError):
            t.post_order(prod,"secretacc",{"ticker":"SBER","uid":"fake"},
                         "BUY",1,Decimal("300"),"test",{},datetime.now(timezone.utc))

    def test_day_rollover(self):
        now=datetime(2026,11,12,9,tzinfo=timezone.utc)
        state=t.initialize_day({"day":"2026-11-11","orders_today":4},now,Decimal("10000"))
        self.assertEqual(state["day"],"2026-11-12")
        self.assertEqual(state["orders_today"],0)
        self.assertEqual(state["start_equity"],"10000")

    def test_candles_request_conservative_window(self):
        from datetime import timedelta
        now=datetime(2026,11,12,11,0,tzinfo=timezone.utc)
        class Stub:
            body=None
            def call(self,method,payload):
                self.body=(method,payload)
                candles=[]
                for i in range(45):
                    ts=now-timedelta(minutes=(45-i)*5)
                    candles.append({"time":ts.isoformat(),"close":{"units":str(300+i//10),"nano":0},
                                    "volume":10,"isComplete":True})
                return {"candles":candles}
        fake=Stub()
        values=t.candles(fake,"test_uid",now)
        self.assertEqual(len(values),45)
        method,body=fake.body
        self.assertEqual(method,"trader_candles")
        self.assertEqual(body["interval"],"CANDLE_INTERVAL_5_MIN")
        self.assertLess((now-datetime.fromisoformat(body["from"])).total_seconds(),7*24*3600)
        self.assertNotIn("candleSourceType",body)
        self.assertLessEqual(body["limit"],500)

    def test_buy_signal_not_for_overbought(self):
        bullish={"ema9":101,"ema21":100,"rsi14":62,"momentum_30m":.006}
        self.assertTrue(t.buy_signal(bullish))
        bullish["rsi14"]=85
        self.assertFalse(t.buy_signal(bullish))

if __name__=="__main__":unittest.main()
