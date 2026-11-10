"""LUK Terminal V2 controlled code promotion and MAIN lifecycle, running as ubuntu.

Never an arbitrary shell. The existing authenticated MCP server is the access gate;
every write requires a per-action confirmation, exact source hash, and fail-closed
process checks. This module does not verify exchange balance or authorize trades.
"""
from __future__ import annotations

import ast
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

BOT = Path("/home/ubuntu/luk_bot_trader")
CAMPAIGN_ROOT = Path("/home/ubuntu/luk_campaigns/campanha_100_20261108_230704_124781")
BRIDGE = Path("/home/ubuntu/luk_codex_bridge_v2")
DEV = BRIDGE / ".luk-main-dev-staging"
STATE = BRIDGE / ".luk-controlled-ops"
CONTROL = STATE / "control.lock"
BACKUPS = STATE / "backups"
MAIN_LOCK = BOT / ".demo_bot.lock"
PYTHON = BOT / "venv/bin/python"
ALLOWED = frozenset(("main.py", "trader_testnet.py", "trading.py",
                     "scanner_multi_crypto.py", "config.py", "memoria.py",
                     "gerenciador.py"))
ACTIONS = frozenset(("status", "dev_verify", "main_stop", "main_start",
                     "promote_dev", "rollback_dev"))
CONFIRM = {
    "main_stop": "AUTORIZO_PARAR_MAIN_DEMO",
    "main_start": "AUTORIZO_INICIAR_MAIN_DEMO",
    "promote_dev": "AUTORIZO_ALTERAR_CODIGO_MAIN_DEMO",
    "rollback_dev": "AUTORIZO_RESTAURAR_CODIGO_MAIN_DEMO",
}
MAX_SOURCE = 500_000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source(base: Path, name: str) -> Path:
    if type(name) is not str or name not in ALLOWED:
        raise ValueError("file not allowlisted")
    if base.is_symlink():
        raise RuntimeError("unsafe base directory")
    path = base / name
    if path.is_symlink():
        raise RuntimeError("symlink refused")
    return path


def _read(path: Path) -> bytes:
    if (not path.is_file() or path.is_symlink()
            or path.stat().st_size > MAX_SOURCE):
        raise RuntimeError("file missing, linked or oversized")
    return path.read_bytes()


def _pid_info(pid: int) -> dict | None:
    path = Path("/proc") / str(pid)
    try:
        argv = (path / "cmdline").read_bytes().split(b"\x00")
        if not any(x == b"main.py" or x.endswith(b"/main.py") for x in argv):
            return None
        cwd = os.readlink(path / "cwd")
        if cwd != str(BOT):
            return None
        raw = (path / "stat").read_text()
        start_ticks = int(raw.rsplit(")", 1)[1].strip().split()[19])
        if start_ticks <= 0:
            return None
        return {"pid": pid, "start_ticks": start_ticks, "cwd": cwd}
    except (PermissionError, ProcessLookupError, OSError, ValueError, IndexError):
        return None


def _main_processes() -> list[dict]:
    found = []
    try:
        entries = os.scandir("/proc")
        for entry in entries:
            if entry.name.isdecimal():
                info = _pid_info(int(entry.name))
                if info:
                    found.append(info)
        entries.close()
    except OSError:
        raise RuntimeError("cannot inspect process namespace")
    return sorted(found, key=lambda x: x["pid"])


def _campaign_processes() -> list[int]:
    found = []
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                proc = Path("/proc") / entry.name
                try:
                    argv = (proc / "cmdline").read_bytes().split(b"\\x00")
                    if not any(x == b"main.py" or x.endswith(b"/main.py") for x in argv):
                        continue
                    cwd = os.readlink(proc / "cwd")
                    if cwd.startswith(str(CAMPAIGN_ROOT) + "/"):
                        found.append(int(entry.name))
                except (OSError, ValueError):
                    continue
    except OSError:
        raise RuntimeError("cannot inspect campaign processes") from None
    return found


def _flock_owned(pid: int) -> bool:
    """Require a kernel FLOCK report for the exact expected process."""
    try:
        result = subprocess.run(
            ["/usr/bin/lslocks", "--noheadings", "--output", "PID,TYPE,PATH"],
            capture_output=True, text=True, timeout=5, check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and parts[0] == str(pid) and parts[1] == "FLOCK" and parts[2] == str(MAIN_LOCK):
            return True
    return False


def _load_nonsecret_flags() -> tuple[bool, bool]:
    """Read only DEMO booleans; never return .env content or credential values."""
    path = BOT / ".env"
    if path.is_symlink() or not path.is_file():
        return False, False
    if path.stat().st_size > 200_000:
        return False, False
    flags = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(USAR_TESTNET|MODO_TESTE)\s*=\s*([^#\r\n]+)", line)
            if m:
                flags[m.group(1)] = m.group(2).strip().strip("'\"").lower()
    except (OSError, UnicodeError):
        return False, False
    return flags.get("USAR_TESTNET") in ("1", "true", "yes"), flags.get("MODO_TESTE") in ("1", "true", "yes")


def _status() -> dict:
    procs = _main_processes()
    flag1, flag2 = _load_nonsecret_flags()
    return {
        "main_processes": [{**p, "kernel_flock_confirmed": _flock_owned(p["pid"])} for p in procs[:4]],
        "main_count": len(procs),
        "campaign_process_count": len(_campaign_processes()),
        "demo_flags_verified": bool(flag1 and flag2),
        "main_binary_present": PYTHON.is_file() and not PYTHON.is_symlink(),
        "host_attested": False,
        "unrestricted_shell": False,
        "ready_to_trade": False,
    }


def _check_caller():
    if os.getuid() != 1001 or os.geteuid() != 1001:
        raise RuntimeError("controlled operations require ubuntu uid 1001")
    if STATE.is_symlink() or BRIDGE.is_symlink() or BOT.is_symlink() or DEV.is_symlink():
        raise RuntimeError("unsafe symlink")
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE, 0o700)
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    os.chmod(BACKUPS, 0o700)


def _atomic(path: Path, content: bytes, mode=0o600):
    fd, temp = tempfile.mkstemp(prefix=".luk-promotion-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _check_stopped():
    if _main_processes():
        raise RuntimeError("NO_GO: MAIN still running; no code writes")
    try:
        locks = subprocess.run(
            ["/usr/bin/lslocks", "--noheadings", "--output", "PID,TYPE,PATH"],
            capture_output=True, text=True, timeout=5, check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("NO_GO: kernel lock table unavailable") from None
    if locks.returncode:
        raise RuntimeError("NO_GO: kernel locks not verified")
    for entry in locks.stdout.splitlines():
        parts = entry.split(maxsplit=2)
        if len(parts) == 3 and parts[1] == "FLOCK" and parts[2] == str(MAIN_LOCK):
            raise RuntimeError("NO_GO: MAIN lock still held")
    if not PYTHON.is_file():
        raise RuntimeError("python interpreter unavailable")


def _verify_dev(name: str, expected_live_sha: str | None = None) -> dict:
    live, staged = _source(BOT, name), _source(DEV, name)
    before, after = _read(live), _read(staged)
    if expected_live_sha is not None and _sha(before) != expected_live_sha:
        raise RuntimeError("stale live source hash")
    ast.parse(after.decode("utf-8"), filename=name)
    if before == after:
        raise RuntimeError("no differences from live source")
    return {"file": name, "live_sha256": _sha(before), "staged_sha256": _sha(after),
            "syntax_ok": True, "tests_executed": False, "main_changed": False}


def _stop_main(args: dict) -> dict:
    if set(args) != {"action", "confirmation", "expected_pid", "expected_start_ticks"}:
        raise ValueError("stop requires explicit PID and start ticks")
    pid, ticks = args["expected_pid"], args["expected_start_ticks"]
    if type(pid) is not int or type(ticks) is not int or pid < 2 or ticks < 1:
        raise ValueError("bad PID or start ticks")
    procs = _main_processes()
    if len(procs) != 1 or procs[0]["pid"] != pid or procs[0]["start_ticks"] != ticks:
        raise RuntimeError("NO_GO: MAIN identity mismatch or duplicate process")
    if not _flock_owned(pid):
        raise RuntimeError("NO_GO: MAIN kernel lock ownership unverified")
    os.kill(pid, signal.SIGINT)
    for _ in range(30):
        if _pid_info(pid) is None:
            break
        time.sleep(0.5)
    if _pid_info(pid) is not None:
        raise RuntimeError("SIGINT sent but MAIN still alive; no forced kill")
    if _main_processes():
        raise RuntimeError("MAIN replacement process exists; stop inconclusive")
    return {"status": "MAIN_STOPPED", "pid": pid, "main_files_changed": False}


def _start_main() -> dict:
    _check_stopped()
    if _campaign_processes():
        raise RuntimeError("NO_GO: campaign process active; refusing concurrent MAIN start")
    if not all(_load_nonsecret_flags()):
        raise RuntimeError("NO_GO: DEMO flags not explicitly set true in main .env")
    if not (BOT / "demo_execution.py").is_file():
        raise RuntimeError("NO_GO: demo_execution.py unavailable")
    try:
        trading = _read(BOT / "trading.py").decode("utf-8")
    except (OSError, ValueError, UnicodeError):
        raise RuntimeError("NO_GO: cannot inspect trading.py") from None
    if not all(x in trading for x in (
            "exchange.set_sandbox_mode(True)", "assert_demo(exchange)",
            "cfg.USAR_TESTNET is not True")):
        raise RuntimeError("NO_GO: mandatory OKX DEMO guards absent in trading.py")
    logdir = BOT / "logs"
    if not logdir.is_dir() or logdir.is_symlink():
        raise RuntimeError("NO_GO: logs directory unavailable")
    try:
        real_python = PYTHON.resolve(strict=True)
        trusted_python = str(real_python).startswith("/usr/bin/python")
        if not real_python.is_file() or not trusted_python:
            raise RuntimeError("NO_GO: virtualenv Python resolves outside trusted OS interpreter")
    except (OSError, RuntimeError):
        raise RuntimeError("NO_GO: virtualenv interpreter unverified") from None
    env = {"HOME": "/home/ubuntu", "PATH": "/home/ubuntu/luk_bot_trader/venv/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONUNBUFFERED": "1"}
    logpath = logdir / "luk-operator-launch.log"
    if logpath.is_symlink():
        raise RuntimeError("unsafe logfile")
    with logpath.open("ab") as log:
        proc = subprocess.Popen([str(PYTHON), "-u", "main.py"],
                                cwd=str(BOT), env=env, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                close_fds=True, start_new_session=True)
    time.sleep(2)
    identity = _pid_info(proc.pid)
    if proc.poll() is not None or identity is None:
        raise RuntimeError("MAIN start did not remain active; inspect operator launch log")
    return {"status": "MAIN_LAUNCHED_UNVERIFIED", "pid": proc.pid,
            "start_ticks": identity["start_ticks"], "healthy": False,
            "main_files_changed": False}


def _promote(args: dict) -> dict:
    if set(args) != {"action", "confirmation", "file", "expected_sha256", "staged_sha256"}:
        raise ValueError("promote requires exact source hashes")
    name = args["file"]
    expected, staged_expected = args["expected_sha256"], args["staged_sha256"]
    if (type(expected) is not str or type(staged_expected) is not str or
            not re.fullmatch(r"[0-9a-f]{64}", expected) or
            not re.fullmatch(r"[0-9a-f]{64}", staged_expected)):
        raise ValueError("invalid SHA-256")
    _check_stopped()
    result = _verify_dev(name, expected)
    if result["staged_sha256"] != staged_expected:
        raise RuntimeError("DEV hash drift; deployment refused")
    before = _read(_source(BOT, name))
    after = _read(_source(DEV, name))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = BACKUPS / (name + "." + stamp + ".bak")
    _atomic(backup, before)
    record = {"file": name, "backup": str(backup), "previous_sha256": _sha(before),
              "promoted_sha256": _sha(after), "timestamp": stamp}
    live_path = _source(BOT, name)
    try:
        _atomic(live_path, after, mode=0o644)
        _atomic(STATE / "last_promotion.json",
                json.dumps(record, sort_keys=True).encode("utf-8"))
    except Exception:
        _atomic(live_path, before, mode=0o644)
        raise
    return {"status": "MAIN_CODE_PROMOTED_NO_RESTART", "file": name,
            "backup": str(backup), "previous_sha256": _sha(before),
            "installed_sha256": _sha(after), "tests_executed": False}


def _rollback(args: dict) -> dict:
    if set(args) != {"action", "confirmation", "file"}:
        raise ValueError("rollback needs exact file")
    _check_stopped()
    record_path = STATE / "last_promotion.json"
    if not record_path.is_file() or record_path.is_symlink():
        raise RuntimeError("no promotion record")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    name = args["file"]
    if record.get("file") != name:
        raise RuntimeError("rollback file does not match last promotion")
    backup = Path(record["backup"])
    if backup.parent != BACKUPS or backup.is_symlink():
        raise RuntimeError("invalid backup path")
    restored = _read(backup)
    current = _read(_source(BOT, name))
    if _sha(restored) != record["previous_sha256"] or _sha(current) != record["promoted_sha256"]:
        raise RuntimeError("backup or live source diverged")
    _atomic(_source(BOT, name), restored, mode=0o644)
    record_path.unlink()
    return {"status": "MAIN_CODE_ROLLED_BACK", "file": name,
            "restored_sha256": _sha(restored), "main_restarted": False}


def run(action: str, arguments: dict) -> dict:
    if type(action) is not str or action not in ACTIONS or type(arguments) is not dict:
        raise ValueError("unknown operator action")
    if arguments.get("action") != action:
        raise ValueError("action mismatch")
    _check_caller()
    with CONTROL.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if action == "status":
            if set(arguments) != {"action"}:
                raise ValueError("status is read only")
            result = _status()
        elif action == "dev_verify":
            if set(arguments) != {"action", "file"}:
                raise ValueError("dev_verify needs a file")
            result = _verify_dev(arguments["file"])
        else:
            if arguments.get("confirmation") != CONFIRM.get(action):
                raise PermissionError("exact approval phrase required for requested mutation")
            if action == "main_stop":
                result = _stop_main(arguments)
            elif action == "main_start":
                if set(arguments) != {"action", "confirmation"}:
                    raise ValueError("start does not accept extra arguments")
                result = _start_main()
            elif action == "promote_dev":
                result = _promote(arguments)
            elif action == "rollback_dev":
                result = _rollback(arguments)
            else:
                raise RuntimeError("unhandled action")
    return {"action": action, "scope": "FIXED_LUK_MAIN_ONLY", "host_attested": False,
            "no_real_orders_authorized": True, "result": result}
