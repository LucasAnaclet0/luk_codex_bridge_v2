"""Offline tests of snapshot-pinned, fail-closed MCP bootstrap."""
import ast
import hashlib
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "bootstrap_terminal_readonly.py"
spec = importlib.util.spec_from_file_location("luk_terminal_bootstrap_dev", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

STUB = '''
def _tools():
    return [{"name": "get_task", "inputSchema": {}}]

def _tool_payload(name, arguments):
    return {"name": name, "args": arguments}

class Handler:
    pass

def main():
    return "server"

if __name__ == "__main__":
    main()
'''


class BootstrapOfflineTests(unittest.TestCase):
    def test_bundled_module_hash_matches_pinned_git_blob(self):
        data = module.MODULE_SOURCE.encode("utf-8")
        computed = hashlib.sha1(("blob %d\0" % len(data)).encode() + data).hexdigest()
        self.assertEqual(computed, module.EXPECTED_MODULE_GIT_SHA1)

    def test_valid_layout_is_additive(self):
        source = module.patched_source(STUB)
        self.assertIn(module.MARKER, source)
        ast.parse(source)
        self.assertIn('def _tool_payload(name, arguments):', source)
        self.assertEqual(source.count('def _tools():'), 2)
        self.assertTrue(source.index(module.MARKER) < source.index('if __name__ == "__main__":'))

    def test_rerun_denied(self):
        source = module.patched_source(STUB)
        with self.assertRaises(RuntimeError):
            module.patched_source(source)

    def test_unknown_layout_denied(self):
        for source in ("x = 1\n", STUB.replace("class Handler:", "class Unknown:"),
                       STUB.replace('if __name__ == "__main__":', 'if False:')):
            with self.subTest(source=source[:40]):
                with self.assertRaises(RuntimeError):
                    module.patched_source(source)

    def test_tool_invocation_denies_caller_supplied_shell(self):
        from types import ModuleType
        import sys
        fake = ModuleType("terminal_readonly")
        fake.PROBES = {"system": ((),)}
        fake.run_diagnostic = lambda operation: {"operation": operation, "host_attested": False}
        previous = sys.modules.get("terminal_readonly")
        sys.modules["terminal_readonly"] = fake
        try:
            ns = {"__name__": "dev_offline"}
            exec(compile(module.patched_source(STUB), "<stub>", "exec"), ns)
            names = {x["name"] for x in ns["_tools"]()}
            self.assertIn("vps_diagnostics_readonly", names)
            self.assertIn("get_task", names)
            for bad in ({"operation": "system", "command": "id"}, {"operation": "sh"}):
                with self.assertRaises(ValueError):
                    ns["_tool_payload"]("vps_diagnostics_readonly", bad)
            payload = ns["_tool_payload"]("vps_diagnostics_readonly", {"operation": "system"})
            self.assertFalse(payload["isError"])
            self.assertEqual(ns["_tool_payload"]("get_task", {"task_id": "x"})["name"], "get_task")
        finally:
            if previous is not None:
                sys.modules["terminal_readonly"] = previous
            else:
                sys.modules.pop("terminal_readonly", None)


if __name__ == "__main__":
    unittest.main()
