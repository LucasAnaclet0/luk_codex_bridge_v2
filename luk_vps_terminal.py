"""LUK Terminal V2: audited shell as ubuntu, no root escalation or token access.

A terminal run is user-approved and is NOT a security sandbox; commands can
modify any resource writable by the MCP service account. Never advertise this
as host attestation, unrestricted root, or a replacement for Linux permissions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

BRIDGE = Path("/home/ubuntu/luk_codex_bridge_v2")
V2 = Path("/home/ubuntu/luk_bot_trader_v2")
HOME = Path("/home/ubuntu")
SHELL = Path("/bin/bash")
NO_PRIVS = Path("/usr/bin/setpriv")
AUDIT_DIR = BRIDGE / ".luk-terminal-v2"
AUDIT_LOG = AUDIT_DIR / "execution_audit.jsonl"
CWD_MAP = {"v2": V2, "bridge": BRIDGE, "home": HOME}
CONFIRMATION = "AUTORIZO_TERMINAL_V2_UBUNTU"
MAX_COMMAND = 4096
MAX_RETURN_BYTES = 16384
MAX_TIMEOUT = 90
REDACT_TERMS = (
    "api_key", "apikey", "api-secret", "apisecret", "secret", "password",
    "passwd", "passphrase", "token", "credential", "authorization",
    "bearer", "private_key", "privatekey", "client_secret"
)


def _identity(cwd: str) -> Path:
    if os.getuid() != 1001 or os.geteuid() != 1001:
        raise RuntimeError("NO_GO: expected unprivileged ubuntu uid 1001")
    if cwd not in CWD_MAP:
        raise ValueError("unsupported working-directory selector")
    for location in (BRIDGE, V2, HOME, SHELL, NO_PRIVS, CWD_MAP[cwd]):
        if location.is_symlink() or not location.exists():
            raise RuntimeError("NO_GO: fixed VPS location is missing or symlinked")
    if not SHELL.is_file() or not NO_PRIVS.is_file():
        raise RuntimeError("NO_GO: fixed nonprivileged shell launcher unavailable")
    return CWD_MAP[cwd]


def _args(args: dict) -> tuple[str, str, int]:
    if type(args) is not dict:
        raise ValueError("invalid terminal parameters")
    action = args.get("action")
    if action == "status":
        if set(args) != {"action"}:
            raise ValueError("unexpected status fields")
        return action, "v2", 10
    if action not in ("plan", "execute"):
        raise ValueError("unsupported terminal action")
    expected = ({"action", "command", "cwd", "timeout_seconds"} if action == "plan"
                else {"action", "command", "cwd", "timeout_seconds",
                      "expected_plan_sha256", "confirmation"})
    if set(args) != expected:
        raise ValueError("unexpected or missing terminal fields")
    command = args["command"]
    timeout = args["timeout_seconds"]
    cwd = args["cwd"]
    if (type(command) is not str or not command.strip() or
            len(command.encode("utf-8")) > MAX_COMMAND or
            "\x00" in command or "\r" in command):
        raise ValueError("invalid or oversized shell command")
    if type(timeout) is not int or timeout < 1 or timeout > MAX_TIMEOUT:
        raise ValueError("timeout outside approved bounds")
    if type(cwd) is not str or cwd not in CWD_MAP:
        raise ValueError("invalid working directory")
    if action == "execute":
        digest = args["expected_plan_sha256"]
        if (type(digest) is not str or
                not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("expected_plan_sha256 invalid")
        if args["confirmation"] != CONFIRMATION:
            raise PermissionError("explicit terminal execution confirmation missing")
        if digest != _plan_hash(command, cwd, timeout):
            raise RuntimeError("NO_GO: command differs from its approved plan")
    return action, cwd, timeout


def _plan_hash(command: str, cwd: str, timeout: int) -> str:
    material = json.dumps({"action": "execute", "command": command, "cwd": cwd,
                           "timeout_seconds": timeout}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _redact_output(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    return "\n".join(
        "[REDACTED: credential-related output line]"
        if any(tag in line.lower() for tag in REDACT_TERMS) else line
        for line in text.split("\n")
    )


def _audit(item: dict) -> None:
    if AUDIT_DIR.is_symlink() or AUDIT_LOG.is_symlink():
        raise RuntimeError("NO_GO: terminal audit path is symlinked")
    AUDIT_DIR.mkdir(mode=0o700, parents=False, exist_ok=True)
    os.chmod(AUDIT_DIR, 0o700)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(AUDIT_LOG, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.write(json.dumps(item, ensure_ascii=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        raise


def run(args: dict) -> dict:
    action, cwd, timeout = _args(args)
    workdir = _identity(cwd)
    if action == "status":
        return {"status": "READY", "uid": os.getuid(), "cwd_selectors": sorted(CWD_MAP),
                "max_timeout_seconds": MAX_TIMEOUT,
                "shell": "/usr/bin/setpriv --no-new-privs /bin/bash --noprofile --norc -c",
                "host_attested": False, "root_access": False,
                "security_note": "Commands execute as ubuntu; no filesystem sandbox."}
    command = args["command"]
    plan_sha = _plan_hash(command, cwd, timeout)
    if action == "plan":
        return {"status": "PLANNED_NOT_EXECUTED", "expected_plan_sha256": plan_sha,
                "cwd": str(workdir), "timeout_seconds": timeout,
                "command_bytes": len(command.encode("utf-8")),
                "host_attested": False, "root_access": False}
    audit = {"at": datetime.now(timezone.utc).isoformat(), "action": "execute",
             "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
             "plan_sha256": plan_sha, "cwd": cwd, "timeout_seconds": timeout}
    try:
        # Record intention BEFORE executing. Never store plaintext command or secrets.
        _audit(dict(audit, result="started"))
        env = {"HOME": str(HOME), "USER": "ubuntu", "LOGNAME": "ubuntu",
               "PATH": "/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        with tempfile.TemporaryFile() as stream:
            process = subprocess.Popen([str(NO_PRIVS), "--no-new-privs",
                                       str(SHELL), "--noprofile", "--norc", "-c", command],
                                       cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
                                       stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True, close_fds=True)
            timed_out = False
            try:
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return_code = process.wait(timeout=3)
            stream.flush()
            output_size = stream.tell()
            stream.seek(0)
            output = _redact_output(stream.read(MAX_RETURN_BYTES))
        audit.update(result="timeout" if timed_out else "finished",
                     returncode=int(return_code), output_bytes=output_size)
        return {"status": "TIMED_OUT" if timed_out else "EXECUTED",
                "cwd": str(workdir), "returncode": int(return_code),
                "timed_out": timed_out, "output": output,
                "output_truncated": output_size > MAX_RETURN_BYTES,
                "output_bytes": int(output_size), "host_attested": False,
                "root_access": False, "plan_sha256": plan_sha}
    except (OSError, subprocess.SubprocessError) as exc:
        audit.update(result="error", error_type=type(exc).__name__)
        return {"status": "EXECUTION_ERROR",
                "error_type": type(exc).__name__, "host_attested": False}
    finally:
        _audit(audit)
