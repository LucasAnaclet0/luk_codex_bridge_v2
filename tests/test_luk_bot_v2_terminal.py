"""Offline tests for command-approval and MCP registration (no VPS connection)."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import luk_vps_terminal as terminal
from tools.install_luk_terminal_v2 import patch_mcp, MARKER


class TerminalV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        bridge = base / "bridge"
        v2 = base / "v2"
        home = base / "home"
        for directory in (bridge, v2, home):
            directory.mkdir()
        vals = {"BRIDGE": bridge, "V2": v2, "HOME": home,
                "AUDIT_DIR": bridge / ".luk-terminal-v2",
                "AUDIT_LOG": bridge / ".luk-terminal-v2" / "execution_audit.jsonl",
                "CWD_MAP": {"v2": v2, "bridge": bridge, "home": home}}
        for key, value in vals.items():
            p = patch.object(terminal, key, value)
            p.start()
            self.addCleanup(p.stop)
        for key in ("getuid", "geteuid"):
            p = patch.object(terminal.os, key, return_value=1001)
            p.start()
            self.addCleanup(p.stop)

    def plan(self, cmd="printf 'OK!\\n'", cwd="v2", timeout=5):
        data = {"action": "plan", "command": cmd, "cwd": cwd,
                "timeout_seconds": timeout}
        response = terminal.run(data)
        self.assertEqual(response["status"], "PLANNED_NOT_EXECUTED")
        self.assertEqual(response["root_access"], False)
        return response

    def execute(self, cmd, digest, cwd="v2", timeout=5):
        return terminal.run({"action": "execute", "command": cmd, "cwd": cwd,
                             "timeout_seconds": timeout,
                             "expected_plan_sha256": digest,
                             "confirmation": terminal.CONFIRMATION})

    def test_status_is_readonly_and_unprivileged(self):
        status = terminal.run({"action": "status"})
        self.assertEqual(status["uid"], 1001)
        self.assertFalse(status["host_attested"])
        self.assertEqual(status["status"], "READY")
        self.assertFalse(terminal.AUDIT_DIR.exists())

    def test_plan_never_executes(self):
        output = terminal.V2 / "should-not-exist"
        self.plan("touch should-not-exist")
        self.assertFalse(output.exists())
        self.assertFalse(terminal.AUDIT_DIR.exists())

    def test_execute_binds_exact_command_dir_timeout_and_audits_hash_only(self):
        cmd = "printf 'LUK_TERMINAL_OK\\n'"
        plan = self.plan(cmd)
        with self.assertRaisesRegex(RuntimeError, "differs from its approved"):
            self.execute(cmd + " ", plan["expected_plan_sha256"])
        self.assertFalse(terminal.AUDIT_DIR.exists())
        result = self.execute(cmd, plan["expected_plan_sha256"])
        self.assertEqual(result["status"], "EXECUTED")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("LUK_TERMINAL_OK", result["output"])
        self.assertTrue(terminal.AUDIT_LOG.exists())
        entries = [json.loads(line) for line in terminal.AUDIT_LOG.read_text().splitlines()]
        self.assertEqual([row["result"] for row in entries], ["started", "finished"])
        self.assertEqual(entries[1]["command_sha256"], hashlib.sha256(cmd.encode()).hexdigest())
        self.assertNotIn(cmd, terminal.AUDIT_LOG.read_text())

    def test_missing_approval_and_wrong_schema_never_execute(self):
        data = {"action": "execute", "command": "touch not-executed",
                "cwd": "v2", "timeout_seconds": 2,
                "expected_plan_sha256": "0"*64, "confirmation": "yes"}
        with self.assertRaises(PermissionError):
            terminal.run(data)
        with self.assertRaises(ValueError):
            terminal.run({"action": "plan", "command": "echo Hi", "cwd": "v2",
                          "timeout_seconds": 2, "extra": "no"})
        with self.assertRaises(ValueError):
            terminal.run({"action": "plan", "command": "echo Hi", "cwd": "/",
                          "timeout_seconds": 2})
        with self.assertRaises(ValueError):
            terminal.run({"action": "plan", "command": "\u0000", "cwd": "v2",
                          "timeout_seconds": 2})
        self.assertFalse((terminal.V2 / "not-executed").exists())

    def test_redacts_credentials_in_output(self):
        cmd = "printf 'api_key=FAKE_SECRET\\nordinary-output\\n'"
        result = self.execute(cmd, self.plan(cmd)["expected_plan_sha256"])
        self.assertNotIn("FAKE_SECRET", result["output"])
        self.assertIn("ordinary-output", result["output"])

    def test_stdout_is_bounded(self):
        cmd = "python3 -c \"print('x'*24000)\""
        result = self.execute(cmd, self.plan(cmd)["expected_plan_sha256"])
        self.assertTrue(result["output_truncated"])
        self.assertLessEqual(len(result["output"].encode("utf-8")), terminal.MAX_RETURN_BYTES)

    def test_timeout_terminates_process_group(self):
        cmd = "sleep 5"
        result = self.execute(cmd, self.plan(cmd, timeout=1)["expected_plan_sha256"],
                              timeout=1)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["status"], "TIMED_OUT")

    def test_symlinked_audit_directory_blocks_execution(self):
        terminal.AUDIT_DIR.symlink_to(terminal.V2, target_is_directory=True)
        cmd = "touch blocked"
        with self.assertRaisesRegex(RuntimeError, "symlinked"):
            self.execute(cmd, self.plan(cmd)["expected_plan_sha256"])
        self.assertFalse((terminal.V2 / "blocked").exists())


MCP_FIXTURE = """# Original authenticated MCP registry. Ãé Unicode offset test.
def _tools():
    if True:
        return [{"name": "vps_bot_trader_v2"}]
    return [{"name": "old"}]

def _tool_payload(name, arguments):
    if type(arguments) is not dict:
        raise ValueError("bad args")
    return {"name": name}

class Handler:
    pass

# LUK_BOT_TRADER_V2_PLUGIN
if __name__ == "__main__":
    pass
"""


class TerminalMCPPatchTests(unittest.TestCase):
    def test_patches_only_exact_tool_and_preserves_previous_registry(self):
        source = patch_mcp(MCP_FIXTURE).decode()
        self.assertIn(MARKER, source)
        compile(source, "mcp_server.py", "exec")
        ns = {"__name__": "test"}
        exec(source, ns)
        tools = ns["_tools"]()
        self.assertEqual([x["name"] for x in tools],
                         ["vps_bot_trader_v2", "vps_terminal_v2"])
        with self.assertRaises(ValueError):
            ns["_tool_payload"]("vps_terminal_v2", {"action": "execute"})
        self.assertEqual(ns["_tool_payload"]("vps_bot_trader_v2", {"action": "status"}),
                         {"name": "vps_bot_trader_v2"})

    def test_rejects_duplicate_install(self):
        converted = patch_mcp(MCP_FIXTURE).decode()
        with self.assertRaisesRegex(RuntimeError, "ALREADY_REGISTERED"):
            patch_mcp(converted)

    def test_only_wraps_existing_return_paths(self):
        altered = MCP_FIXTURE.replace('return [{"name": "old"}]',
                                     'return [{"name": "another"}]')
        ns = {"__name__": "test"}
        exec(patch_mcp(altered), ns)
        self.assertEqual(ns["_tools"]()[-1]["name"], "vps_terminal_v2")


if __name__ == "__main__":
    unittest.main()
