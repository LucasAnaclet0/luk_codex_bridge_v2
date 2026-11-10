#!/usr/bin/env python3
"""Install LUK scoped operator: fixed-path code deploy + guarded MAIN lifecycle.

User runs once on VPS as ubuntu (no sudo). This installer edits only the
authenticated MCP service, not the MAIN bot, and verifies offline registration
before restarting MCP. It will attempt MCP-only rollback after failure.
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BRIDGE = Path("/home/ubuntu/luk_codex_bridge_v2")
MCP = BRIDGE / "mcp_server.py"
MODULE = BRIDGE / "luk_remote_operator_v1.py"
SERVICE = "luk-codex-mcp.service"
MARKER = "# LUK_CONTROLLED_OPERATOR_V1"
URL = ("https://raw.githubusercontent.com/LucasAnaclet0/luk_codex_bridge_v2/"
       "6b094f2e646320755b48671f58e0200def998d2a/luk_remote_operator_v1.py")
EXPECTED_BLOB = "9ca808b8e2466e9bfbd5053aa5c1505ead092b72"

WRAPPER = """
# LUK_CONTROLLED_OPERATOR_V1
import json as _luk_operator_json
from luk_remote_operator_v1 import ACTIONS as _luk_operator_actions
from luk_remote_operator_v1 import ALLOWED as _luk_operator_files
from luk_remote_operator_v1 import run as _luk_operator_run

_luk_operator_prior_tools = _tools
def _tools():
    items = _luk_operator_prior_tools()
    if any(item.get("name") == "vps_controlled_operator_v1" for item in items):
        raise RuntimeError("duplicate controlled operator tool")
    return [*items, {
        "name": "vps_controlled_operator_v1",
        "description": (
            "Scoped, authenticated LUK Demo MAIN maintenance. Status and DEV AST "
            "verification; stop/start by verified process PID and kernel lock; "
            "promote or rollback only hash-matched DEV files when MAIN is stopped. "
            "No arbitrary shell. Mutating calls require exact user approval. "
            "No exchange reconciliation or trade readiness claim."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(_luk_operator_actions)},
                "confirmation": {"type": "string", "maxLength": 80},
                "file": {"type": "string", "enum": sorted(_luk_operator_files)},
                "expected_pid": {"type": "integer", "minimum": 2},
                "expected_start_ticks": {"type": "integer", "minimum": 1},
                "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "staged_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}
            },
            "required": ["action"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True}
    }]

_luk_operator_prior_payload = _tool_payload
def _tool_payload(name, arguments):
    if name != "vps_controlled_operator_v1":
        return _luk_operator_prior_payload(name, arguments)
    if type(arguments) is not dict or arguments.get("action") not in _luk_operator_actions:
        raise ValueError("invalid controlled operator request")
    allowed = {"action", "confirmation", "file", "expected_pid",
               "expected_start_ticks", "expected_sha256", "staged_sha256"}
    if set(arguments) - allowed:
        raise ValueError("unexpected controlled operator field")
    payload = _luk_operator_run(arguments["action"], arguments)
    return {"content": [{"type": "text", "text": _luk_operator_json.dumps(
        payload, ensure_ascii=False)}], "isError": False}
"""


def fail(message):
    raise RuntimeError(message)


def run(argv, **kwargs):
    return subprocess.run(argv, timeout=15, capture_output=True, text=True, **kwargs)


def atomic_write(path, content, mode=0o644):
    fd, tmp = tempfile.mkstemp(prefix=".luk-operator-install-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def blob_hash(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\x00" + data).hexdigest()


def patch_mcp(source):
    if MARKER in source:
        fail("operator already installed")
    if "# LUK_TERMINAL_V2_MAIN_MAINTENANCE" not in source:
        fail("V2 bootstrap marker not found: refusing unknown server")
    tree = ast.parse(source)
    defined = {x.name for x in tree.body if isinstance(x, ast.FunctionDef)}
    classes = {x.name for x in tree.body if isinstance(x, ast.ClassDef)}
    if not {"_tools", "_tool_payload", "main"} <= defined or "Handler" not in classes:
        fail("required MCP definitions missing")
    guards = [x for x in tree.body if isinstance(x, ast.If)
              and isinstance(x.test, ast.Compare)
              and isinstance(x.test.left, ast.Name) and x.test.left.id == "__name__"
              and len(x.test.comparators) == 1
              and isinstance(x.test.comparators[0], ast.Constant)
              and x.test.comparators[0].value == "__main__"]
    if len(guards) != 1 or tree.body[-1] is not guards[0]:
        fail("MCP main guard shape unexpected")
    lines = source.splitlines(keepends=True)
    lines.insert(guards[0].lineno - 1, WRAPPER + "\n")
    result = "".join(lines)
    compile(result, str(MCP), "exec")
    return result.encode("utf-8")


def verify_service():
    state = run(["systemctl", "show", SERVICE,
                 "-p", "LoadState", "-p", "ActiveState", "-p", "User",
                 "-p", "ExecStart", "-p", "WorkingDirectory"])
    if state.returncode or not all(value in state.stdout for value in
       ("LoadState=loaded", "ActiveState=active", "User=ubuntu",
        "mcp_server.py", str(BRIDGE))):
        fail("MCP service identity mismatch")
    if run(["sudo", "-n", "true"]).returncode:
        fail("sudo noninteractive auth missing")


def verify_heartbeat():
    if str(BRIDGE) not in sys.path:
        sys.path.insert(0, str(BRIDGE))
    from config import MCP_PORT
    for _ in range(8):
        active = run(["systemctl", "is-active", SERVICE])
        if active.stdout.strip() == "active":
            req = urllib.request.Request("http://127.0.0.1:%d/mcp" % MCP_PORT)
            try:
                urllib.request.urlopen(req, timeout=2).close()
                fail("unauthenticated request unexpectedly permitted")
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    return True
            except (OSError, TimeoutError):
                pass
        time.sleep(1)
    return False


def main():
    if sys.argv[1:] != ["--apply"]:
        fail("usage: python3 -B bootstrap_luk_controlled_operator.py --apply")
    if os.getuid() != 1001 or os.geteuid() != 1001:
        fail("must run as ubuntu uid=1001 (without sudo)")
    if not BRIDGE.is_dir() or MCP.is_symlink() or MODULE.exists() or not MCP.is_file():
        fail("unexpected bridge layout or already installed")
    # Do not attempt to assert host identity: access to /proc/1/ns/pid is
    # restricted on this VPS. Operational writes use process + kernel-lock checks.
    verify_service()
    previous = MCP.read_bytes()
    patch = patch_mcp(previous.decode("utf-8"))

    req = urllib.request.Request(URL, headers={"User-Agent": "luk-operator"})
    with urllib.request.urlopen(req, timeout=10) as response:
        module_bytes = response.read(150_000)
    if blob_hash(module_bytes) != EXPECTED_BLOB:
        fail("module integrity hash mismatch")
    compile(module_bytes, str(MODULE), "exec")

    backups = BRIDGE / ".luk-terminal-backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backups, 0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backups / ("mcp_server.py.operator." + stamp + ".bak")
    if backup.exists():
        fail("backup conflict")
    atomic_write(backup, previous, mode=0o600)
    print("MCP_BACKUP_CREATED", backup, flush=True)
    changed = False
    try:
        changed = True
        atomic_write(MODULE, module_bytes)
        atomic_write(MCP, patch)
        canary = (
            "from mcp_server import _tools, _tool_payload\n"
            "assert len([x for x in _tools() if x['name']=='vps_controlled_operator_v1']) == 1\n"
            "for payload in ({'action':'shell'}, {'action':'main_stop','command':'kill -9 1'}):\n"
            "  try: _tool_payload('vps_controlled_operator_v1', payload)\n"
            "  except ValueError: pass\n"
            "  else: raise AssertionError('unsafe arguments accepted')\n"
        )
        env = {"HOME": "/home/ubuntu", "PATH": "/usr/local/bin:/usr/bin:/bin",
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C"}
        result = run([sys.executable, "-B", "-c", canary], cwd=str(BRIDGE), env=env)
        if result.returncode:
            fail("offline canary failed")
        print("OFFLINE_REGISTRATION_OK", flush=True)
        restart = run(["sudo", "-n", "systemctl", "restart", SERVICE])
        if restart.returncode:
            fail("MCP service restart failed")
        if not verify_heartbeat():
            fail("MCP auth heartbeat failed")
        print("MCP_AUTH_HEARTBEAT_OK")
        print("INSTALLED=vps_controlled_operator_v1")
        print("CODE_WRITE_ALLOWED_ONLY_AFTER_EXPLICIT_APPROVAL=true")
        print("MAIN_CHANGED=false")
        print("MAIN_RESTARTED=false")
        print("HOST_ATTESTED=false")
        print("NEXT_STEP=refresh ChatGPT tool definitions")
    except Exception:
        if changed:
            atomic_write(MCP, previous)
            if MODULE.exists():
                MODULE.unlink()
            run(["sudo", "-n", "systemctl", "restart", SERVICE])
            print("MCP_ROLLBACK_ATTEMPTED", flush=True)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("NO_GO", type(exc).__name__, str(exc), file=sys.stderr)
        sys.exit(1)
