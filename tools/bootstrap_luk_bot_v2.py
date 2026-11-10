#!/usr/bin/env python3
"""Install independent LUK Bot Trader V2 integration into existing authenticated MCP.

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
MODULE = BRIDGE / "luk_bot_v2_operator.py"
SERVICE = "luk-codex-mcp.service"
MARKER = "# LUK_BOT_TRADER_V2_PLUGIN"
URL = ("https://raw.githubusercontent.com/LucasAnaclet0/luk_codex_bridge_v2/"
       "44beb8a6cd70c94219f9660f173c867bdb2c0132/luk_bot_v2_operator.py")
EXPECTED_BLOB = "8f9f0ed6f8e768be3aeecb730e472e39314b255d"

HELPERS = """
# LUK_BOT_TRADER_V2_PLUGIN
import json as _luk_v2_json
from luk_bot_v2_operator import ACTIONS as _luk_v2_actions
from luk_bot_v2_operator import SAFE_FILES as _luk_v2_files
from luk_bot_v2_operator import run as _luk_v2_run

def _luk_v2_tool_spec():
    return {
        "name": "vps_bot_trader_v2",
        "description": (
            "Fixed-path LUK Bot Trader V2 observability and strictly approved "
            "maintenance. Inspect systemd, processes, code inventory and campaign "
            "SQLite counts; stage syntax-checked changes; stop/start fixed systemd "
            "unit or promote/rollback code only with explicit confirmation and "
            "identity/hash safeguards. No arbitrary shell, no V1 access."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(_luk_v2_actions)},
                "file": {"type": "string", "maxLength": 70},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 100},
                "old_text": {"type": "string", "minLength": 1, "maxLength": 6000},
                "new_text": {"type": "string", "maxLength": 6000},
                "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "staged_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "expected_pid": {"type": "integer", "minimum": 2},
                "expected_ticks": {"type": "integer", "minimum": 1},
                "confirmation": {"type": "string", "maxLength": 80}
            },
            "required": ["action"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True}
    }

def _luk_v2_dispatch(arguments):
    if type(arguments) is not dict or arguments.get("action") not in _luk_v2_actions:
        raise ValueError("invalid V2 tool request")
    allowed = {"action", "file", "start_line", "line_count", "old_text", "new_text",
               "expected_sha256", "staged_sha256", "expected_pid",
               "expected_ticks", "confirmation"}
    if set(arguments) - allowed:
        raise ValueError("unexpected V2 tool field")
    payload = _luk_v2_run(arguments)
    return {"content": [{"type": "text", "text": _luk_v2_json.dumps(
        payload, ensure_ascii=False)}], "isError": False}
"""


def fail(message):
    raise RuntimeError(message)


def run(argv, **kwargs):
    return subprocess.run(argv, timeout=15, capture_output=True, text=True, **kwargs)


def atomic_write(path, content, mode=0o644):
    fd, tmp = tempfile.mkstemp(prefix=".luk-bot-v2-install-", dir=str(path.parent))
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
    """Insert V2 in the oldest MCP handlers; do not add a recursive wrapper."""
    if MARKER in source:
        fail("operator already installed")
    if "# LUK_CONTROLLED_OPERATOR_V1" not in source:
        fail("existing controlled operator marker not found")
    tree = ast.parse(source)
    funcs = {name: [node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == name]
             for name in ("_tools", "_tool_payload", "main")}
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    if not all(funcs.values()) or "Handler" not in classes:
        fail("required MCP definitions missing")
    guards = [node for node in tree.body if isinstance(node, ast.If)
              and isinstance(node.test, ast.Compare)
              and isinstance(node.test.left, ast.Name)
              and node.test.left.id == "__name__"
              and len(node.test.comparators) == 1
              and isinstance(node.test.comparators[0], ast.Constant)
              and node.test.comparators[0].value == "__main__"]
    if len(guards) != 1 or tree.body[-1] is not guards[0]:
        fail("MCP main guard shape unexpected")
    base_tools = funcs["_tools"][0]
    base_payload = funcs["_tool_payload"][0]
    if (not base_tools.body or not isinstance(base_tools.body[0], ast.Return)
            or not isinstance(base_tools.body[0].value, ast.List)
            or not base_payload.body):
        fail("original MCP function layout unrecognized")
    lines = source.splitlines(keepends=True)
    return_line = base_tools.body[0].lineno
    if "return [" not in lines[return_line - 1]:
        fail("base tool registry layout changed")
    insertions = [
        (return_line, "        _luk_v2_tool_spec()," + chr(10)),
        (base_payload.body[0].lineno - 1,
         '    if name == "vps_bot_trader_v2":' + chr(10)
         + '        return _luk_v2_dispatch(arguments)' + chr(10)),
        (guards[0].lineno - 1, HELPERS + chr(10)),
    ]
    for position, code in sorted(insertions, key=lambda pair: pair[0], reverse=True):
        lines.insert(position, code)
    patched = "".join(lines)
    compile(patched, str(MCP), "exec")
    return patched.encode("utf-8")


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
        fail("usage: python3 -B bootstrap_luk_bot_v2.py --apply")
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

    # Fail BEFORE touching the authenticated MCP service or creating a backup.
    # The test runs in a disposable directory; local bridge imports stay readonly.
    canary = (
        "from mcp_server import _tools, _tool_payload\n"
        "assert len([x for x in _tools() if x['name']=='vps_bot_trader_v2']) == 1\n"
        "for payload in ({'action':'shell'}, {'action':'service_stop','command':'kill -9 1'}):\n"
        "  try: _tool_payload('vps_bot_trader_v2', payload)\n"
        "  except ValueError: pass\n"
        "  else: raise AssertionError('unsafe arguments accepted')\n"
    )
    with tempfile.TemporaryDirectory(prefix="luk-bot-v2-preflight-") as folder:
        test_dir = Path(folder)
        (test_dir / "mcp_server.py").write_bytes(patch)
        (test_dir / "luk_bot_v2_operator.py").write_bytes(module_bytes)
        env = {"HOME": "/home/ubuntu", "PATH": "/usr/local/bin:/usr/bin:/bin",
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C",
               "PYTHONPATH": str(BRIDGE)}
        result = run([sys.executable, "-B", "-c", canary], cwd=folder, env=env)
        if result.returncode:
            lines = result.stderr.splitlines()
            last_line = lines[-1] if lines else "stderr_unavailable"
            # Provide bounded diagnostic without echoing environment or command output.
            import re as _re
            last_line = _re.sub(
                r"(?i)(bearer|token|secret|password|api[_-]?key)\\s*[:=]\\s*[^\\s]+",
                r"\\1=[REDACTED]", last_line
            )
            fail("offline canary failed: " + last_line[:260])
    print("OFFLINE_REGISTRATION_OK", flush=True)
    backups = BRIDGE / ".luk-terminal-backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backups, 0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backups / ("mcp_server.py.bot-v2." + stamp + ".bak")
    if backup.exists():
        fail("backup conflict")
    atomic_write(backup, previous, mode=0o600)
    print("MCP_BACKUP_CREATED", backup, flush=True)
    changed = False
    try:
        changed = True
        atomic_write(MODULE, module_bytes)
        atomic_write(MCP, patch)
        restart = run(["sudo", "-n", "systemctl", "restart", SERVICE])
        if restart.returncode:
            fail("MCP service restart failed")
        if not verify_heartbeat():
            fail("MCP auth heartbeat failed")
        print("MCP_AUTH_HEARTBEAT_OK")
        print("INSTALLED=vps_bot_trader_v2")
        print("V2_MUTATIONS_REQUIRE_EXPLICIT_APPROVAL=true")
        print("V1_CHANGED=false")
        print("V2_RESTARTED=false")
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
