"""Offline safety tests: never connect to OKX or manage a real service."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import luk_bot_v2_operator as mod
from tools import bootstrap_luk_bot_v2 as install


class V2OperatorOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bot = root / "bot_v2"
        self.bot.mkdir()
        self.bridge = root / "bridge"
        self.bridge.mkdir()
        self.dev = self.bridge / ".luk-bot-v2-dev-staging"
        self.ops = self.bridge / ".luk-bot-v2-operator"
        self.backups = self.ops / "backups"
        self.file = self.bot / "main.py"
        self.old = b"def f():\n    return 1\n"
        self.new = b"def f():\n    return 2\n"
        self.file.write_bytes(self.old)
        for name, value in {"ROOT": self.bot, "BRIDGE": self.bridge,
                            "DEV": self.dev, "OPS": self.ops,
                            "BACKUP": self.backups,
                            "MARKER": self.ops / "last_deployment.json",
                            "LOCK": self.ops / "operator.lock"}.items():
            p = patch.object(mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(mod.os, "getuid", return_value=1001)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(mod.os, "geteuid", return_value=1001)
        p.start()
        self.addCleanup(p.stop)

    def _patch(self, expected=None):
        return {"action": "dev_stage_patch", "file": "main.py",
                "old_text": "return 1", "new_text": "return 2",
                "expected_sha256": expected or hashlib.sha256(self.old).hexdigest()}

    def test_stage_only_and_syntax(self):
        result = mod.run(self._patch())
        self.assertEqual(result["result"]["status"], "STAGED_ONLY")
        self.assertTrue((self.dev / "main.py").is_file())
        self.assertEqual(self.file.read_bytes(), self.old)
        verified = mod.run({"action": "dev_verify", "file": "main.py"})
        self.assertTrue(verified["result"]["syntax_ok"])
        self.assertFalse(verified["result"]["tests_run"])

    def test_stale_hash_blocks_staging(self):
        with self.assertRaises(RuntimeError):
            mod.run(self._patch("0"*64))
        self.assertEqual(self.file.read_bytes(), self.old)

    def test_read_only_discovery_of_new_v2_python_file(self):
        file = self.bot / "improved_v2_strategy.py"
        file.write_text("def strategy():\n    return 42\n")
        observed = mod.run({"action": "source_read", "file": file.name,
                            "start_line": 1, "line_count": 2})
        self.assertIn("return 42", observed["result"]["excerpt"])
        with self.assertRaises(ValueError):
            mod.run({"action": "dev_stage_patch", "file": file.name,
                     "old_text": "return 42", "new_text": "return 43",
                     "expected_sha256": hashlib.sha256(file.read_bytes()).hexdigest()})

    def test_private_and_unknown_files_rejected(self):
        for name in (".env", "../bot.py", "secrets.py", "/etc/passwd"):
            with self.assertRaises(ValueError):
                mod.run({"action": "source_read", "file": name,
                         "start_line": 1, "line_count": 1})

    def test_promote_requires_confirmation_and_service_stopped(self):
        staged = mod.run(self._patch())["result"]
        args = {"action": "promote_dev", "file": "main.py",
                "expected_sha256": staged["live_sha256"],
                "staged_sha256": staged["staged_sha256"],
                "confirmation": "yes"}
        with self.assertRaises(PermissionError):
            mod.run(args)
        args["confirmation"] = mod.CONFIRM["promote_dev"]
        with patch.object(mod, "_service_identity", return_value=(
                {"ActiveState": "active", "MainPID": "42"}, [])):
            with self.assertRaisesRegex(RuntimeError, "not stopped"):
                mod.run(args)
        self.assertEqual(self.file.read_bytes(), self.old)

    def test_promote_rollback_with_hashes_and_backup(self):
        staged = mod.run(self._patch())["result"]
        args = {"action": "promote_dev", "file": "main.py",
                "expected_sha256": staged["live_sha256"],
                "staged_sha256": staged["staged_sha256"],
                "confirmation": mod.CONFIRM["promote_dev"]}
        with patch.object(mod, "_ensure_stopped"):
            result = mod.run(args)
            self.assertEqual(result["result"]["status"], "V2_SOURCE_PROMOTED_NO_RESTART")
            self.assertEqual(self.file.read_bytes(), self.new)
            rolled = mod.run({"action": "rollback", "file": "main.py",
                              "confirmation": mod.CONFIRM["rollback"]})
            self.assertEqual(rolled["result"]["status"], "V2_SOURCE_ROLLED_BACK_NO_RESTART")
            self.assertEqual(self.file.read_bytes(), self.old)

    def test_source_read_line_bound(self):
        r = mod.run({"action": "source_read", "file": "main.py", "start_line": 2,
                     "line_count": 1})
        self.assertEqual(r["result"]["excerpt"], "    return 1")
        with self.assertRaises(ValueError):
            mod.run({"action": "source_read", "file": "main.py",
                     "start_line": 1, "line_count": 101})

    def test_sqlite_counts_are_read_only(self):
        db = self.bot / "campaign.sqlite3"
        dbcon = sqlite3.connect(str(db))
        dbcon.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, closed INTEGER)")
        dbcon.execute("INSERT INTO trades(closed) VALUES (1)")
        dbcon.commit()
        dbcon.close()
        records = mod._sqlite_summary()
        self.assertEqual(records[0]["row_counts"]["trades"], 1)
        self.assertEqual(records[0]["status"], "OBSERVED")

    def test_unique_service_validation(self):
        expected = (
            "LoadState=loaded\nActiveState=active\nSubState=running\n"
            "MainPID=444\nUser=ubuntu\n"
            f"WorkingDirectory={self.bot}\n"
            f"ExecStart={{ path={self.bot}/venv/bin/python ; argv[]={{...}} }}\n"
            "FragmentPath=/etc/systemd/system/luk-bot-trader-v2.service\n"
        )
        def call(argv, timeout=8):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, expected, "")
            return subprocess.CompletedProcess(argv, 0,
                "luk-bot-trader-v2.service enabled\n", "")
        with patch.object(mod, "_cmd", side_effect=call):
            found = mod._service()
            self.assertEqual(found["status"], "OBSERVED")
            self.assertEqual(found["Unit"], "luk-bot-trader-v2.service")
            redacted = mod._public_service_status(found)
            self.assertNotIn("ExecStart", redacted)
            self.assertTrue(redacted["exec_start_contains_v2_path"])

    def test_reject_incorrect_service_target(self):
        expected = (
            "LoadState=loaded\nActiveState=active\nMainPID=444\nUser=ubuntu\n"
            "WorkingDirectory=/home/ubuntu/luk_bot_trader\n"
            "ExecStart=/home/ubuntu/luk_bot_trader/main.py\n"
            "FragmentPath=/etc/systemd/system/luk-bot-trader-v2.service\n"
        )
        def call(argv, timeout=8):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, expected, "")
            return subprocess.CompletedProcess(argv, 0, "luk-bot-trader-v2.service enabled\n", "")
        with patch.object(mod, "_cmd", side_effect=call):
            with self.assertRaises(RuntimeError):
                mod._service_identity()

    def test_stop_checks_pid_identity_and_no_unsafe_signals(self):
        service = {"status": "OBSERVED", "Unit": "luk-bot-trader-v2.service",
                   "ActiveState": "active", "MainPID": "777"}
        procs = [{"pid": 777, "start_ticks": 77, "cwd": str(self.bot)}]
        with patch.object(mod, "_service_identity", return_value=(service, procs)):
            with patch.object(mod, "_cmd") as command:
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    mod.run({"action": "service_stop",
                             "confirmation": mod.CONFIRM["service_stop"],
                             "expected_pid": 777, "expected_ticks": 78})
                command.assert_not_called()

    def test_canary_patch_of_existing_mcp_registration(self):
        server = (
            "# LUK_CONTROLLED_OPERATOR_V1\n"
            "def _tools():\n"
            "    items = [{'name': 'base_tool'}]\n"
            "    return items\n"
            "def _tool_payload(name, arguments):\n"
            "    return None\n"
            "class Handler: pass\n"
            "def main(): pass\n"
            'if __name__ == "__main__":\n    main()\n'
        )
        updated = install.patch_mcp(server)
        self.assertIn(b"LUK_BOT_TRADER_V2_PLUGIN", updated)
        compile(updated, "mcp_server.py", "exec")
        with self.assertRaises(RuntimeError):
            install.patch_mcp(updated.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
