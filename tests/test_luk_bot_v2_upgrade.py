"""Offline tests for pinned V2 MCP-only operator updater: no host/service writes."""
from pathlib import Path
import tempfile
import unittest
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
