#!/usr/bin/env python3
"""One-time LUK read-only MCP tool bootstrap, pinned to an observed VPS snapshot.

Fail closed on drift. Only luk-codex-mcp.service can be restarted.
NO arbitrary command execution, bot mutations or MAIN service restarts.
"""
import ast
import datetime
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BRIDGE = Path("/home/ubuntu/luk_codex_bridge_v2")
MCP = BRIDGE / "mcp_server.py"
MODULE = BRIDGE / "terminal_readonly.py"
SERVICE = "luk-codex-mcp.service"
EXPECTED_MCP_SHA256 = "7aee6bc2217a263bb72f499d39ce61fed11c8e9a74b08b19c8b10caf99e1f971"
EXPECTED_MODULE_GIT_SHA1 = "6a3a5db5c05bc7428929707e110016914be0b3ef"
MODULE_SOURCE = "\"\"\"Fixed, read-only diagnostic probes for an *unverified* process namespace.\n\nThis module is NOT an arbitrary command executor or a proof of host identity.\nOnly constant argv vectors are allowed. Never accept paths, commands, flags,\nor environment variables from remote callers.\n\"\"\"\nfrom __future__ import annotations\n\nimport subprocess\n\n# Constant commands, no shell and no user-controlled arguments.\nPROBES = {\n    \"system\": (\n        (\"architecture\", (\"/usr/bin/uname\", \"-sm\")),\n        (\"uptime\", (\"/usr/bin/uptime\", \"-p\")),\n    ),\n    \"resources\": (\n        (\"memory\", (\"/usr/bin/free\", \"-h\")),\n        (\"disk\", (\"/usr/bin/df\", \"-h\", \"/\")),\n    ),\n    \"bridge_services\": (\n        (\"bridge_states\", (\n            \"/usr/bin/systemctl\", \"is-active\",\n            \"luk-codex-mcp.service\",\n            \"luk-codex-dashboard.service\",\n            \"luk-codex-notifier.service\",\n            \"luk-host-agent.service\",\n        )),\n    ),\n}\nMAX_STDOUT_CHARS = 2400\nMAX_STDERR_CHARS = 300\nTIMEOUT_SECONDS = 5\n\n\ndef _bounded(value: str, limit: int) -> str:\n    \"\"\"Keep the beginning of diagnostic output and mark truncation explicitly.\"\"\"\n    return value[:limit] + (\"\\n[TRUNCATED]\" if len(value) > limit else \"\")\n\n\ndef run_diagnostic(operation: str, *, runner=None) -> dict:\n    \"\"\"Run a fixed, read-only probe in the caller's CURRENT namespace.\n\n    Nothing here attests that the caller is on the host, or that the namespaced\n    systemd/process view corresponds to the real VPS. GO remains false.\n    \"\"\"\n    if type(operation) is not str or operation not in PROBES:\n        raise ValueError(\"unsupported diagnostic operation\")\n    if runner is None:\n        runner = subprocess.run\n    observations = []\n    for label, argv in PROBES[operation]:\n        try:\n            completed = runner(\n                list(argv),\n                shell=False,\n                check=False,\n                capture_output=True,\n                text=True,\n                encoding=\"utf-8\",\n                errors=\"replace\",\n                timeout=TIMEOUT_SECONDS,\n                cwd=\"/\",\n                env={\"PATH\": \"/usr/bin:/bin\", \"LANG\": \"C\", \"LC_ALL\": \"C\"},\n            )\n            observations.append({\n                \"name\": label,\n                \"returncode\": completed.returncode,\n                \"stdout\": _bounded(completed.stdout, MAX_STDOUT_CHARS),\n                \"stderr\": _bounded(completed.stderr, MAX_STDERR_CHARS),\n            })\n        except (OSError, subprocess.TimeoutExpired) as exc:\n            observations.append({\n                \"name\": label,\n                \"returncode\": None,\n                \"stdout\": \"\",\n                \"stderr\": type(exc).__name__,\n            })\n    return {\n        \"operation\": operation,\n        \"scope\": \"CURRENT_PROCESS_NAMESPACE_ONLY\",\n        \"host_attested\": False,\n        \"host_health_verified\": False,\n        \"go_for_mutations\": False,\n        \"observations\": observations,\n    }\n"

MARKER = "# LUK_TERMINAL_READONLY_BOOTSTRAP_V1"
WRAPPER = '''
# LUK_TERMINAL_READONLY_BOOTSTRAP_V1
import json as _luk_terminal_json
from terminal_readonly import PROBES as _luk_terminal_probes
from terminal_readonly import run_diagnostic as _luk_terminal_run_diagnostic

_luk_original_tools = _tools
def _tools():
    tools = _luk_original_tools()
    if any(tool.get("name") == "vps_diagnostics_readonly" for tool in tools):
        raise RuntimeError("duplicate tool: vps_diagnostics_readonly")
    return [*tools, {
        "name": "vps_diagnostics_readonly",
        "description": (
            "Fixed read-only VPS diagnostic probes in an unverified process namespace. "
            "No shell, caller-controlled commands, host attestation or mutations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"operation": {"type": "string", "enum": sorted(_luk_terminal_probes)}},
            "required": ["operation"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": True}
    }]

_luk_original_tool_payload = _tool_payload
def _tool_payload(name, arguments):
    if name != "vps_diagnostics_readonly":
        return _luk_original_tool_payload(name, arguments)
    if not isinstance(arguments, dict) or set(arguments) != {"operation"}:
        raise ValueError("invalid tool arguments")
    if type(arguments["operation"]) is not str or arguments["operation"] not in _luk_terminal_probes:
        raise ValueError("invalid diagnostic operation")
    payload = _luk_terminal_run_diagnostic(arguments["operation"])
    return {
        "content": [{"type": "text", "text": _luk_terminal_json.dumps(payload, ensure_ascii=False)}],
        "isError": False
    }

'''

def fail(reason):
    raise RuntimeError(reason)

def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=12, **kwargs)

def replace_bytes(path, content, mode=0o644):
    fd, temp = tempfile.mkstemp(prefix=".luk-terminal-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def patched_source(original):
    if MARKER in original:
        fail("marker already present, no changes")
    tree = ast.parse(original)
    definitions = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    if not {"_tools", "_tool_payload", "Handler", "main"} <= definitions:
        fail("unknown MCP layout: required definitions missing")
    guards = []
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            guards.append(node)
    if len(guards) != 1 or tree.body[-1] is not guards[0]:
        fail("unknown MCP layout: final main guard not unique")
    lines = original.splitlines(keepends=True)
    lines.insert(guards[0].lineno - 1, WRAPPER + "\n")
    candidate = "".join(lines)
    compile(candidate, str(MCP), "exec")
    return candidate

def main():
    if sys.argv[1:] != ["--apply"]:
        print("Usage: python3 bootstrap_terminal_readonly.py --apply")
        sys.exit(2)
    if os.getuid() != 1001 or os.geteuid() != 1001:
        fail("run only as ubuntu uid=1001; no sudo")
    if not MCP.is_file() or MODULE.exists() or MCP.is_symlink():
        fail("expected original file absent, module exists, or symlink detected")
    original_bytes = MCP.read_bytes()
    old_hash = hashlib.sha256(original_bytes).hexdigest()
    if old_hash != EXPECTED_MCP_SHA256:
        fail("live mcp_server.py differs from inspected snapshot: " + old_hash)
    module_bytes = MODULE_SOURCE.encode("utf-8")
    blob_sha = hashlib.sha1(
        ("blob " + str(len(module_bytes)) + "\0").encode("ascii") + module_bytes
    ).hexdigest()
    if blob_sha != EXPECTED_MODULE_GIT_SHA1:
        fail("bundled module integrity check failed")
    original_text = original_bytes.decode("utf-8")
    candidate = patched_source(original_text)
    compile(MODULE_SOURCE, str(MODULE), "exec")
    if not BRIDGE.is_dir() or not os.access(BRIDGE, os.W_OK):
        fail("bridge directory not writable by ubuntu")
    state = run(["systemctl", "show", SERVICE,
                 "-p", "LoadState", "-p", "ActiveState", "-p", "User",
                 "-p", "ExecStart", "-p", "WorkingDirectory"])
    if state.returncode:
        fail("cannot inspect MCP systemd unit")
    info = state.stdout
    if (
        "LoadState=loaded" not in info
        or "ActiveState=active" not in info
        or "User=ubuntu" not in info
        or "mcp_server.py" not in info
        or str(BRIDGE) not in info
    ):
        fail("MCP unit identity not confirmed; no changes (details suppressed)")
    sudo = run(["sudo", "-n", "true"])
    if sudo.returncode:
        fail("non-interactive sudo not available; no changes")

    backup_root = BRIDGE / ".luk-terminal-backups"
    backup_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backup_root, 0o700)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / ("mcp_server.py." + timestamp + ".bak")
    if backup.exists():
        fail("backup already exists")
    replace_bytes(backup, original_bytes, mode=0o600)
    print("BACKUP_CREATED", backup)

    applied = False
    try:
        applied = True
        replace_bytes(MODULE, module_bytes)
        replace_bytes(MCP, candidate.encode("utf-8"), mode=0o644)
        # Verify register/reject behavior without running the server or making
        # any subprocess diagnostic call.
        verify_code = (
            'import mcp_server\n'
            'from mcp_server import _tools, _tool_payload\n'
            'tools = _tools()\n'
            'assert sum(x["name"] == "vps_diagnostics_readonly" for x in tools) == 1\n'
            'for bad in [{"operation": "shell"}, {"operation": "system", "command": "id"}]:\n'
            '    try: _tool_payload("vps_diagnostics_readonly", bad)\n'
            '    except ValueError: pass\n'
            '    else: raise AssertionError("unsafe input accepted")\n'
        )
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/ubuntu",
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C"}
        canary = run([sys.executable, "-B", "-c", verify_code], cwd=str(BRIDGE), env=env)
        if canary.returncode:
            fail("offline import/registration canary failed")
        print("OFFLINE_CANARY_OK")

        restart = run(["sudo", "-n", "systemctl", "restart", SERVICE])
        if restart.returncode:
            fail("MCP restart failed")
        # A 401 without auth is expected when the authenticated MCP endpoint responds.
        # Never read or print the secret.
        if str(BRIDGE) not in sys.path:
            sys.path.insert(0, str(BRIDGE))
        from config import MCP_PORT
        authenticated_gate_ready = False
        for attempt in range(8):
            active = run(["systemctl", "is-active", SERVICE])
            if active.returncode == 0 and active.stdout.strip() == "active":
                probe = urllib.request.Request("http://127.0.0.1:%d/mcp" % MCP_PORT)
                try:
                    urllib.request.urlopen(probe, timeout=2).close()
                    fail("unexpected unauthenticated MCP success")
                except urllib.error.HTTPError as error:
                    if error.code == 401:
                        authenticated_gate_ready = True
                        break
                except (OSError, TimeoutError):
                    pass
            time.sleep(1)
        if not authenticated_gate_ready:
            fail("MCP authentication heartbeat failed after restart")
        print("MCP_RESTART_AND_AUTH_HEARTBEAT_OK")
        print("INSTALLED_READONLY_TOOL=true")
        print("NEW_TOOL=vps_diagnostics_readonly")
        print("HOST_ATTESTED=false")
        print("MUTATIONS_ALLOWED=false")
        print("BOT_SERVICES_CHANGED=false")
        print("NEXT_STEP=refresh ChatGPT bridge tool definitions")
    except Exception:
        if applied:
            replace_bytes(MCP, original_bytes, mode=0o644)
            if MODULE.exists():
                MODULE.unlink()
            run(["sudo", "-n", "systemctl", "restart", SERVICE])
            print("ROLLBACK_ATTEMPTED")
        raise

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("NO_GO:", type(error).__name__, str(error), file=sys.stderr)
        sys.exit(1)
