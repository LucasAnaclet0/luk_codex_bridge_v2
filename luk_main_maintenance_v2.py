"""LUK Terminal V2: bounded MAIN telemetry and isolated, syntax-only DEV staging.

This is not host attestation or permission to deploy/restart MAIN. No shell,
no credential reads, no live bot writes and no exchange/network calls.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

BOT = Path("/home/ubuntu/luk_bot_trader")
STAGE = Path("/home/ubuntu/luk_codex_bridge_v2/.luk-main-dev-staging")
CAMPAIGN = Path("/home/ubuntu/luk_campaigns/campanha_100_20261108_230704_124781")
CAMPAIGN_STAGE = CAMPAIGN / "runtime_staging"
LOCK = BOT / ".demo_bot.lock"
ALLOWED_SOURCE = frozenset({
    "main.py", "trader_testnet.py", "trading.py", "scanner_multi_crypto.py",
    "config.py", "memoria.py", "gerenciador.py",
})
ACTIONS = frozenset({
    "snapshot", "positions", "errors", "activity", "dev_status",
    "dev_stage_replace", "dev_validate", "dev_discard",
    "campaign_100_status", "source_read",
})
READ_ACTIONS = frozenset({"snapshot", "positions", "errors", "activity", "dev_status", "campaign_100_status"})
MAX_SOURCE = 500_000
MAX_LOG_BYTES = 196_608
MAX_PATCH_CHARS = 6_000
ERROR_TERMS = ("ERROR", "CRITICAL", "OrderNotFound", "abaixo do mínimo", "RECONCILIACAO")
ACTIVITY_TERMS = ("Scan concluído", "Ciclo #", "Decisão:", "COMPRA CONFIRMADA",
                  "VENDA REAL EXECUTADA", "STOP-WATCHDOG")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_file(base: Path, name: str) -> Path:
    if type(name) is not str or name not in ALLOWED_SOURCE:
        raise ValueError("source file not allowlisted")
    path = base / name
    if base.is_symlink() or path.is_symlink():
        raise ValueError("symlinks not allowed")
    return path


def _read_source(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_SOURCE:
        raise ValueError("source unavailable or too large")
    return path.read_bytes()


def _read_positions() -> dict:
    path = BOT / "posicoes_abertas.json"
    if path.is_symlink():
        return {"status": "UNAVAILABLE", "reason": "symlink"}
    if not path.is_file():
        return {"status": "MISSING"}
    try:
        if path.stat().st_size > 2_000_000:
            raise ValueError("too large")
        parsed = json.loads(path.read_text(encoding="utf-8"))
        positions = parsed.get("positions", parsed) if isinstance(parsed, dict) else {}
        if not isinstance(positions, dict):
            raise ValueError("invalid positions schema")
        keys = ("simbolo", "quantidade", "preco_entrada", "valor_investido",
                "stop_loss", "take_profit", "aberta_em", "atualizado_em")
        records = []
        for sym, position in list(positions.items())[:25]:
            if not isinstance(position, dict):
                continue
            row = {"symbol": str(sym)[:30]}
            for key in keys:
                val = position.get(key)
                if type(val) in (float, int, str) and len(str(val)) < 100:
                    row[key] = val
            records.append(row)
        return {"status": "OK", "count": len(positions), "records": records,
                "mtime_age_seconds": max(0, int(time.time() - path.stat().st_mtime))}
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as e:
        return {"status": "UNAVAILABLE", "reason": type(e).__name__}


def _process_info() -> dict:
    candidates = []
    root = Path("/proc")
    try:
        entries = list(root.iterdir())
    except OSError:
        return {"status": "UNAVAILABLE", "candidates": []}
    for proc in entries:
        if not proc.name.isdecimal():
            continue
        try:
            args = (proc / "cmdline").read_bytes().split(b"\x00")
            if not any(arg == b"main.py" or arg.endswith(b"/main.py") for arg in args):
                continue
            if os.readlink(proc / "cwd") != str(BOT):
                continue
            owned_fd = []
            for fd in (proc / "fd").iterdir():
                try:
                    if os.readlink(fd) == str(LOCK):
                        owned_fd.append(fd.name)
                except OSError:
                    continue
            candidates.append({"pid": int(proc.name), "lock_fd_seen": bool(owned_fd),
                               "lock_fd_count": len(owned_fd)})
        except (OSError, ValueError):
            continue
    return {"status": "OBSERVED_ONLY", "candidates": candidates[:10],
            "candidate_count": len(candidates),
            "lock_ownership_verified": False,
            "note": "Open FD alone is not proof of an active flock."}


def _latest_log() -> tuple[Path | None, bytes]:
    log_dir = BOT / "logs"
    if not log_dir.is_dir() or log_dir.is_symlink():
        return None, b""
    try:
        options = [p for p in log_dir.glob("bot*.log")
                   if p.is_file() and not p.is_symlink()]
        if not options:
            return None, b""
        latest = max(options, key=lambda x: x.stat().st_mtime)
        with latest.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - MAX_LOG_BYTES))
            data = f.read(MAX_LOG_BYTES)
        if size > MAX_LOG_BYTES:
            data = data.split(b"\n", 1)[-1]
        return latest, data
    except OSError:
        return None, b""


def _sanitize(line: str) -> str:
    # Only select known operational lines; redact token-shaped fields anyway.
    line = re.sub(r"(?i)(api[_-]?key|token|secret|passphrase|authorization)\s*[=:]\s*\S+",
                  r"\1=[REDACTED]", line)
    return line[:260]


def _log_report() -> dict:
    path, data = _latest_log()
    if path is None:
        return {"status": "UNAVAILABLE"}
    lines = data.decode("utf-8", errors="replace").splitlines()
    errors = [_sanitize(line) for line in lines if any(t.lower() in line.lower() for t in ERROR_TERMS)]
    activity = [_sanitize(line) for line in lines if any(t.lower() in line.lower() for t in ACTIVITY_TERMS)]
    try:
        age = max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        age = None
    return {"status": "OK", "file": path.name, "age_seconds": age,
            "errors_in_tail": len(errors), "recent_errors": errors[-18:],
            "recent_activity": activity[-18:],
            "scan_completed_in_tail": sum("Scan concluído" in v for v in activity),
            "reconciliation_error_in_tail": sum("RECONCILIACAO" in v for v in errors),
            "low_balance_in_tail": sum("Saldo livre abaixo" in v for v in errors)}


def _campaign_state_file(root: Path, filename: str) -> dict:
    """Read only nonsecret counts from campaign JSON; never modify."""
    if root.is_symlink():
        return {"status": "UNAVAILABLE"}
    path = root / filename
    if path.is_symlink() or not path.is_file():
        return {"status": "MISSING"}
    try:
        if path.stat().st_size > 2_000_000:
            return {"status": "UNAVAILABLE", "reason": "oversized"}
        value = json.loads(path.read_text(encoding="utf-8"))
        if filename == "historico_operacoes.json":
            records = value if isinstance(value, list) else (value.get("operacoes") if isinstance(value, dict) else None)
        else:
            records = value.get("positions", value) if isinstance(value, dict) else None
        if not isinstance(records, (list, dict)):
            return {"status": "UNAVAILABLE", "reason": "unknown-schema"}
        return {"status": "OK", "count": len(records), "path_scope": root.name}
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return {"status": "UNAVAILABLE", "reason": "read-failed"}


def _campaign_status() -> dict:
    if CAMPAIGN.is_symlink() or CAMPAIGN_STAGE.is_symlink():
        return {"status": "UNAVAILABLE", "reason": "symlink"}
    exists = CAMPAIGN_STAGE.is_dir()
    py_files = []
    if exists:
        try:
            py_files = sorted(p.name for p in CAMPAIGN_STAGE.glob("*.py")
                              if p.is_file() and not p.is_symlink())
        except OSError:
            return {"status": "UNAVAILABLE", "reason": "staging-not-readable"}
    states = {}
    for name in ("historico_operacoes.json", "posicoes_abertas.json"):
        one = _campaign_state_file(CAMPAIGN_STAGE, name)
        if one["status"] == "MISSING":
            one = _campaign_state_file(CAMPAIGN, name)
        states[name] = one
    trades = states["historico_operacoes.json"].get("count")
    return {
        "status": "OBSERVED_ONLY" if exists else "STAGING_UNAVAILABLE",
        "campaign": CAMPAIGN.name,
        "staging_exists": exists,
        "python_file_count": len(py_files),
        "files": py_files[:30],
        "state": states,
        "closed_trades_observed": trades,
        "target_closed_trades": 100,
        "main_or_signals_changed": False,
        "demo_exchange_reconciled_by_this_probe": False,
        "account_isolation_proven": False,
        "ready_to_activate": False,
    }


def _source_read(args: dict) -> dict:
    if set(args) != {"action", "file", "start_line", "line_count", "source"}:
        raise ValueError("source_read requires exact fields")
    start, count, which = args["start_line"], args["line_count"], args["source"]
    if (type(start) is not int or type(count) is not int
            or start < 1 or count < 1 or count > 100
            or which not in ("main_live", "campaign_staging", "dev_staging")):
        raise ValueError("invalid source_read args")
    root = {"main_live": BOT, "campaign_staging": CAMPAIGN_STAGE,
            "dev_staging": STAGE}[which]
    if not root.is_dir() or root.is_symlink():
        return {"status": "UNAVAILABLE", "source": which}
    path = _safe_file(root, args["file"])
    content = _read_source(path)
    lines = content.decode("utf-8").splitlines()
    if start > len(lines):
        raise ValueError("requested lines past end")
    selected = lines[start - 1:start - 1 + count]
    return {"status": "OK", "source": which, "file": args["file"],
            "start_line": start, "last_line": start + len(selected) - 1,
            "total_lines": len(lines), "source_sha256": _sha(content),
            "excerpt": "\n".join(selected), "read_only": True}


def _dev_status() -> dict:
    records = []
    for name in sorted(ALLOWED_SOURCE):
        source = _safe_file(BOT, name)
        staged = _safe_file(STAGE, name)
        try:
            original = _read_source(source)
            records.append({"file": name, "live_sha256": _sha(original),
                            "staged_sha256": _sha(_read_source(staged)) if staged.exists() else None})
        except (OSError, ValueError):
            records.append({"file": name, "status": "UNAVAILABLE"})
    return {"files": records, "staging_only": True, "main_files_changed": False}


def _stage_replace(args: dict) -> dict:
    if set(args) != {"action", "file", "expected_sha256", "old_text", "new_text"}:
        raise ValueError("invalid stage arguments")
    name, expected = args["file"], args["expected_sha256"]
    old, new = args["old_text"], args["new_text"]
    if (type(expected) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or type(old) is not str or type(new) is not str or not old
            or len(old) > MAX_PATCH_CHARS or len(new) > MAX_PATCH_CHARS):
        raise ValueError("invalid patch payload")
    source = _safe_file(BOT, name)
    staged = _safe_file(STAGE, name)
    base = _read_source(staged if staged.exists() else source)
    if _sha(base) != expected:
        raise ValueError("sha256 mismatch; refusing stale patch")
    text = base.decode("utf-8")
    if text.count(old) != 1:
        raise ValueError("old_text must appear exactly once")
    updated = text.replace(old, new, 1).encode("utf-8")
    if len(updated) > MAX_SOURCE:
        raise ValueError("staged file too large")
    ast.parse(updated.decode("utf-8"), filename=name)
    STAGE.mkdir(mode=0o700, parents=False, exist_ok=True)
    if STAGE.is_symlink():
        raise ValueError("unsafe staging directory")
    fd, temp = tempfile.mkstemp(prefix=".luk-stage-", dir=str(STAGE))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, staged)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return {"status": "STAGED_DEV_ONLY", "file": name, "staged_sha256": _sha(updated),
            "main_changed": False, "bot_restarted": False}


def run(action: str, args: dict | None = None) -> dict:
    if type(action) is not str or action not in ACTIONS:
        raise ValueError("unsupported maintenance action")
    if args is None:
        args = {"action": action}
    if type(args) is not dict or args.get("action") != action:
        raise ValueError("invalid maintenance arguments")
    if action in READ_ACTIONS and set(args) != {"action"}:
        raise ValueError("read-only action takes no extra arguments")
    if action == "source_read":
        return {"action": action, "scope": "CURRENT_PROCESS_NAMESPACE_ONLY",
                "host_attested": False, "go_for_main_mutations": False,
                "result": _source_read(args)}
    if action == "dev_stage_replace":
        return _stage_replace(args)
    if action in ("dev_validate", "dev_discard"):
        if set(args) != {"action", "file"}:
            raise ValueError("invalid DEV arguments")
        staged = _safe_file(STAGE, args["file"])
        if not staged.is_file():
            raise ValueError("staged file does not exist")
        if action == "dev_discard":
            staged.unlink()
            return {"status": "DISCARDED_DEV_ONLY", "main_changed": False}
        code = _read_source(staged).decode("utf-8")
        ast.parse(code, filename=args["file"])
        return {"status": "SYNTAX_OK_ONLY", "file": args["file"], "tests_run": False,
                "production_safe_to_deploy": False}
    if action == "positions":
        result = _read_positions()
    elif action == "campaign_100_status":
        result = _campaign_status()
    elif action == "dev_status":
        result = _dev_status()
    else:
        log = _log_report()
        if action == "errors":
            result = {k: v for k, v in log.items() if k in
                      ("status", "file", "age_seconds", "errors_in_tail",
                       "recent_errors", "reconciliation_error_in_tail", "low_balance_in_tail")}
        elif action == "activity":
            result = {k: v for k, v in log.items() if k in
                      ("status", "file", "age_seconds", "recent_activity",
                       "scan_completed_in_tail")}
        else:
            result = {"main_process": _process_info(), "positions": _read_positions(),
                      "log": log}
    return {"action": action, "scope": "CURRENT_PROCESS_NAMESPACE_ONLY",
            "host_attested": False, "go_for_main_mutations": False, "result": result}
