"""Exercise the full MCP tool registry with V1 + V2 wrappers, not just AST."""
from pathlib import Path
import subprocess
import sys
import unittest
import tempfile

from tools import bootstrap_luk_controlled_operator as v1
from tools import bootstrap_luk_bot_v2 as v2


class CombinedMCPV2Canary(unittest.TestCase):
    def test_legacy_v1_plus_v2_module_canary(self):
        root = Path(__file__).resolve().parents[1]
        original = (root / "mcp_server.py").read_text()
        self.assertNotIn("LUK_CONTROLLED_OPERATOR_V1", original)
        old_guard = 'if __name__ == "__main__":'
        self.assertEqual(original.count(old_guard), 1)
        augmented = original.replace(old_guard, v1.WRAPPER + "\n" + old_guard)
        candidate = v2.patch_mcp(augmented).decode("utf-8")
        self.assertEqual(candidate.count("LUK_BOT_TRADER_V2_PLUGIN"), 1)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for name in ("config.py", "database.py", "terminal_readonly.py",
                         "luk_remote_operator_v1.py", "luk_bot_v2_operator.py"):
                (workspace / name).write_bytes((root / name).read_bytes())
            (workspace / "mcp_server.py").write_text(candidate)
            script = (
                "from mcp_server import _tools, _tool_payload\n"
                "names = [entry['name'] for entry in _tools()]\n"
                "assert names.count('vps_bot_trader_v2') == 1, names\n"
                "assert names.count('vps_controlled_operator_v1') == 1, names\n"
                "for args in ({'action':'shell'}, {'action':'service_stop','command':'kill -9 1'}):\n"
                "    try: _tool_payload('vps_bot_trader_v2', args)\n"
                "    except ValueError: continue\n"
                "    raise AssertionError('invalid arguments accepted')\n"
                "print('COMBINED_MCP_CANARY_OK')\n"
            )
            process = subprocess.run([sys.executable, "-B", "-c", script],
                 cwd=workspace, capture_output=True, text=True, timeout=8)
            self.assertEqual(process.returncode, 0, process.stderr[-1800:])
            self.assertIn("COMBINED_MCP_CANARY_OK", process.stdout)


if __name__ == "__main__":
    unittest.main()
