#!/usr/bin/env python3
"""One-time, rollback-safe LUK Terminal V2 bootstrap for authenticated MCP.

Installs the audited ubuntu-user terminal operator and registers a separate
MCP tool, without touching/restarting either trading bot. Run as ubuntu.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
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
TERMINAL = BRIDGE / "luk_vps_terminal.py"
SERVICE = "luk-codex-mcp.service"
MARKER = "# LUK_TERMINAL_V2_PLUGIN"
PINNED_BLOB = "86f50c39c539f8d35bb2a45d750b65c931d069c2"
MODULE_URL = ("https://raw.githubusercontent.com/LucasAnaclet0/"
              "luk_codex_bridge_v2/1ec103c153e34c59024bc9f724c963528b87d576/"
              "luk_vps_terminal.py")

HELPERS = '''
# LUK_TERMINAL_V2_PLUGIN
import json as _luk_term_json
from luk_vps_terminal import run as _luk_term_run

def _luk_term_add_tool(existing):
    if not isinstance(existing, list):
        raise RuntimeError("LUK terminal MCP registry not a list")
    if any(isinstance(item, dict) and
           item.get("name") == "vps_terminal_v2" for item in existing):
        raise RuntimeError("duplicate terminal tool registration")
    return [*existing, _luk_term_tool_spec()]

def _luk_term_tool_spec():
    return {
        "name": "vps_terminal_v2",
        "description": (
            "LUK Terminal V2: authenticated, audited command execution on the "
            "user's VPS AS UNPRIVILEGED ubuntu (not root). Plan a shell command "
            "first and execute only with matching SHA256 plus explicit confirmation. "
            "Commands may read/write files and control services permitted by Linux; "
            "this is NOT a filesystem sandbox. Output is bounded/redacted. "
            "No automatic host attestation or token disclosure."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "plan", "execute"]},
                "command": {"type": "string", "minLength": 1, "maxLength": 4096},
                "cwd": {"type": "string", "enum": ["v2", "bridge", "home"]},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 90},
                "expected_plan_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "confirmation": {"type": "string",
                                 "enum": ["AUTORIZO_TERMINAL_V2_UBUNTU"]}
            },
            "required": ["action"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True}
    }

def _luk_term_dispatch(arguments):
    if type(arguments) is not dict:
        raise ValueError("invalid LUK terminal args")
    result = _luk_term_run(arguments)
    return {"content": [{"type": "text", "text":
             _luk_term_json.dumps(result, ensure_ascii=False)}], "isError": False}
'''


def fail(message: str):
    raise RuntimeError(message)


def git_blob(payload: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\x00" + payload).hexdigest()


def command(argv, *, timeout=18, cwd=None, env=None):
    return subprocess.run(argv, timeout=timeout, cwd=cwd, env=env,
                          capture_output=True, text=True)


def atomic_write(path: Path, payload: bytes, *, mode=0o644):
    fd, temporary = tempfile.mkstemp(prefix=".luk-terminal-v2-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def patch_mcp(source: str) -> bytes:
    """Wrap last active _tools returns and insert exact-name _tool_payload route."""
    if MARKER in source:
        fail("TERMINAL_ALREADY_REGISTERED")
    if "# LUK_BOT_TRADER_V2_PLUGIN" not in source:
        fail("EXISTING_V2_PLUGIN_MISSING")
    parsed = ast.parse(source)
    functions = {
        name: [node for node in parsed.body
               if isinstance(node, ast.FunctionDef) and node.name == name]
        for name in ("_tools", "_tool_payload")
    }
    if not all(functions.values()):
        fail("MCP_FUNCTIONS_MISSING")
    tools = functions["_tools"][-1]
    dispatch = functions["_tool_payload"][-1]
    if len(tools.args.args) != 0 or [
            arg.arg for arg in dispatch.args.args[:2]] != ["name", "arguments"]:
        fail("MCP_SIGNATURES_UNRECOGNIZED")
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
        fail("MCP_MAIN_GUARD_UNRECOGNIZED")
    class ReturnFinder(ast.NodeVisitor):
        def __init__(self):
            self.values = []

        def visit_FunctionDef(self, node):
            return

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

        def visit_Return(self, node):
            self.values.append(node)

    finder = ReturnFinder()
    for statement in tools.body:
        finder.visit(statement)
    if not finder.values or any(r.value is None for r in finder.values):
        fail("MCP_TOOLS_RETURN_UNRECOGNIZED")
    code = source.encode("utf-8")
    lines = code.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def bytepos(line: int, col: int) -> int:
        if not 1 <= line <= len(lines) or not 0 <= col <= len(lines[line - 1]):
            fail("MCP_POSITION_OUT_OF_RANGE")
        return offsets[line - 1] + col

    edits = []
    for item in finder.values:
        node = item.value
        start = bytepos(node.lineno, node.col_offset)
        end = bytepos(node.end_lineno, node.end_col_offset)
        if start >= end:
            fail("MCP_TOOLS_EMPTY_RETURN")
        edits.append((start, end, b"_luk_term_add_tool(" + code[start:end] + b")"))
    first = dispatch.body[0]
    if (isinstance(first, ast.Expr) and
            isinstance(first.value, ast.Constant) and
            isinstance(first.value.value, str)):
        if len(dispatch.body) < 2:
            fail("MCP_DISPATCH_ONLY_DOCSTRING")
        first = dispatch.body[1]
    if first.lineno == dispatch.lineno:
        fail("MCP_DISPATCH_SINGLE_LINE_UNSUPPORTED")
    indent = lines[first.lineno - 1][:first.col_offset]
    if not indent or indent.strip() or b"\t" in indent:
        fail("MCP_DISPATCH_INDENT_INVALID")
    branch = (indent + b'if name == "vps_terminal_v2":\n'
              + indent + b'    return _luk_term_dispatch(arguments)\n')
    edits.append((bytepos(first.lineno, 0), bytepos(first.lineno, 0), branch))
    guard_at = bytepos(guards[0].lineno, 0)
    edits.append((guard_at, guard_at, HELPERS.encode("utf-8") + b"\n"))
    for start, end, substitute in sorted(edits, key=lambda item: item[0],
                                         reverse=True):
        code = code[:start] + substitute + code[end:]
    compile(code.decode("utf-8"), str(MCP), "exec")
    return code


def verify_mcp_service():
    result = command(["/usr/bin/systemctl", "show", SERVICE,
                      "-p", "LoadState", "-p", "ActiveState", "-p", "User",
                      "-p", "WorkingDirectory", "-p", "ExecStart"])
    if result.returncode:
        fail("MCP_SERVICE_INACCESSIBLE")
    facts = dict(line.split("=", 1) for line in result.stdout.splitlines()
                 if "=" in line)
    if (facts.get("LoadState") != "loaded"
            or facts.get("ActiveState") != "active"
            or facts.get("User") != "ubuntu"
            or facts.get("WorkingDirectory") != str(BRIDGE)
            or "mcp_server.py" not in facts.get("ExecStart", "")):
        fail("MCP_SERVICE_IDENTITY_MISMATCH")
    if command(["/usr/bin/sudo", "-n", "true"]).returncode:
        fail("MCP_SUDO_NOT_AVAILABLE")


def auth_heartbeat(retries=20):
    sys.path.insert(0, str(BRIDGE))
    from config import MCP_PORT
    for attempt in range(retries):
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/mcp" % MCP_PORT)
            urllib.request.urlopen(req, timeout=2).close()
            return False
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return True
            return False
        except (OSError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(1)
    return False


def offline_canary(mcp_bytes: bytes, module: bytes):
    with tempfile.TemporaryDirectory(prefix="luk-terminal-v2-check-") as temp:
        temp = Path(temp)
        (temp / "mcp_server.py").write_bytes(mcp_bytes)
        (temp / "luk_vps_terminal.py").write_bytes(module)
        script = (
            "from mcp_server import _tools,_tool_payload\n"
            "registry=_tools()\n"
            "names=[item['name'] for item in registry]\n"
            "assert names.count('vps_terminal_v2')==1\n"
            "assert names.count('vps_bot_trader_v2')==1\n"
            "spec=next(x for x in registry if x['name']=='vps_terminal_v2')\n"
            "assert spec['inputSchema']['properties']['action']['enum']=="
            "['status','plan','execute']\n"
            "try: _tool_payload('vps_terminal_v2',{'action':'execute'})\n"
            "except ValueError: pass\n"
            "else: raise AssertionError('bad command accepted')\n"
            "print('TERMINAL_MCP_CANARY_OK')\n"
        )
        env = {"HOME": "/home/ubuntu", "PATH": "/usr/local/bin:/usr/bin:/bin",
               "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(temp) + ":" + str(BRIDGE)}
        outcome = command([sys.executable, "-B", "-c", script],
                          timeout=15, cwd=str(temp), env=env)
        if outcome.returncode or "TERMINAL_MCP_CANARY_OK" not in outcome.stdout:
            fail("TERMINAL_OFFLINE_CANARY_FAILED")
    print("TERMINAL_MCP_CANARY_OK", flush=True)


def main():
    if sys.argv[1:] != ["--apply"]:
        fail("USAGE_REQUIRES_APPLY")
    if os.getuid() != 1001 or os.geteuid() != 1001:
        fail("TERMINAL_BOOTSTRAP_REQUIRES_UBUNTU_UID_1001")
    if (not BRIDGE.is_dir() or BRIDGE.is_symlink()
            or not MCP.is_file() or MCP.is_symlink()
            or TERMINAL.is_symlink() or TERMINAL.exists()):
        fail("TERMINAL_MCP_LAYOUT_UNEXPECTED")
    source = MCP.read_text(encoding="utf-8")
    if MARKER in source:
        fail("TERMINAL_ALREADY_INSTALLED")
    verify_mcp_service()
    request = urllib.request.Request(MODULE_URL, headers={
        "User-Agent": "luk-terminal-v2-bootstrap"})
    with urllib.request.urlopen(request, timeout=12) as res:
        module = res.read(20000)
    if git_blob(module) != PINNED_BLOB:
        fail("TERMINAL_OPERATOR_BLOB_MISMATCH")
    compile(module, str(TERMINAL), "exec")
    patched = patch_mcp(source)
    offline_canary(patched, module)
    backups = BRIDGE / ".luk-terminal-backups"
    if backups.is_symlink():
        fail("TERMINAL_BACKUP_DIR_SYMLINK")
    backups.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backups, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backups / ("mcp_server.py." + stamp + ".terminal.bak")
    if backup.exists():
        fail("TERMINAL_BACKUP_EXISTS")
    atomic_write(backup, source.encode("utf-8"), mode=0o600)
    print("TERMINAL_MCP_BACKUP_CREATED", str(backup), flush=True)
    wrote_module = False
    wrote_mcp = False
    try:
        atomic_write(TERMINAL, module)
        wrote_module = True
        atomic_write(MCP, patched)
        wrote_mcp = True
        restarted = command(["/usr/bin/sudo", "-n", "/usr/bin/systemctl",
                             "restart", SERVICE], timeout=18)
        if restarted.returncode:
            fail("TERMINAL_MCP_RESTART_FAILED")
        if not auth_heartbeat():
            fail("TERMINAL_MCP_AUTH_HEARTBEAT_FAILED")
        print("TERMINAL_MCP_UPDATED=true", flush=True)
        print("TERMINAL_MODULE_INSTALLED=true", flush=True)
        print("MCP_AUTH_HEARTBEAT_OK", flush=True)
        print("V2_BOT_SERVICE_RESTARTED=false", flush=True)
        print("MAIN_V1_CHANGED=false", flush=True)
        print("TRADES_EXECUTED=false", flush=True)
        print("HOST_ATTESTED=false", flush=True)
        print("NEXT_STEP=refresh ChatGPT plugin tools", flush=True)
    except Exception:
        if wrote_mcp:
            atomic_write(MCP, source.encode("utf-8"))
        if wrote_module:
            TERMINAL.unlink(missing_ok=True)
        restart = command(["/usr/bin/sudo", "-n", "/usr/bin/systemctl",
                           "restart", SERVICE], timeout=18)
        if restart.returncode or not auth_heartbeat():
            print("CRITICAL_MCP_ROLLBACK_FAILED", flush=True)
        else:
            print("MCP_ROLLBACK_OK=true", flush=True)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("TERMINAL_NO_GO=" + type(exc).__name__ + ":" + str(exc), flush=True)
        raise SystemExit(1)
