"""No-network tests for the MCP fixed read-only diagnostic surface."""
import unittest
from types import SimpleNamespace

from terminal_readonly import MAX_STDOUT_CHARS, PROBES, run_diagnostic
from mcp_server import _tool_payload, _tools


class TerminalReadOnlyTests(unittest.TestCase):
    def test_unknown_operation_fails_closed(self):
        for value in ("ls", "system;id", "", None, {}, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    run_diagnostic(value, runner=lambda *a, **k: None)

    def test_no_shell_and_no_caller_arguments(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        payload = run_diagnostic("resources", runner=fake_run)
        self.assertEqual(len(calls), 2)
        self.assertFalse(payload["host_attested"])
        self.assertFalse(payload["go_for_mutations"])
        for argv, kwargs in calls:
            self.assertEqual(argv[0], "/usr/bin/" + argv[0].split("/")[-1])
            self.assertFalse(kwargs["shell"])
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["cwd"], "/")
            self.assertEqual(kwargs["timeout"], 5)
            self.assertTrue(kwargs["capture_output"])
        self.assertEqual(payload["observations"][0]["stdout"], "ok")

    def test_output_bound(self):
        result = run_diagnostic(
            "system",
            runner=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="x" * 9000, stderr="y" * 9000
            ),
        )
        for observation in result["observations"]:
            self.assertLessEqual(len(observation["stdout"]), MAX_STDOUT_CHARS + 12)
            self.assertIn("[TRUNCATED]", observation["stdout"])
            self.assertIn("[TRUNCATED]", observation["stderr"])

    def test_constant_argv_and_allowlist(self):
        self.assertEqual(set(PROBES), {"system", "resources", "bridge_services"})
        for commands in PROBES.values():
            for _, argv in commands:
                self.assertIsInstance(argv, tuple)
                self.assertTrue(argv[0].startswith("/usr/bin/"))
                self.assertTrue(all(isinstance(x, str) for x in argv))

    def test_mcp_rejects_free_form_commands(self):
        with self.assertRaises(ValueError):
            _tool_payload("vps_diagnostics_readonly", {"operation": "system", "command": "id"})
        with self.assertRaises(ValueError):
            _tool_payload("vps_diagnostics_readonly", {"operation": "sh"})
        advertised = {tool["name"]: tool for tool in _tools()}
        self.assertTrue(advertised["vps_diagnostics_readonly"]["annotations"]["readOnlyHint"])
        self.assertFalse("command" in advertised["vps_diagnostics_readonly"]["inputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main()
