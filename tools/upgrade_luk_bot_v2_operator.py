#!/usr/bin/env python3
"""Pinned, rollback-safe V2 operator upgrade. Never touches trade bots.

This is a one-time SSH bootstrap: updates only the existing MCP V2 Python
operator module and restarts only the authenticated MCP service.
"""
from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

BRIDGE = Path("/home/ubuntu/luk_codex_bridge_v2")
MCP = BRIDGE / "mcp_server.py"
OPERATOR = BRIDGE / "luk_bot_v2_operator.py"
SERVICE = "luk-codex-mcp.service"
OLD_BLOB = "8f9f0ed6f8e768be3aeecb730e472e39314b255d"
NEW_BLOB = "348fde8c92a211b8a90d4189388b6692d06cc7c0"
URL = ("https://raw.githubusercontent.com/LucasAnaclet0/luk_codex_bridge_v2/"
       "9f0c316549f96445a7c257244d43962ab9637a4e/"
       "luk_bot_v2_operator.py")


def git_blob(payload):
    return hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\x00" + payload).hexdigest()


def command(args, *, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=18)


def fail(code):
    raise RuntimeError(code)


def atomic(path, payload, mode=0o644):
    fd, temp = tempfile.mkstemp(prefix=".luk-v2-operator-update-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def verify_mcp_service():
    cp = command(["/usr/bin/systemctl", "show", SERVICE,
                  "-p", "LoadState", "-p", "ActiveState", "-p", "User",
                  "-p", "WorkingDirectory", "-p", "ExecStart"])
    if cp.returncode:
        fail("MCP_SERVICE_READ_UNAVAILABLE")
    data = dict(x.split("=", 1) for x in cp.stdout.splitlines() if "=" in x)
    if (data.get("LoadState") != "loaded" or
            data.get("ActiveState") != "active" or
            data.get("User") != "ubuntu" or
            data.get("WorkingDirectory") != str(BRIDGE) or
            "mcp_server.py" not in data.get("ExecStart", "")):
        fail("MCP_SERVICE_IDENTITY_MISMATCH")
    if command(["/usr/bin/sudo", "-n", "true"]).returncode:
        fail("MCP_SUDO_MISSING")


def auth_heartbeat():
    sys.path.insert(0, str(BRIDGE))
    from config import MCP_PORT
    try:
        request = urllib.request.Request("http://127.0.0.1:%d/mcp" % MCP_PORT)
        urllib.request.urlopen(request, timeout=3).close()
        return False
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except (OSError, TimeoutError):
        return False


def canary(new_module):
    with tempfile.TemporaryDirectory(prefix="luk-v2-module-preflight-") as temp:
        test = Path(temp)
        (test / "mcp_server.py").write_bytes(MCP.read_bytes())
        (test / "luk_bot_v2_operator.py").write_bytes(new_module)
        script = (
            "from mcp_server import _tools, _tool_payload\n"
            "from luk_bot_v2_operator import _valid_v2_exec\n"
            "names=[item['name'] for item in _tools()]\n"
            "assert names.count('vps_bot_trader_v2')==1\n"
            "assert names.count('vps_controlled_operator_v1')==1\n"
            "actions=next(item['inputSchema']['properties']['action']['enum']\n"
            "             for item in _tools() if item['name']=='vps_bot_trader_v2')\n"
            "assert 'service_restart' in actions\n"
            "assert not _valid_v2_exec('invalid command')\n"
            "try: _tool_payload('vps_bot_trader_v2', {'action':'shell'})\n"
            "except ValueError: pass\n"
            "else: raise AssertionError('unsafe arguments accepted')\n"
            "print('V2_MODULE_CANARY_OK')\n"
        )
        env = {"HOME": "/home/ubuntu", "PATH": "/usr/local/bin:/usr/bin:/bin",
               "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(BRIDGE)}
        cp = command([sys.executable, "-B", "-c", script], cwd=temp, env=env)
        if cp.returncode or "V2_MODULE_CANARY_OK" not in cp.stdout:
            fail("V2_MODULE_OFFLINE_CANARY_FAILED")
    print("V2_MODULE_OFFLINE_CANARY_OK", flush=True)


def main():
    if sys.argv[1:] != ["--apply"]:
        fail("USAGE_APPLY_REQUIRED")
    if (os.getuid(), os.geteuid()) != (1001, 1001):
        fail("MUST_RUN_AS_UBUNTU_UID_1001")
    if (not BRIDGE.is_dir() or BRIDGE.is_symlink() or
            not MCP.is_file() or MCP.is_symlink() or
            not OPERATOR.is_file() or OPERATOR.is_symlink()):
        fail("MCP_LAYOUT_UNEXPECTED")
    if "# LUK_BOT_TRADER_V2_PLUGIN" not in MCP.read_text():
        fail("V2_TOOL_REGISTRATION_MISSING")
    previous = OPERATOR.read_bytes()
    current_blob = git_blob(previous)
    if current_blob == NEW_BLOB:
        print("ALREADY_UPDATED=true", flush=True)
        return
    if current_blob != OLD_BLOB:
        fail("V2_OPERATOR_EXISTING_BLOB_MISMATCH")
    verify_mcp_service()
    request = urllib.request.Request(URL, headers={"User-Agent": "luk-v2-update"})
    with urllib.request.urlopen(request, timeout=12) as response:
        new = response.read(150000)
    if git_blob(new) != NEW_BLOB:
        fail("V2_OPERATOR_NEW_BLOB_MISMATCH")
    compile(new, str(OPERATOR), "exec")
    canary(new)  # no live file changes occur before this passes
    backups = BRIDGE / ".luk-terminal-backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    if backups.is_symlink():
        fail("BACKUP_DIR_SYMLINK")
    os.chmod(backups, 0o700)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backups / ("luk_bot_v2_operator.py." + timestamp + ".bak")
    if backup.exists():
        fail("BACKUP_ALREADY_EXISTS")
    atomic(backup, previous, mode=0o600)
    print("V2_OPERATOR_BACKUP_CREATED", str(backup), flush=True)
    changed = False
    try:
        atomic(OPERATOR, new)
        changed = True
        cp = command(["/usr/bin/sudo", "-n", "/usr/bin/systemctl",
                      "restart", SERVICE])
        if cp.returncode or not auth_heartbeat():
            fail("MCP_POST_UPDATE_HEARTBEAT_FAILED")
        print("V2_OPERATOR_UPDATED=true", flush=True)
        print("MCP_AUTH_HEARTBEAT_OK", flush=True)
        print("MAIN_V1_CHANGED=false", flush=True)
        print("V2_BOT_SERVICE_RESTARTED=false", flush=True)
        print("V2_TRADES_EXECUTED=false", flush=True)
        print("HOST_ATTESTED=false", flush=True)
        print("NEXT_STEP=refresh ChatGPT plugin tools", flush=True)
    except Exception:
        if changed:
            atomic(OPERATOR, previous)
            rollback = command(["/usr/bin/sudo", "-n", "/usr/bin/systemctl",
                                "restart", SERVICE])
            print("MCP_ROLLBACK_OK=" +
                  str(rollback.returncode == 0 and auth_heartbeat()).lower(),
                  flush=True)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("NO_GO", type(exc).__name__, str(exc), file=sys.stderr)
        sys.exit(1)
