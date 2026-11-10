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
       "12479958db8a1f84b8e4d705909248eb0cae89ff/luk_bot_v2_operator.py")
EXPECTED_BLOB = "ab26c2e6b06a16c5cf9a0dbb01084f08173ea5c2"

WRAPPER = """
# LUK_BOT_TRADER_V2_PLUGIN
import json as _luk_v2_json
from luk_bot_v2_operator import ACTIONS as _luk_v2_actions
from luk_bot_v2_operator import SAFE_FILES as _luk_v2_files
from luk_bot_v2_operator import run as _luk_v2_run

_luk_v2_prior_tools = _tools
def _tools():
    items = _luk_v2_prior_tools()
    if any(item.get("name") == "vps_bot_trader_v2" for item in items):
        raise RuntimeError("duplicate bot V2 tool")
    return [*items, {
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
                "file": {"type": "string", "enum": sorted(_luk_v2_files)},
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
    }]

_luk_v2_prior_payload = _tool_payload
def _tool_payload(name, arguments):
    if name != "vps_bot_trader_v2":
        return _luk_v2_prior_payload(name, arguments)
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
    if MARKER in source:
        fail("operator already installed")
    if "# LUK_CONTROLLED_OPERATOR_V1" not in source:
        fail("existing controlled operator marker not found")
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
        canary = (
            "from mcp_server import _tools, _tool_payload\n"
            "assert len([x for x in _tools() if x['name']=='vps_bot_trader_v2']) == 1\n"
            "for payload in ({'action':'shell'}, {'action':'service_stop','command':'kill -9 1'}):\n"
            "  try: _tool_payload('vps_bot_trader_v2', payload)\n"
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
