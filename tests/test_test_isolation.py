"""Protect the operator's filesystem from installer test fixtures."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import test_data_cleanup

SOURCE = Path(__file__).resolve().parents[1]


class TestInstallerIsolation(unittest.TestCase):
    def test_runner_cannot_write_operator_home_or_open_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, tests = root/"operator", root/"probe"
            target = home/".local/share/tinvest-market/trading.sqlite3"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"existing operator data")
            tests.mkdir()
            (tests/"test_probe.py").write_text('''import unittest,socket
from pathlib import Path
class Probe(unittest.TestCase):
    def test_isolation(self):
        target=Path.home()/".local/share/tinvest-market/trading.sqlite3"
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(b"test fixture")
        with self.assertRaises(AssertionError):
            socket.create_connection(("example.invalid",443))
''')
            code = '''import sys
from pathlib import Path
from unittest.mock import patch
with patch.object(Path,"home",return_value=Path(sys.argv[1])):
    import run_offline_tests
    result=run_offline_tests.run(test_directory=Path(sys.argv[2]),pattern="test_probe.py")
raise SystemExit(result)
'''
            result = subprocess.run([sys.executable,"-c",code,str(home),str(tests)],cwd=SOURCE,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(target.read_bytes(),b"existing operator data")

    def fixture_database(self, path):
        import trade_ledger
        with trade_ledger.connection(path) as db:
            for identifier in ("fixture","linked","real","run"):
                context = json.loads(json.dumps(test_data_cleanup.CONTEXT))
                if identifier == "real":
                    context["tradable"][0]["uid"] = "12345678-1234-1234-1234-123456789abc"
                db.execute("INSERT INTO cycles(id,utc,mode,provider,model,version,context_json,decision_json,validation,status) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)",(identifier,"2026-11-12T17:42:00+00:00",
                            "run" if identifier == "run" else "dry-run","openai","gpt-6.1-sol","1.11.0",
                            json.dumps(context),json.dumps(test_data_cleanup.DECISIONS[0]),"HOLD","hold"))
            db.execute("INSERT INTO model_calls(id,cycle_id,utc,day,model,mode,status) VALUES(?,?,?,?,?,?,?)",
                       ("paid","linked","2026-11-12T17:42:00+00:00","2026-11-12","gpt-6.1-sol","dry-run","completed"))

    def test_cleanup_only_exact_unlinked_mocked_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"trading.sqlite3"
            self.fixture_database(path)
            preview = test_data_cleanup.cleanup(path)
            self.assertEqual(preview["candidates"],["fixture"])
            self.assertEqual(preview["removed"],0)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0],4)
            applied = test_data_cleanup.cleanup(path,apply=True,backup=True)
            self.assertEqual(applied["removed"],1)
            with sqlite3.connect(applied["backup"]) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0],4)
            with sqlite3.connect(path) as db:
                self.assertEqual({r[0] for r in db.execute("SELECT id FROM cycles")},{"linked","real","run"})
                self.assertEqual(db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0],1)

    def test_cleanup_absent_or_unknown_database_does_not_create_or_modify(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory)/"missing.sqlite3"
            self.assertEqual(test_data_cleanup.cleanup(missing,apply=True)["removed"],0)
            self.assertFalse(missing.exists())
            unknown = Path(directory)/"unknown.sqlite3"
            with sqlite3.connect(unknown) as db:
                db.execute("CREATE TABLE unrelated(value TEXT)")
            self.assertEqual(test_data_cleanup.cleanup(unknown,apply=True)["skipped"],"unrecognized_schema")
            with sqlite3.connect(unknown) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0],0)


if __name__ == "__main__":
    unittest.main()
