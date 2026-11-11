"""Offline tests for pinned V2 MCP-only operator updater: no host/service writes."""
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import luk_bot_v2_operator
from tools import bootstrap_luk_bot_v2 as old_installer
from tools import bootstrap_luk_controlled_operator as v1
from tools import upgrade_luk_bot_v2_operator as upgrade


class V2UpdaterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.mcp = self.base / "mcp_server.py"
        self.operator = self.base / "luk_bot_v2_operator.py"
        root = Path(__file__).resolve().parents[1]
        original = (root / "mcp_server.py").read_text()
        guard = 'if __name__ == "__main__":'
        with_v1 = original.replace(guard, v1.WRAPPER + "\n" + guard)
        combined = old_installer.patch_mcp(with_v1)
        self.mcp.write_bytes(combined)
        for name in ("config.py", "database.py", "terminal_readonly.py",
                     "luk_remote_operator_v1.py"):
            (self.base / name).write_bytes((root / name).read_bytes())
        self.operator.write_text("dummy outdated operator")
        for key, value in {"BRIDGE": self.base, "MCP": self.mcp,
                           "OPERATOR": self.operator}.items():
            mock = patch.object(upgrade, key, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_pinned_new_blob_matches_working_module(self):
        source = (Path(__file__).resolve().parents[1] / "luk_bot_v2_operator.py").read_bytes()
        self.assertEqual(upgrade.git_blob(source), upgrade.NEW_BLOB)
        self.assertNotEqual(upgrade.OLD_BLOB, upgrade.NEW_BLOB)

    def test_canary_imports_new_backend_into_existing_toolchain(self):
        source = (Path(__file__).resolve().parents[1] / "luk_bot_v2_operator.py").read_bytes()
        with patch.object(upgrade, "command", wraps=upgrade.command):
            upgrade.canary(source)
        self.assertFalse((self.base / ".luk-terminal-backups").exists())

    def test_heartbeat_retries_until_authenticated_service_is_ready(self):
        unauthorized = urllib.error.HTTPError(
            "http://127.0.0.1:8765/mcp", 401, "Unauthorized", {}, None
        )
        with patch.object(upgrade.urllib.request, "urlopen",
                          side_effect=[urllib.error.URLError("refused"),
                                       unauthorized]) as open_request, \
             patch.object(upgrade.time, "sleep") as pause:
            self.assertTrue(upgrade.auth_heartbeat(retries=3))
            self.assertEqual(open_request.call_count, 2)
            pause.assert_called_once_with(1)

    def test_heartbeat_fails_closed_if_never_ready(self):
        with patch.object(upgrade.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("refused")) as req, \
             patch.object(upgrade.time, "sleep") as pause:
            self.assertFalse(upgrade.auth_heartbeat(retries=3))
            self.assertEqual(req.call_count, 3)
            self.assertEqual(pause.call_count, 2)

    def test_heartbeat_rejects_unexpected_http_status(self):
        unverified = urllib.error.HTTPError(
            "http://127.0.0.1:8765/mcp", 403, "Forbidden", {}, None
        )
        with patch.object(upgrade.urllib.request, "urlopen",
                          side_effect=unverified), \
             patch.object(upgrade.time, "sleep") as pause:
            self.assertFalse(upgrade.auth_heartbeat(retries=3))
            pause.assert_not_called()

    def test_mismatch_never_touches_module(self):
        previous = self.operator.read_bytes()
        with patch.object(upgrade.os, "getuid", return_value=1001), \
             patch.object(upgrade.os, "geteuid", return_value=1001), \
             patch("sys.argv", ["v2-updater.py", "--apply"]), \
             patch.object(upgrade, "verify_mcp_service") as svc:
            with self.assertRaisesRegex(RuntimeError, "BLOB_MISMATCH"):
                upgrade.main()
            svc.assert_not_called()
        self.assertEqual(self.operator.read_bytes(), previous)
        self.assertFalse((self.base / ".luk-terminal-backups").exists())


if __name__ == "__main__":
    unittest.main()
