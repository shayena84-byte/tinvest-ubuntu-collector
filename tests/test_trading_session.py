"""All tests use fake GetTradingStatus responses, no network and no orders."""
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trading_session as s


UID="valid-share-uid"

def resp(state=s.NORMAL, api=True, market=True, uid=UID):
    return {"instrumentUid":uid,"tradingStatus":state,
            "apiTradeAvailableFlag":api,
            "marketOrderAvailableFlag":market}

class TradingSessionTests(unittest.TestCase):
    def test_evening_uses_actual_normal_trading_not_clock(self):
        evening=datetime(2026,11,12,17,42,tzinfo=timezone.utc)
        self.assertEqual(evening.hour,17)
        self.assertEqual(s.status_reason(resp(),UID),(True,"normal_trading_verified"))

    def test_weekend_cannot_trade_if_api_reports_closed(self):
        weekend=datetime(2026,11,14,12,0,tzinfo=timezone.utc)
        self.assertEqual(weekend.weekday(),5)
        self.assertFalse(s.status_reason(resp("SECURITY_TRADING_STATUS_SESSION_CLOSE"),UID)[0])

    def test_exchange_may_open_on_special_day_if_status_normal(self):
        # No weekday whitelist. A genuine unexpected session requires live flags.
        holiday=datetime(2026,11,15,12,0,tzinfo=timezone.utc)
        self.assertEqual(holiday.weekday(),6)
        self.assertTrue(s.status_reason(resp(),UID)[0])

    def test_opening_auction_disallowed(self):
        for state in ("SECURITY_TRADING_STATUS_OPENING_PERIOD",
                      "SECURITY_TRADING_STATUS_OPENING_AUCTION_PERIOD",
                      "SECURITY_TRADING_STATUS_CLOSING_AUCTION",
                      "SECURITY_TRADING_STATUS_DISCRETE_AUCTION",
                      "SECURITY_TRADING_STATUS_BREAK_IN_TRADING",
                      "SECURITY_TRADING_STATUS_SESSION_ASSIGNED",
                      "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING",
                      "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING"):
            self.assertFalse(s.status_reason(resp(state),UID)[0],state)

    def test_market_order_flag_false_rejects(self):
        self.assertEqual(s.status_reason(resp(market=False),UID),
                         (False,"market_orders_not_available"))

    def test_api_trade_flag_false_rejects(self):
        self.assertEqual(s.status_reason(resp(api=False),UID),
                         (False,"api_trading_not_available"))

    def test_missing_flags_rejects(self):
        for field in ("apiTradeAvailableFlag","marketOrderAvailableFlag"):
            sample=resp()
            sample.pop(field)
            self.assertFalse(s.status_reason(sample,UID)[0])

    def test_uid_mismatch_or_missing_rejects(self):
        self.assertEqual(s.status_reason(resp(uid="another-uid"),UID),
                         (False,"instrument_uid_unverified"))
        d=resp()
        d.pop("instrumentUid")
        self.assertFalse(s.status_reason(d,UID)[0])

    def test_unknown_or_invalid_response_rejects(self):
        self.assertFalse(s.status_reason({"instrumentUid":UID},UID)[0])
        self.assertFalse(s.status_reason(None,UID)[0])
        self.assertFalse(s.status_reason([],UID)[0])

    def test_probes_only_ticker_verified_share_uids(self):
        api=Mock()
        api.call.side_effect=lambda method,body: (
            resp(uid=body["instrumentId"]) if method=="trader_status" else {})
        def find(_api,symbol):
            return {"uid":"id-"+symbol,"ticker":symbol,"lot":10}
        with patch.object(s.trader,"find_share",side_effect=find):
            allowed,results=s.open_watchlist(api,["SBER","MOEX"])
        self.assertEqual(allowed,["SBER","MOEX"])
        self.assertEqual([x["reason"] for x in results],["normal_trading_verified"]*2)
        self.assertEqual([x[0][1]["instrumentId"] for x in api.call.call_args_list],
                         ["id-SBER","id-MOEX"])

    def test_one_closed_share_does_not_prevent_other(self):
        api=Mock()
        def fake_call(method,request):
            if request["instrumentId"]=="id-MOEX":
                return resp(state="SECURITY_TRADING_STATUS_BREAK_IN_TRADING",uid="id-MOEX")
            return resp(uid=request["instrumentId"])
        api.call.side_effect=fake_call
        with patch.object(s.trader,"find_share",
               side_effect=lambda api,ticker:{"uid":"id-"+ticker}):
            allowed,details=s.open_watchlist(api,["SBER","MOEX"])
        self.assertEqual(allowed,["SBER"])
        self.assertFalse(details[1]["allowed"])

    def test_error_fails_closed_per_instrument(self):
        with patch.object(s.trader,"find_share",side_effect=s.trader.c.APIError("bad request")):
            allowed,items=s.open_watchlist(Mock(),["SBER"])
        self.assertEqual(allowed,[])
        self.assertEqual(items[0]["reason"],"status_probe_error")

    def test_immediate_status_query_by_exact_uid(self):
        api=Mock()
        api.call.return_value=resp()
        self.assertEqual(s.instrument_open(api,UID),(True,"normal_trading_verified"))
        api.call.assert_called_once_with("trader_status",{"instrumentId":UID})
        with self.assertRaises(s.trader.c.APIError):
            s.instrument_open(api,"")


if __name__=="__main__":
    unittest.main()
