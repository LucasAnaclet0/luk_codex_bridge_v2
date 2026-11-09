#!/usr/bin/env python3
"""Install a bounded LUK Terminal V2 MAIN-inspection/isolated-DEV tool.

One-time update of MCP endpoint only. The bot, its data and its services are
not modified or restarted. Rolls MCP back if canary/heartbeat fails.
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
MODULE = BRIDGE / "luk_main_maintenance_v2.py"
SERVICE = "luk-codex-mcp.service"
MARKER = "# LUK_TERMINAL_V2_MAIN_MAINTENANCE"
PINNED_MODULE_URL = (
    "https://raw.githubusercontent.com/LucasAnaclet0/luk_codex_bridge_v2/"
    "05a93fd4cd7bb4f9d3c88bc35aabb94590a5669e/luk_main_maintenance_v2.py"
)
EXPECTED_MODULE_GIT_BLOB = "33e095c019ee2cc98efd0ab8c70bf9f0fe1ba6b7"

WRAPPER = """
# LUK_TERMINAL_V2_MAIN_MAINTENANCE
import json as _luk_v2_json
from luk_main_maintenance_v2 import ACTIONS as _luk_v2_actions
from luk_main_maintenance_v2 import ALLOWED_SOURCE as _luk_v2_files
from luk_main_maintenance_v2 import run as _luk_v2_run

_luk_v2_prior_tools = _tools
def _tools():
    tools = _luk_v2_prior_tools()
    if any(item.get("name") == "vps_main_maintenance_v2" for item in tools):
        raise RuntimeError("duplicate vps_main_maintenance_v2")
    return [*tools, {
        "name": "vps_main_maintenance_v2",
        "description": (
            "Read bounded MAIN status/errors/positions and prepare syntax-checked "
            "changes in an isolated DEV staging directory ONLY. No live bot writes, "
            "no bot restarts, no host attestation or deploy authority."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(_luk_v2_actions)},
                "file": {"type": "string", "enum": sorted(_luk_v2_files)},
                "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "old_text": {"type": "string", "minLength": 1, "maxLength": 6000},
                "new_text": {"type": "string", "maxLength": 6000}
            },
            "required": ["action"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": False}
    }]

_luk_v2_prior_payload = _tool_payload
def _tool_payload(name, arguments):
    if name != "vps_main_maintenance_v2":
        return _luk_v2_prior_payload(name, arguments)
    if type(arguments) is not dict or arguments.get("action") not in _luk_v2_actions:
        raise ValueError("invalid maintenance arguments")
    payload = _luk_v2_run(arguments["action"], arguments)
    return {
        "content": [{"type": "text", "text": _luk_v2_json.dumps(payload, ensure_ascii=False)}],
        "isError": False
    }
"""

def fail(why):
    raise RuntimeError(why)

def cmd(args, **kwargs):
    return subprocess.run(args, timeout=15, capture_output=True, text=True, **kwargs)

def git_blob_sha(source):
    return hashlib.sha1(b"blob " + str(len(source)).encode() + b"\x00" + source).hexdigest()

def atomic_write(path, content, mode=0o644):
    fd, tmp = tempfile.mkstemp(prefix=".luk-terminal-v2-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def patch_main_source(source):
    if MARKER in source:
        fail("already patched; no action")
    parsed = ast.parse(source)
    functions = {n.name for n in parsed.body if isinstance(n, ast.FunctionDef)}
    classes = {n.name for n in parsed.body if isinstance(n, ast.ClassDef)}
    if not {"_tools", "_tool_payload", "main"} <= functions or "Handler" not in classes:
        fail("unknown server layout")
    guards = [
        n for n in parsed.body if isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name)
        and n.test.left.id == "__name__"
        and len(n.test.comparators) == 1
        and isinstance(n.test.comparators[0], ast.Constant)
        and n.test.comparators[0].value == "__main__"
    ]
    if len(guards) != 1 or parsed.body[-1] is not guards[0]:
        fail("unrecognized final main guard")
    lines = source.splitlines(keepends=True)
    lines.insert(guards[0].lineno - 1, WRAPPER + "\n")
    result = "".join(lines)
    compile(result, str(MCP), "exec")
    return result.encode("utf-8")

def check_service():
    status = cmd(["systemctl", "show", SERVICE,
                  "-p", "LoadState", "-p", "ActiveState", "-p", "User",
                  "-p", "ExecStart", "-p", "WorkingDirectory"])
    if status.returncode:
        fail("could not inspect MCP unit")
    out = status.stdout
    checks = ("LoadState=loaded", "ActiveState=active", "User=ubuntu",
              "mcp_server.py", str(BRIDGE))
    if not all(item in out for item in checks):
        fail("unexpected MCP unit identity")
    if cmd(["sudo", "-n", "true"]).returncode != 0:
        fail("sudo -n unavailable")

def heartbeat():
    if str(BRIDGE) not in sys.path:
        sys.path.insert(0, str(BRIDGE))
    from config import MCP_PORT
    for _ in range(9):
        if cmd(["systemctl", "is-active", SERVICE]).stdout.strip() == "active":
            request = urllib.request.Request(f"http://127.0.0.1:{MCP_PORT}/mcp")
            try:
                urllib.request.urlopen(request, timeout=2).close()
                fail("unauthenticated access unexpectedly succeeded")
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    return True
            except (OSError, TimeoutError):
                pass
        time.sleep(1)
    return False

def main():
    if sys.argv[1:] != ["--apply"]:
        fail("usage: python3 bootstrap_luk_terminal_v2.py --apply")
    if os.getuid() != 1001 or os.geteuid() != 1001:
        fail("must run as ubuntu uid 1001, without sudo")
    if (not BRIDGE.is_dir() or not MCP.is_file()
            or MCP.is_symlink() or MODULE.exists() or MODULE.is_symlink()):
        fail("unexpected bridge/MCP/module path")
    current = MCP.read_bytes()
    text = current.decode("utf-8")
    if MARKER in text:
        fail("V2 marker already installed")
    if "vps_diagnostics_readonly" not in text:
        fail("required V1 read-only feature missing; no changes")
    if os.readlink("/proc/self/ns/pid") != os.readlink("/proc/1/ns/pid"):
        fail("unexpected PID namespace; refusing installation")
    if Path("/proc/1/comm").read_text().strip() != "systemd":
        fail("unexpected PID 1")
    check_service()
    req = urllib.request.Request(PINNED_MODULE_URL, headers={"User-Agent": "luk-v2-bootstrap"})
    with urllib.request.urlopen(req, timeout=10) as res:
        module = res.read(200_000)
    if git_blob_sha(module) != EXPECTED_MODULE_GIT_BLOB:
        fail("downloaded module hash mismatch")
    compile(module, str(MODULE), "exec")
    patched = patch_main_source(text)
    if not os.access(BRIDGE, os.W_OK):
        fail("bridge directory not writable")

    backups = BRIDGE / ".luk-terminal-backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backups, 0o700)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backups / f"mcp_server.py.v2.{now}.bak"
    if backup.exists():
        fail("backup exists")
    atomic_write(backup, current, mode=0o600)
    print("MCP_BACKUP_CREATED", backup, flush=True)

    did_mutate = False
    try:
        did_mutate = True
        atomic_write(MODULE, module)
        atomic_write(MCP, patched)
        code = (
            "from mcp_server import _tools, _tool_payload\n"
            "assert len([x for x in _tools() if x['name']=='vps_main_maintenance_v2']) == 1\n"
            "for arg in ({'action':'shell'}, {'action':'snapshot','command':'cat /etc/shadow'}):\n"
            "  try: _tool_payload('vps_main_maintenance_v2', arg)\n"
            "  except ValueError: pass\n"
            "  else: raise AssertionError('unsafe argument accepted')\n"
            "from luk_main_maintenance_v2 import run\n"
            "assert run('dev_status')['action'] == 'dev_status'\n"
        )
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/ubuntu",
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C"}
        canary = cmd([sys.executable, "-B", "-c", code], cwd=str(BRIDGE), env=env)
        if canary.returncode != 0:
            fail("offline canary failed (output withheld)")
        print("OFFLINE_CANARY_OK", flush=True)
        if cmd(["sudo", "-n", "systemctl", "restart", SERVICE]).returncode != 0:
            fail("MCP restart failed")
        if not heartbeat():
            fail("MCP authenticated heartbeat failed")
        print("MCP_HEARTBEAT_OK", flush=True)
        print("INSTALLED=vps_main_maintenance_v2")
        print("MAIN_FILES_CHANGED=false")
        print("MAIN_RESTARTED=false")
        print("LIVE_DEPLOY_ALLOWED=false")
        print("HOST_ATTESTED=false")
        print("NEXT_STEP=refresh ChatGPT plugin tool definitions")
    except Exception:
        if did_mutate:
            atomic_write(MCP, current)
            try:
                MODULE.unlink()
            except OSError:
                pass
            cmd(["sudo", "-n", "systemctl", "restart", SERVICE])
            print("ROLLBACK_ATTEMPTED", flush=True)
        raise

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("NO_GO", type(exc).__name__, str(exc), file=sys.stderr)
        sys.exit(1)
