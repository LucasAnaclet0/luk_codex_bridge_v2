"""Offline tests for scoped LUK maintenance; never touch the actual VPS."""
import hashlib
import json
import tempfile
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import luk_remote_operator_v1 as op
from tools import bootstrap_luk_controlled_operator as bootstrap


class OperatorOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bot = root / "main"
        self.dev = root / "dev"
        self.state = root / "state"
        self.backups = self.state / "backups"
        self.bot.mkdir()
        self.dev.mkdir()
        self.state.mkdir()
        self.backups.mkdir()
        self.python = self.bot / "venv/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("# fake interpreter")
        names = {"BOT": self.bot, "DEV": self.dev, "STATE": self.state,
                 "BACKUPS": self.backups, "CONTROL": self.state / "control.lock",
                 "MAIN_LOCK": self.bot / ".demo_bot.lock", "PYTHON": self.python}
        for name, value in names.items():
            p = patch.object(op, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(op, "_check_caller", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        self.original = b"def calc():\n    return 1\n"
        self.changed = b"def calc():\n    return 2\n"
        (self.bot / "main.py").write_bytes(self.original)
        (self.dev / "main.py").write_bytes(self.changed)

    def test_dev_code_can_be_delivered_without_live_mutation(self):
        result = op.run("dev_verify", {"action": "dev_verify", "file": "main.py"})
        self.assertTrue(result["result"]["syntax_ok"])
        self.assertFalse(result["result"]["main_changed"])
        self.assertFalse(result["result"]["tests_executed"])
        self.assertEqual((self.bot / "main.py").read_bytes(), self.original)

    def test_wrong_confirmation_never_promotes(self):
        arguments = {"action": "promote_dev", "file": "main.py",
                     "confirmation": "yes",
                     "expected_sha256": hashlib.sha256(self.original).hexdigest(),
                     "staged_sha256": hashlib.sha256(self.changed).hexdigest()}
        with self.assertRaises(PermissionError):
            op.run("promote_dev", arguments)
        self.assertEqual((self.bot / "main.py").read_bytes(), self.original)

    def test_running_main_blocks_promotion(self):
        arguments = {"action": "promote_dev", "file": "main.py",
                     "confirmation": op.CONFIRM["promote_dev"],
                     "expected_sha256": hashlib.sha256(self.original).hexdigest(),
                     "staged_sha256": hashlib.sha256(self.changed).hexdigest()}
        with patch.object(op, "_main_processes", return_value=[{"pid": 123}]):
            with self.assertRaisesRegex(RuntimeError, "MAIN still running"):
                op.run("promote_dev", arguments)
        self.assertEqual((self.bot / "main.py").read_bytes(), self.original)

    def test_promote_then_rollback_roundtrip(self):
        arguments = {"action": "promote_dev", "file": "main.py",
                     "confirmation": op.CONFIRM["promote_dev"],
                     "expected_sha256": hashlib.sha256(self.original).hexdigest(),
                     "staged_sha256": hashlib.sha256(self.changed).hexdigest()}
        clean_locks = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(op, "_main_processes", return_value=[]):
            with patch.object(op.subprocess, "run", return_value=clean_locks):
                promoted = op.run("promote_dev", arguments)
                self.assertEqual(promoted["result"]["status"], "MAIN_CODE_PROMOTED_NO_RESTART")
                self.assertEqual((self.bot / "main.py").read_bytes(), self.changed)
                restored = op.run("rollback_dev", {
                    "action": "rollback_dev", "file": "main.py",
                    "confirmation": op.CONFIRM["rollback_dev"]})
                self.assertEqual(restored["result"]["status"], "MAIN_CODE_ROLLED_BACK")
                self.assertEqual((self.bot / "main.py").read_bytes(), self.original)

    def test_wrong_sha_never_promotes(self):
        arguments = {"action": "promote_dev", "file": "main.py",
                     "confirmation": op.CONFIRM["promote_dev"],
                     "expected_sha256": "0" * 64,
                     "staged_sha256": hashlib.sha256(self.changed).hexdigest()}
        clean_locks = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(op, "_main_processes", return_value=[]):
            with patch.object(op.subprocess, "run", return_value=clean_locks):
                with self.assertRaisesRegex(RuntimeError, "stale live"):
                    op.run("promote_dev", arguments)
        self.assertEqual((self.bot / "main.py").read_bytes(), self.original)

    def test_stop_main_requires_kernel_flock_and_exact_identity(self):
        info = {"pid": 123, "start_ticks": 9981, "cwd": str(self.bot)}
        args = {"action": "main_stop", "confirmation": op.CONFIRM["main_stop"],
                "expected_pid": 123, "expected_start_ticks": 9981}
        with patch.object(op, "_main_processes", return_value=[info]):
            with patch.object(op, "_flock_owned", return_value=False):
                with patch.object(op.os, "kill") as kill:
                    with self.assertRaisesRegex(RuntimeError, "lock ownership unverified"):
                        op.run("main_stop", args)
                    kill.assert_not_called()
            args["expected_start_ticks"] = 9982
            with patch.object(op.os, "kill") as kill:
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    op.run("main_stop", args)
                kill.assert_not_called()

    def test_no_arbitrary_file_or_shell(self):
        with self.assertRaises(ValueError):
            op.run("rm -rf /", {"action": "rm -rf /"})
        with self.assertRaises(ValueError):
            op.run("dev_verify", {"action": "dev_verify", "file": ".env"})
        with self.assertRaises(ValueError):
            op.run("main_start", {"action": "main_start",
                                   "confirmation": op.CONFIRM["main_start"],
                                   "command": "rm -rf /"})

    def test_bootstrap_patcher_does_not_replace_v2(self):
        server = (
            "# LUK_TERMINAL_V2_MAIN_MAINTENANCE\n"
            "def _tools():\n    return []\n"
            "def _tool_payload(name, args):\n    return None\n"
            "class Handler: pass\n"
            "def main(): pass\n"
            'if __name__ == "__main__":\n    main()\n'
        )
        updated = bootstrap.patch_mcp(server)
        self.assertIn(bootstrap.MARKER.encode(), updated)
        self.assertIn(b"LUK_TERMINAL_V2_MAIN_MAINTENANCE", updated)
        compile(updated, "mcp_server.py", "exec")
        with self.assertRaises(RuntimeError):
            bootstrap.patch_mcp(updated.decode())


if __name__ == "__main__":
    unittest.main()
