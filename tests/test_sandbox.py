import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sandbox as s

class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): pass

class TestSandbox(unittest.TestCase):
    def test_endpoint_and_routes(self):
        self.assertEqual(s.c.METHODS['accounts'],'SandboxService/GetSandboxAccounts')
        self.assertEqual(s.c.METHODS['portfolio'],'SandboxService/GetSandboxPortfolio')
        self.assertEqual(s.c.METHODS['positions'],'SandboxService/GetSandboxPositions')
        self.assertEqual(s.c.METHODS['operations'],'SandboxService/GetSandboxOperationsByCursor')
        self.assertIn('sandbox-invest-public-api.tbank.ru',s.SandboxAPI('FAKE').base)
        self.assertTrue(s.c.TOKEN_PATH.name.endswith('sandbox-token'))

    def test_no_prod(self):
        with self.assertRaises(ValueError):
            s.SandboxAPI('FAKE',base='https://invest-public-api.tbank.ru/rest/')

    def test_sandbox_bearer(self):
        requests=[]
        def fake(req,timeout):
            requests.append(req)
            return FakeResponse(b'{"accounts":[]}')
        a=s.SandboxAPI('FAKE',opener=fake)
        self.assertEqual(a.call('accounts',{})['accounts'],[])
        self.assertTrue(requests[0].full_url.startswith(s.SANDBOX_BASE))

    def test_fund_boundaries(self):
        with self.assertRaises(s.c.APIError): s.fund_account('tb_x','0')
        with self.assertRaises(s.c.APIError): s.fund_account('tb_x','30000001')
        with self.assertRaises(s.c.APIError): s.fund_account('tb_x','2.5')

if __name__=='__main__': unittest.main()
