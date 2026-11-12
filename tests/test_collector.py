import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector as C

class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): pass

class TestCollector(unittest.TestCase):
    def test_quote_money(self):
        self.assertEqual(C.val_quote({'units':'97','nano':2000000}),'97.002')
        self.assertEqual(C.val_quote({'units':'-1','nano':-500000000}),'-1.5')
        self.assertEqual(C.money({'currency':'rub','units':'100','nano':0}), {'currency':'rub','value':'100'})
    def test_method_allowlist(self):
        api=C.API('DUMMY', opener=lambda req,timeout:FakeResp(b'{}'))
        with self.assertRaises(ValueError):
            api.call('postorder',{})
        with self.assertRaises(ValueError):
            api.call('cancelorder',{})
    def test_auth_header_but_no_token_in_export(self):
        reqs=[]
        def fake(req, timeout):
            reqs.append(req)
            return FakeResp(b'{"accounts": []}')
        result=C.API('TOPSECRET',opener=fake).call('accounts',{})
        self.assertEqual(result['accounts'], [])
        self.assertEqual(reqs[0].get_header('Authorization'),'Bearer TOPSECRET')
        self.assertNotIn('TOPSECRET',json.dumps(result))
    def test_sanitize(self):
        salt=b'\x01'*32
        op={'id':'operation-secret-id','accountId':'account-secret-id','description':'customer secret','date':'2026-11-12T08:00:00Z','quantity':10,'type':'OPERATION_TYPE_COUPON', 'payment':{'currency':'rub','units':'100','nano':0}}
        sanitized=C.operation_transform(op,salt)
        js=json.dumps(sanitized)
        self.assertNotIn('account-secret-id',js)
        self.assertNotIn('customer secret',js)
        self.assertNotIn('operation-secret-id',js)
        self.assertEqual(sanitized['payment']['value'],'100')
    def test_paging(self):
        class Fake:
            n=0
            def call(self,m,p):
                self.n+=1
                if self.n==1:
                    return {'items':[{'id':'a','type':'OPERATION_TYPE_COUPON'}],'hasNext':True,'nextCursor':'next'}
                return {'items':[{'id':'b','type':'OPERATION_TYPE_BUY'}],'hasNext':False}
        with patch.object(C.time,'sleep'):
            rows, truncated=C.cursor_operations(Fake(),'acc','start','end',b'\x02'*32)
        self.assertEqual(len(rows),2)
        self.assertFalse(truncated)
    def test_http400_details_are_sanitized(self):
        from urllib.error import HTTPError
        message=b'{"code":"INVALID_ARGUMENT","message":"invalid candle window"}'
        def invalid(req,timeout):
            raise HTTPError(req.full_url,400,"Bad Request",{},io.BytesIO(message))
        api=C.API("DUMMYSECRET",opener=invalid)
        with self.assertRaises(C.APIError) as err:
            api.call("accounts",{})
        self.assertIn("invalid candle window",str(err.exception))
        self.assertNotIn("DUMMYSECRET",str(err.exception))
    def test_quote_sber_uses_ticker_and_board_not_ambiguous_find(self):
        class Fake:
            def __init__(self): self.requests=[]
            def call(self, method, body):
                self.requests.append((method, body))
                if method=="instrument":
                    return {"instrument":{"ticker":"SBER","classCode":"TQBR",
                                         "uid":"verified-tqbr-uid","isin":"RU0009029540",
                                         "name":"Сбербанк"}}
                raise AssertionError("Quote must not call unfiltered FindInstrument for SBER")
        api=Fake()
        instrument=C.quote_instrument(api,"SBER")
        self.assertEqual(instrument["uid"],"verified-tqbr-uid")
        self.assertEqual(api.requests[0],("instrument",{
            "idType":"INSTRUMENT_ID_TYPE_TICKER","classCode":"TQBR","id":"SBER"}))

    def test_quote_refuses_wrong_board_or_ambiguous_isin(self):
        class Fake:
            def call(self,method,body):
                if method=="instrument":
                    return {"instrument":{"ticker":"SBER","classCode":"SPBRU","uid":"bad-uid"}}
                return {"instruments":[
                    {"ticker":"OMEGA","isin":"RU000A1092T7","uid":"a"},
                    {"ticker":"OMEGA","isin":"RU000A1092T7","uid":"b"}]}
        with self.assertRaises(C.APIError):
            C.quote_instrument(Fake(),"SBER")
        with self.assertRaises(C.APIError):
            C.quote_instrument(Fake(),"RU000A1092T7")

    def test_portfolio_transform(self):
        raw={'accountId':'123secret','totalAmountPortfolio':{'currency':'rub','units':'1000','nano':0},'positions':[{'instrumentUid':'uid1','quantity':{'units':'10','nano':0},'currentPrice':{'currency':'rub','units':'100','nano':0}}]}
        p=C.portfolio_transform(raw)
        self.assertEqual(p['position_count'],1)
        self.assertNotIn('123secret',json.dumps(p))

if __name__=='__main__':unittest.main()
