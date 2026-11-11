"""LUK Bot Trader V2 targeted tool. All paths and service names are fixed.

Observes V2 without touching V1. Modifications are separate, explicitly
approved actions with identity/hash checks and automatic file backups.
The MCP authenticator remains the outer access control. Not host attestation.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

ROOT = Path("/home/ubuntu/luk_bot_trader_v2")
UNIT = "luk-bot-trader-v2.service"
UNIT_NAME_PATTERN = re.compile(r"luk-[a-z0-9-]*v2[a-z0-9-]*\.service")
BRIDGE = Path("/home/ubuntu/luk_codex_bridge_v2")
DEV = BRIDGE / ".luk-bot-v2-dev-staging"
OPS = BRIDGE / ".luk-bot-v2-operator"
BACKUP = OPS / "backups"
MARKER = OPS / "last_deployment.json"
LOCK = OPS / "operator.lock"
MAX_FILE = 500_000
MAX_PATCH = 6_000
ACTIONS = frozenset(("status", "file_inventory", "source_read", "dev_stage_patch",
                     "dev_verify", "service_stop", "service_start", "service_restart",
                     "promote_dev", "rollback", "exchange_audit_readonly"))
CONFIRM = {
    "service_stop": "AUTORIZO_PARAR_BOT_V2_DEMO",
    "service_start": "AUTORIZO_INICIAR_BOT_V2_DEMO",
    "service_restart": "AUTORIZO_REINICIAR_BOT_V2_DEMO",
    "promote_dev": "AUTORIZO_APLICAR_CODIGO_BOT_V2_DEMO",
    "rollback": "AUTORIZO_RESTAURAR_BOT_V2_DEMO",
}
SAFE_FILES = frozenset((
    "main.py", "bot.py", "app.py", "config.py", "trading.py",
    "trader.py", "trader_testnet.py", "demo_execution.py",
    "strategies.py", "strategy.py", "risk_manager.py", "risk.py",
    "order_ledger.py", "db.py", "database.py", "scanner.py",
    "scanner_multi_crypto.py", "engine.py", "executor.py",
    "src/main.py", "src/bot.py", "src/engine.py",
    "src/trading.py", "src/strategies.py", "src/risk_manager.py",
    "src/orders.py", "src/executor.py", "src/database.py",
    "v2/demo.py", "v2/engine.py", "v2/portfolio.py", "v2/strategy.py",
    "v2/universe.py", "v2/telegram_menu.py", "v2/telegram_notify.py",
))
ALLOWED_SERVICE_FIELDS = ("LoadState", "ActiveState", "SubState", "MainPID",
                          "User", "WorkingDirectory", "ExecStart", "FragmentPath")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cmd(argv, timeout=8) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C"})


def _candidate_units() -> list[str]:
    names = {UNIT}
    for command in (["/usr/bin/systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"],
                    ["/usr/bin/systemctl", "list-units", "--all", "--type=service", "--no-legend", "--no-pager"]):
        try:
            result = _cmd(command)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode:
            continue
        for line in result.stdout.splitlines():
            name = line.strip().split(maxsplit=1)[0] if line.strip() else ""
            if UNIT_NAME_PATTERN.fullmatch(name):
                names.add(name)
    return sorted(names)[:25]


def _valid_v2_exec(raw: str) -> bool:
    """Recognize ONLY fixed V2 Python module entrypoint; V1 venv is read-only dependency."""
    if not isinstance(raw, str) or len(raw) > 1024:
        return False
    expected_interpreters = {
        "/home/ubuntu/luk_bot_trader_v2/venv/bin/python",
        "/home/ubuntu/luk_bot_trader/venv/bin/python",
    }
    match_path = re.search(r"(?:^|[\s{;])path=([^\s;}]+)", raw)
    match_argv = re.search(r"argv\[\]=\{?\s*([^;}]+)", raw)
    if not match_path or not match_argv:
        return False
    binary = match_path.group(1)
    tokens = match_argv.group(1).strip().split()
    if tokens and tokens[-1] == "}":
        tokens.pop()
    return (binary in expected_interpreters and bool(tokens) and
            tokens[0] == binary and
            tokens[1:] in (["-m", "v2.demo", "--daemon"],
                           ["-u", "-m", "v2.demo", "--daemon"]))


def _service_probe() -> dict:
    """Return only bounded identity predicates, never unit command lines/env."""
    facts = []
    fields = [arg for prop in ALLOWED_SERVICE_FIELDS for arg in ("-p", prop)]
    for unit in _candidate_units():
        try:
            cp = _cmd(["/usr/bin/systemctl", "show", "--no-pager", unit, *fields])
        except (OSError, subprocess.TimeoutExpired):
            facts.append({"unit": unit, "status": "SYSTEMD_UNAVAILABLE"})
            continue
        if cp.returncode:
            facts.append({"unit": unit, "status": "SYSTEMD_UNAVAILABLE"})
            continue
        data = {}
        for line in cp.stdout.splitlines():
            k, sep, v = line.partition("=")
            if sep and k in ALLOWED_SERVICE_FIELDS:
                data[k] = v[:1024]
        checks = {
            "load_state_loaded": data.get("LoadState") == "loaded",
            "user_ubuntu": data.get("User") == "ubuntu",
            "working_directory_v2": data.get("WorkingDirectory") == str(ROOT),
            "exec_start_v2_module_verified": _valid_v2_exec(data.get("ExecStart", "")),
            "unit_file_systemd": data.get("FragmentPath", "").startswith(
                "/etc/systemd/system/"),
        }
        facts.append({"unit": unit, "status": "OBSERVED",
                      "active_state": data.get("ActiveState", "UNKNOWN"),
                      "main_pid": data.get("MainPID", "UNKNOWN"),
                      "identity_checks": checks})
    return {"status": "OBSERVED", "units": facts[:25]}


def _service() -> dict:
    """Bind lifecycle strictly to fixed trade service; ignore Telegram menu unit."""
    target = UNIT
    fields = [arg for prop in ALLOWED_SERVICE_FIELDS for arg in ("-p", prop)]
    try:
        cp = _cmd(["/usr/bin/systemctl", "show", "--no-pager", target, *fields])
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "UNVERIFIED_V2_SERVICE",
                "candidate_names": _candidate_units()}
    if cp.returncode:
        return {"status": "UNVERIFIED_V2_SERVICE",
                "candidate_names": _candidate_units()}
    data = {}
    for line in cp.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep and k in ALLOWED_SERVICE_FIELDS:
            data[k] = v[:1024]
    if (data.get("LoadState") == "loaded" and
            data.get("User") == "ubuntu" and
            data.get("WorkingDirectory") == str(ROOT) and
            _valid_v2_exec(data.get("ExecStart", "")) and
            data.get("FragmentPath", "") == "/etc/systemd/system/" + target):
        return {"status": "OBSERVED", "Unit": target, **data}
    return {"status": "UNVERIFIED_V2_SERVICE",
            "candidate_names": _candidate_units()}


def _proc_info(pid: int) -> dict | None:
    p = Path("/proc") / str(pid)
    try:
        cwd = Path(os.readlink(p / "cwd"))
        if cwd != ROOT and ROOT not in cwd.parents:
            return None
        cmd = (p / "cmdline").read_bytes().split(b"\x00")
        if not cmd or not any(b"python" in part or part.endswith(b"/main.py")
                              for part in cmd[:3]):
            return None
        raw = (p / "stat").read_text()
        ticks = int(raw.rsplit(")", 1)[1].strip().split()[19])
        if ticks <= 0:
            return None
        # Classify by module, never expose argv/credentials.
        modules = {cmd[i + 1] for i, x in enumerate(cmd[:-1]) if x == b"-m"}
        role = ("trade_demo" if b"v2.demo" in modules else
                "telegram_menu" if b"v2.telegram_menu" in modules else
                "unknown_python")
        return {"pid": pid, "start_ticks": ticks, "cwd": str(cwd), "role": role}
    except (OSError, ValueError, IndexError):
        return None


def _processes() -> list[dict]:
    procs = []
    try:
        with os.scandir("/proc") as entries:
            for e in entries:
                if e.name.isdecimal():
                    item = _proc_info(int(e.name))
                    if item:
                        procs.append(item)
    except OSError:
        raise RuntimeError("NO_GO: /proc unavailable")
    return sorted(procs, key=lambda d: d["pid"])


def _safe_path(base: Path, name: str, *, discovery_read=False) -> Path:
    # Discovery is read-only in trusted V2 source packages, not backup or .env.
    discovered = bool(
        discovery_read and isinstance(name, str) and
        re.fullmatch(r"(?:(?:src|v2|core)/)?[A-Za-z_][A-Za-z0-9_-]{0,62}\.py", name)
    )
    if type(name) is not str or (name not in SAFE_FILES and not discovered):
        raise ValueError("Python file not allowlisted")
    if discovered and any(word in part.lower() for part in Path(name).parts
                          for word in ("secret", "credential", "token",
                                       "password", "api_key", "private")):
        raise ValueError("sensitive source filename refused")
    if base.is_symlink():
        raise RuntimeError("base path is symlink")
    p = base
    for part in Path(name).parts:
        p = p / part
        if p.is_symlink():
            raise RuntimeError("symlink refused")
    return p


def _read(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE:
        raise RuntimeError("source missing, linked or oversized")
    return path.read_bytes()


def _inventory() -> dict:
    """Find Python modules in bounded V2 source tree, no symlink traversal."""
    if not ROOT.is_dir() or ROOT.is_symlink():
        return {"status": "V2_DIRECTORY_MISSING", "root": str(ROOT)}
    found = []
    skipped = 0
    visited = 0
    excluded = {"venv", ".venv", "__pycache__", "node_modules", ".git",
                "site-packages", "data", "logs", "backups", "secrets", "private",
                "credentials", "keys", ".luk-bot-v2-operator"}
    # Breadth-first, bounded to depth 4. No recursive glob or untrusted symlink follows.
    pending = [(ROOT, 0)]
    while pending and visited < 180:
        folder, depth = pending.pop(0)
        visited += 1
        if folder.is_symlink():
            continue
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError:
            skipped += 1
            continue
        for item in children:
            if item.is_symlink():
                skipped += 1
                continue
            if item.is_dir():
                if depth < 4 and item.name not in excluded and not item.name.startswith("."):
                    pending.append((item, depth + 1))
                continue
            if item.is_file() and (item.suffix == ".py" or
                                  (depth <= 1 and item.suffix in (".sqlite3", ".db"))):
                rel = item.relative_to(ROOT)
                # Never disclose filenames of secret-like sources.
                if any(word in item.stem.lower() for word in
                       ("secret", "credential", "token", "password", "api_key")):
                    skipped += 1
                    continue
                try:
                    found.append({"file": str(rel), "bytes": item.stat().st_size})
                except OSError:
                    skipped += 1
    return {"status": "OBSERVED", "root": str(ROOT), "files": found[:120],
            "files_total_observed": len(found), "directories_checked": visited,
            "entries_skipped": skipped, "inventory_truncated": bool(pending or len(found) > 120)}


def _sqlite_summary() -> list[dict]:
    summaries = []
    if not ROOT.is_dir():
        return summaries
    for folder in (ROOT, ROOT / "data"):
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            candidates = sorted(p for p in folder.iterdir()
                                if p.is_file() and not p.is_symlink()
                                and p.suffix in (".sqlite3", ".db"))[:6]
        except OSError:
            continue
        for p in candidates:
            obj = {"database": str(p.relative_to(ROOT))}
            if p.stat().st_size > 200_000_000:
                obj["status"] = "SIZE_LIMIT"
                summaries.append(obj)
                continue
            conn = None
            try:
                # Read-only, no migrations, no modifications to running databases.
                conn = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=1)
                conn.execute("PRAGMA query_only=ON")
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                obj["tables"] = tables[:35]
                obj["status"] = "OBSERVED"
                counts = {}
                for table in tables:
                    if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", table) and any(
                            term in table.lower() for term in
                            ("trade", "operac", "position", "posic", "order", "fill")):
                        row = conn.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()
                        counts[table] = row[0]
                obj["row_counts"] = counts
                if obj["database"] == "data/v2_okx_demo.sqlite3" and {
                        "orders", "positions", "closed_trades"}.issubset(tables):
                    # Fixed readonly queries. Never return client/exchange order IDs.
                    grouped = conn.execute(
                        "SELECT status,side,count(*) FROM orders GROUP BY status,side"
                    ).fetchall()
                    obj["orders_by_status"] = [
                        {"status": str(status)[:24], "side": str(side)[:8],
                         "count": int(n)}
                        for status, side, n in grouped[:24]
                    ]
                    q = conn.execute(
                        "SELECT symbol,side,status,created_at FROM orders "
                        "WHERE status IN ('PREPARED','UNKNOWN','OPEN') "
                        "ORDER BY created_at LIMIT 10"
                    ).fetchall()
                    obj["unresolved_orders"] = [
                        {"symbol": str(sym)[:30], "side": str(side)[:8],
                         "status": str(status)[:24], "created_at": str(at)[:32]}
                        for sym, side, status, at in q
                    ]
                    obj["unresolved_orders_truncated"] = (
                        conn.execute(
                            "SELECT count(*) FROM orders WHERE status IN "
                            "('PREPARED','UNKNOWN','OPEN')"
                        ).fetchone()[0] > 10
                    )
                    p = conn.execute(
                        "SELECT symbol,cost,entry,stop,take FROM positions "
                        "ORDER BY symbol LIMIT 11"
                    ).fetchall()
                    obj["positions_summary"] = [
                        {"symbol": str(sym)[:30], "cost_usdt": round(float(cost or 0), 4),
                         "stop_take_valid": bool(stop is not None and entry is not None
                                                 and take is not None and
                                                 0 < float(stop) < float(entry) < float(take))}
                        for sym, cost, entry, stop, take in p[:10]
                    ]
                    obj["positions_truncated"] = len(p) > 10
                    obj["campaign_target_closed_trades"] = 100
                    obj["campaign_closed_trades"] = counts.get("closed_trades", 0)
            except (OSError, sqlite3.Error, ValueError):
                obj["status"] = "UNAVAILABLE"
            finally:
                if conn is not None:
                    conn.close()
            summaries.append(obj)
    return summaries


def _log_summary() -> dict:
    service = _service()
    if service.get("status") != "OBSERVED":
        return {"status": "V2_SERVICE_UNVERIFIED"}
    try:
        cp = _cmd(["/usr/bin/journalctl", "--no-pager", "--output=cat",
                   "-u", service["Unit"], "-n", "120"], timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "UNAVAILABLE"}
    if cp.returncode:
        return {"status": "UNAVAILABLE"}
    lines = cp.stdout.splitlines()
    markers = ("ERROR", "CRITICAL", "Traceback", "OrderNotFound", "NO_GO",
               "Trade", "trade", "ordem", "posição", "campaign", "campanha")
    marked = [line for line in lines if any(word in line for word in markers)]
    # No arbitrary log content returned: avoid leaking tokens, balances or private data.
    labels = {}
    for term in markers:
        labels[term] = sum(term in line for line in lines)
    return {"status": "OBSERVED", "lines_examined": len(lines),
            "marker_counts": labels, "marked_lines_count": len(marked)}


def _public_service_status(service: dict) -> dict:
    public = dict(service)
    # Command lines can include sensitive flags: provide identity evidence only.
    raw = public.pop("ExecStart", None)
    public["exec_start_v2_module_verified"] = _valid_v2_exec(raw or "")
    return public



# This child runs only fixed, authenticated GETs against OKX DEMO. It never
# sends orders, cancels orders, transfers funds, or changes the live SQLite DB.
_EXCHANGE_READONLY_CHILD = r'''
import json
import logging
import math
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

ROOT = Path("/home/ubuntu/luk_bot_trader_v2")
DB = ROOT / "data" / "v2_okx_demo.sqlite3"


def safe_number(raw):
    try:
        v = float(raw)
        return round(v, 9) if math.isfinite(v) and v >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def main():
    # Immutable read explicitly refuses active WAL, avoiding false order absence.
    if not DB.is_file() or DB.is_symlink():
        return {"status": "UNVERIFIED", "reason": "V2_DATABASE_UNAVAILABLE"}
    for suffix in ("-wal", "-shm"):
        p = DB.with_name(DB.name + suffix)
        if p.exists() and (p.is_symlink() or p.stat().st_size):
            return {"status": "UNVERIFIED", "reason": "SQLITE_ACTIVE_WAL"}
    original_stat = DB.stat()
    conn = sqlite3.connect(DB.as_uri() + "?mode=ro&immutable=1",
                           uri=True, timeout=2)
    try:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            return {"status": "UNVERIFIED", "reason": "SQLITE_QUICK_CHECK_FAILED"}
        rows = conn.execute(
            "SELECT cid,symbol,side,status FROM orders "
            "WHERE status IN ('PREPARED','UNKNOWN','OPEN')"
        ).fetchall()
        positions = conn.execute("SELECT symbol,qty,buy_cid FROM positions").fetchall()
        sol_buy = conn.execute(
            "SELECT o.exchange_id FROM positions p "
            "JOIN orders o ON o.cid=p.buy_cid "
            "WHERE p.symbol='SOL/USDT' AND o.symbol=p.symbol "
            "AND o.side='buy' AND o.status='FILLED' LIMIT 2"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) > 25 or len(positions) > 25:
        return {"status": "UNVERIFIED", "reason": "LEDGER_BOUNDS_EXCEEDED"}
    pending_inj = [r for r in rows
                   if r[1] == "INJ/USDT" and r[2] == "buy"]
    if len(pending_inj) > 1:
        return {"status": "UNVERIFIED", "reason": "INJ_DUPLICATE_PENDING"}
    position_map = {str(sym): safe_number(qty) for sym, qty, _ in positions}
    if any(qty is None for qty in position_map.values()):
        return {"status": "UNVERIFIED", "reason": "LEDGER_POSITION_INVALID"}
    # Only import the sanctioned V2 constructor: no daemon main or DB ledger.
    logging.disable(logging.CRITICAL)
    from v2.demo import make_exchange, assert_demo
    ex = make_exchange()
    try:
        assert_demo(ex)
        if getattr(ex, "id", "") != "okx":
            return {"status": "UNVERIFIED", "reason": "NOT_OKX"}
        if hasattr(ex, "session"):
            ex.session.max_redirects = 0
        allowed = {"/api/v5/trade/order", "/api/v5/account/balance",
                   "/api/v5/trade/orders-history",
                   "/api/v5/trade/orders-pending",
                   "/api/v5/trade/fills-history"}
        original_fetch = ex.fetch
        observed_requests = []

        def get_only(url, method="GET", headers=None, body=None):
            parsed = urlsplit(url)
            if (str(method).upper() != "GET" or
                    parsed.scheme != "https" or
                    parsed.hostname not in ("www.okx.com", "okx.com") or
                    parsed.port not in (None, 443) or
                    parsed.username is not None or parsed.password is not None or
                    parsed.path not in allowed):
                raise RuntimeError("NON_ALLOWED_READONLY_REQUEST")
            observed_requests.append(parsed.path)
            if len(observed_requests) > 6:
                raise RuntimeError("READONLY_REQUEST_LIMIT")
            return original_fetch(url, method, headers, body)

        ex.fetch = get_only
        report = {"status": "UNVERIFIED", "mode": "OKX_DEMO_SPOT_GET_ONLY",
                  "pending_inj": len(pending_inj),
                  "positions_count": len(positions),
                  "order_inj": "NO_PENDING" if not pending_inj else "UNVERIFIED",
                  "sol_balance": "UNVERIFIED"}
        if pending_inj:
            cid, symbol, side, state = pending_inj[0]
            try:
                raw = ex.private_get_trade_order({"instId": "INJ-USDT",
                                                  "clOrdId": cid})
                data = raw.get("data", []) if isinstance(raw, dict) else []
                matching = [v for v in data if isinstance(v, dict)
                            and v.get("clOrdId") == cid
                            and v.get("instId") == "INJ-USDT"
                            and v.get("side") == "buy"
                            and v.get("ordId")]
                if str(raw.get("code")) != "0" or len(matching) != 1:
                    report["order_inj"] = "NOT_POSITIVELY_CONFIRMED"
                else:
                    state = str(matching[0].get("state") or "UNKNOWN").lower()
                    filled_qty = safe_number(matching[0].get("accFillSz"))
                    if state == "filled" and filled_qty and filled_qty > 0:
                        report["order_inj"] = "CONFIRMED_FILLED"
                        report["inj_filled_qty"] = filled_qty
                    elif state in ("canceled", "cancelled") and filled_qty == 0:
                        report["order_inj"] = "CONFIRMED_CANCELED_ZERO_FILL"
                    elif state in ("live", "partially_filled", "partially-filled"):
                        report["order_inj"] = "EXCHANGE_ORDER_STILL_OPEN"
                    else:
                        report["order_inj"] = "ORDER_STATE_UNVERIFIED"
            except Exception as exc:
                report["order_inj"] = "QUERY_FAILED"
                report["inj_exception_class"] = type(exc).__name__[:50]
        # A 404/OrderNotFound is not a terminal order observation. Independently
        # search the bounded SPOT history and pending endpoints by instrument,
        # accepting only a uniquely matching full client identity.
        if pending_inj:
            cid = pending_inj[0][0]
            for label, method in (
                ("inj_order_history", "private_get_trade_orders_history"),
                ("inj_open_orders", "private_get_trade_orders_pending"),
            ):
                try:
                    response = getattr(ex, method)({
                        "instType": "SPOT", "instId": "INJ-USDT", "limit": "100"
                    })
                    if not isinstance(response, dict) or str(response.get("code")) != "0":
                        report[label] = "QUERY_UNVERIFIED"
                        continue
                    data = response.get("data")
                    if not isinstance(data, list) or len(data) > 100:
                        report[label] = "INVALID_RESPONSE"
                        continue
                    found = [item for item in data if isinstance(item, dict)
                             and item.get("clOrdId") == cid
                             and item.get("instId") == "INJ-USDT"
                             and item.get("side") == "buy"
                             and item.get("ordId")]
                    if len(found) != 1:
                        report[label] = "NO_UNIQUE_IDENTITY_MATCH"
                        continue
                    record = found[0]
                    state = str(record.get("state") or "").lower()
                    filled = safe_number(record.get("accFillSz"))
                    if state == "filled" and filled is not None and filled > 0:
                        report[label] = "CONFIRMED_FILLED"
                        report["inj_filled_qty"] = filled
                        report["order_inj"] = "POSITIVE_HISTORY_FILLED"
                    elif state == "canceled" and filled == 0:
                        report[label] = "CONFIRMED_CANCELED_ZERO_FILL"
                        report["order_inj"] = "POSITIVE_HISTORY_CANCELED_ZERO_FILL"
                    elif state in ("live", "partially_filled"):
                        report[label] = "EXCHANGE_OPEN_ORDER"
                        report["order_inj"] = "POSITIVE_OPEN_ORDER"
                    else:
                        report[label] = "MATCH_STATE_UNVERIFIED"
                except Exception as exc:
                    report[label] = "QUERY_FAILED"
                    report[label + "_exception_class"] = type(exc).__name__[:50]
        # Use ONLY the V2-owned SOL buy order ID to audit fees. Never disclose IDs.
        if len(sol_buy) == 1 and isinstance(sol_buy[0][0], str) and sol_buy[0][0]:
            try:
                response = ex.private_get_trade_fills_history({
                    "instType": "SPOT", "instId": "SOL-USDT",
                    "ordId": sol_buy[0][0], "limit": "100"
                })
                fills = response.get("data") if isinstance(response, dict) else None
                if (not isinstance(response, dict) or
                        str(response.get("code")) != "0" or
                        not isinstance(fills, list) or len(fills) > 100):
                    report["sol_buy_fills"] = "QUERY_UNVERIFIED"
                else:
                    valid = [f for f in fills if isinstance(f, dict)
                             and f.get("ordId") == sol_buy[0][0]
                             and f.get("instId") == "SOL-USDT"]
                    if not valid or len(valid) != len(fills):
                        report["sol_buy_fills"] = "INCOMPLETE_OR_IDENTITY_MISMATCH"
                    else:
                        amounts = [safe_number(f.get("fillSz")) for f in valid]
                        fees = [float(f.get("fee") or 0)
                                if f.get("feeCcy") == "SOL" else 0.0
                                for f in valid]
                        if (any(v is None or v <= 0 for v in amounts)
                                or any(not math.isfinite(fee) for fee in fees)):
                            report["sol_buy_fills"] = "NONFINITE_FILL_OR_FEE"
                        else:
                            net = round(sum(amounts) + sum(fees), 9)
                            report["sol_buy_fills"] = "OBSERVED"
                            report["sol_net_qty_from_buy_fills"] = net
                            report["sol_net_fills_covers_ledger"] = (
                                net + 1e-9 >= position_map.get("SOL/USDT", float("inf")))
                            # Funds may later be moved/sold; fills alone do not prove
                            # the entire current free balance is V2-owned.
                            report["ownership_proven"] = False
            except Exception as exc:
                report["sol_buy_fills"] = "QUERY_FAILED"
                report["sol_buy_fills_exception_class"] = type(exc).__name__[:50]
        else:
            report["sol_buy_fills"] = "V2_BUY_ORDER_ID_UNVERIFIED"

        try:
            raw = ex.private_get_account_balance({})
            if isinstance(raw, dict) and str(raw.get("code")) == "0":
                data = raw.get("data") or []
                rows = (data[0].get("details") or []) if data and isinstance(data[0], dict) else []
                sol = [r for r in rows if isinstance(r, dict) and r.get("ccy") == "SOL"]
                if len(sol) <= 1:
                    balance = sol[0] if sol else {}
                    free = safe_number(balance.get("availBal") or 0)
                    frozen = safe_number(balance.get("frozenBal") or 0)
                    owned = position_map.get("SOL/USDT")
                    if free is not None and frozen is not None and owned is not None:
                        report.update({"sol_balance": "OBSERVED",
                                       "sol_free": free, "sol_frozen": frozen,
                                       "sol_ledger_qty": owned,
                                       "sol_free_covers_ledger": free + 1e-10 >= owned,
                                       "sol_total_covers_ledger": free+frozen+1e-10>=owned,
                                       "ownership_proven": False})
            else:
                report["sol_balance"] = "QUERY_NOT_CONFIRMED"
        except Exception as exc:
            report["sol_balance"] = "QUERY_FAILED"
            report["balance_exception_class"] = type(exc).__name__[:50]
        after = DB.stat()
        if (original_stat.st_size != after.st_size or
                original_stat.st_mtime_ns != after.st_mtime_ns):
            return {"status": "UNVERIFIED", "reason": "SQLITE_CHANGED_DURING_AUDIT"}
        report["get_requests_only"] = all(p in allowed for p in observed_requests)
        report["get_requests_count"] = len(observed_requests)
        report["status"] = "READONLY_AUDIT_COMPLETED"
        return report
    finally:
        try:
            ex.close()
        except Exception:
            pass


try:
    print(json.dumps(main(), separators=(",", ":"), ensure_ascii=True))
except Exception as exc:
    print(json.dumps({"status": "UNVERIFIED",
                      "reason": "READONLY_AUDIT_FAILED",
                      "exception_class": type(exc).__name__[:50]}))
'''


def _exchange_audit_readonly() -> dict:
    """Fixed read-only authenticated audit. Never return raw child stdout/stderr."""
    try:
        service, processes = _service_identity()
    except (RuntimeError, OSError):
        return {"status": "NO_GO", "reason": "V2_SERVICE_IDENTITY_UNVERIFIED"}
    if (service.get("ActiveState") != "active" or
            not any(str(p["pid"]) == service.get("MainPID") and
                    p.get("role") == "trade_demo" for p in processes) or
            not _demo_policy_proof()):
        return {"status": "NO_GO", "reason": "V2_DEMO_RUNTIME_IDENTITY_UNVERIFIED"}
    cmd = ["/home/ubuntu/luk_bot_trader/venv/bin/python",
           "-B", "-c", _EXCHANGE_READONLY_CHILD]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "LANG": "C", "HOME": "/home/ubuntu",
           "PYTHONDONTWRITEBYTECODE": "1",
           "LUK_V2_DEMO_APPROVED": "I_APPROVE_OKX_DEMO_ONLY"}
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True,
                           capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "UNVERIFIED", "reason": "READONLY_CHILD_UNAVAILABLE"}
    if p.returncode or len(p.stdout) > 3000:
        return {"status": "UNVERIFIED", "reason": "READONLY_CHILD_FAILED"}
    try:
        item = json.loads(p.stdout)
    except (ValueError, TypeError):
        return {"status": "UNVERIFIED", "reason": "READONLY_CHILD_INVALID_RESPONSE"}
    if type(item) is not dict:
        return {"status": "UNVERIFIED", "reason": "READONLY_CHILD_INVALID_SHAPE"}
    fields = {"status", "reason", "mode", "pending_inj", "positions_count",
              "order_inj", "inj_filled_qty", "sol_balance", "sol_free",
              "sol_frozen", "sol_ledger_qty", "sol_free_covers_ledger",
              "sol_total_covers_ledger", "ownership_proven", "get_requests_only",
              "get_requests_count", "inj_exception_class",
              "balance_exception_class", "exception_class",
              "inj_order_history", "inj_open_orders",
              "inj_order_history_exception_class", "inj_open_orders_exception_class",
              "sol_buy_fills", "sol_buy_fills_exception_class",
              "sol_net_qty_from_buy_fills", "sol_net_fills_covers_ledger"}
    return {k: item[k] for k in fields if k in item}


def _status() -> dict:
    service = _service()
    return {
        "root_exists": ROOT.is_dir() and not ROOT.is_symlink(),
        "service": _public_service_status(service),
        "demo_source_guard_proof": _demo_policy_proof(),
        "service_identity_diagnostics": _service_probe(),
        "observed_processes": _processes(),
        "source_files": _inventory(),
        "database_observations": _sqlite_summary(),
        "journal": _log_summary(),
        "host_attested": False,
        "ready_for_mutation": (service.get("status") == "OBSERVED"
                               and _demo_policy_proof()),
        "ready_to_trade": False,
    }


def _demo_policy_proof() -> bool:
    """Structural proof of mandatory OKX Demo guards; never proves live exchange."""
    try:
        script = _read(_safe_path(ROOT, "v2/demo.py")).decode("utf-8")
        tree = ast.parse(script, filename="v2/demo.py")
    except (OSError, ValueError, RuntimeError, UnicodeError, SyntaxError):
        return False
    functions = {node.name: node for node in tree.body
                 if isinstance(node, ast.FunctionDef)}
    required = {"assert_env", "make_exchange", "assert_demo",
                "assert_environment_ready", "place", "cycle",
                "main", "v1_processes"}
    if not required.issubset(functions):
        return False
    strings = {node.value for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    if not {"USAR_TESTNET", "LUK_V2_DEMO_APPROVED",
            "I_APPROVE_OKX_DEMO_ONLY", "x-simulated-trading"}.issubset(strings):
        return False
    def call_lines(func, name):
        return sorted(node.lineno for node in ast.walk(functions[func])
                      if isinstance(node, ast.Call) and
                      (getattr(node.func, "id", None) == name or
                       getattr(node.func, "attr", None) == name))
    for func, name in (("make_exchange", "assert_env"),
                       ("make_exchange", "set_sandbox_mode"),
                       ("make_exchange", "assert_demo"),
                       ("place", "assert_demo"),
                       ("place", "v1_processes"),
                       ("place", "create_order"),
                       ("main", "v1_processes"),
                       ("main", "assert_environment_ready")):
        if not call_lines(func, name):
            return False
    if not (call_lines("make_exchange", "assert_env")[0] <
            call_lines("make_exchange", "set_sandbox_mode")[0] <
            call_lines("make_exchange", "assert_demo")[0]):
        return False
    if not (call_lines("place", "assert_demo")[0] <
            call_lines("place", "create_order")[0]):
        return False
    return True


def _service_identity() -> tuple[dict, list[dict]]:
    service = _service()
    processes = _processes()
    if (service.get("status") != "OBSERVED" or
            service.get("LoadState") != "loaded" or
            service.get("User") != "ubuntu" or
            service.get("WorkingDirectory") != str(ROOT) or
            not _valid_v2_exec(service.get("ExecStart", "")) or
            not str(service.get("FragmentPath", "")).startswith("/etc/systemd/system/")):
        raise RuntimeError("NO_GO: fixed V2 systemd unit identity unverified")
    return service, processes


def _ensure_trade_stopped():
    """For start/restart: ignore verified Telegram menu, reject all unknown workers."""
    service, processes = _service_identity()
    if service.get("ActiveState") not in ("inactive", "failed") or service.get("MainPID") not in ("0", ""):
        raise RuntimeError("NO_GO: V2 systemd service not stopped")
    if any(p.get("role") != "telegram_menu" for p in processes):
        raise RuntimeError("NO_GO: unknown or trading V2 Python process remains")
    if processes:
        # Menu process is independent: require fixed secondary unit attestation.
        try:
            cp = _cmd(["/usr/bin/systemctl", "show", "--no-pager",
                       "luk-bot-trader-v2-telegram-menu.service",
                       "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID",
                       "-p", "User", "-p", "WorkingDirectory", "-p", "ExecStart"])
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("NO_GO: Telegram menu identity unavailable") from None
        if cp.returncode:
            raise RuntimeError("NO_GO: Telegram menu service inaccessible")
        fields = dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)
        if (fields.get("LoadState") != "loaded" or
                fields.get("User") != "ubuntu" or
                fields.get("WorkingDirectory") != str(ROOT) or
                "v2.telegram_menu" not in fields.get("ExecStart", "") or
                len(processes) != 1 or
                fields.get("MainPID") != str(processes[0]["pid"])):
            raise RuntimeError("NO_GO: Telegram menu process unverified")
    return service


def _ensure_stopped():
    """Code promotion requires no Python process from V2 root, including menu."""
    service, processes = _service_identity()
    if service.get("ActiveState") not in ("inactive", "failed") or service.get("MainPID") not in ("0", ""):
        raise RuntimeError("NO_GO: V2 systemd service not stopped")
    if processes:
        raise RuntimeError("NO_GO: V2 still has running Python processes")
    return service


def _atomic(path: Path, payload: bytes, mode=0o600):
    fd, temp = tempfile.mkstemp(prefix=".luk-v2-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _stage_patch(args: dict) -> dict:
    """Patch live source to DEV, or append a verified change to existing DEV.

    Never writes to ROOT. Hashes are required for every stage operation.
    Existing staged content may be continued only if its current hash matches
    the supplied expected_sha256; deployment still requires exact LIVE hash.
    """
    expected = args["expected_sha256"]
    if type(expected) is not str or not re.fullmatch("[a-f0-9]{64}", expected):
        raise ValueError("expected_sha256 required")
    old, new = args["old_text"], args["new_text"]
    if type(old) is not str or type(new) is not str or not (0 < len(old) <= MAX_PATCH) or len(new) > MAX_PATCH:
        raise ValueError("bad patch")
    name = args["file"]
    live_path = _safe_path(ROOT, name)
    live = _read(live_path)
    staged = _safe_path(DEV, name)
    if _hash(live) == expected:
        base = live
        origin = "LIVE_TO_DEV"
    elif staged.is_file() and not staged.is_symlink():
        base = _read(staged)
        if _hash(base) != expected:
            raise RuntimeError("staged/live file hash changed")
        origin = "STAGED_TO_DEV"
    else:
        raise RuntimeError("staged/live file hash changed")
    original = base.decode("utf-8")
    if original.count(old) != 1:
        raise RuntimeError("patch anchor not unique")
    candidate = original.replace(old, new, 1).encode("utf-8")
    if len(candidate) > MAX_FILE:
        raise RuntimeError("result too large")
    ast.parse(candidate.decode("utf-8"), filename=name)
    staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic(staged, candidate)
    return {"status": "STAGED_ONLY", "file": name,
            "live_sha256": _hash(live),
            "staged_sha256": _hash(candidate), "origin": origin,
            "main_changed": False, "tests_run": False}


def _verify(name: str) -> dict:
    live, staged = _read(_safe_path(ROOT, name)), _read(_safe_path(DEV, name))
    ast.parse(staged.decode("utf-8"), filename=name)
    return {"file": name, "live_sha256": _hash(live),
            "staged_sha256": _hash(staged), "syntax_ok": True, "tests_run": False}


def _stop(args: dict) -> dict:
    service, procs = _service_identity()
    pid, ticks = args["expected_pid"], args["expected_ticks"]
    if (type(pid) is not int or type(ticks) is not int or pid < 2 or ticks < 1
            or service.get("ActiveState") != "active" or service.get("MainPID") != str(pid)
            or len([p for p in procs if p["pid"] == pid and
                    p["start_ticks"] == ticks and
                    p.get("role") == "trade_demo"]) != 1 or
            sum(p.get("role") == "trade_demo" for p in procs) != 1):
        raise RuntimeError("NO_GO: V2 service/process identity changed")
    # The service manager, not arbitrary signals or shell, controls the process.
    try:
        result = _cmd(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", service["Unit"]], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("NO_GO: systemd stop failed") from None
    if result.returncode:
        raise RuntimeError("NO_GO: systemd denied stop")
    _ensure_trade_stopped()
    return {"status": "V2_STOPPED", "pid": pid, "main_v1_changed": False}


def _start() -> dict:
    verified_service = _ensure_trade_stopped()
    if not _demo_policy_proof():
        raise RuntimeError("NO_GO: V2 explicit OKX DEMO proof missing")
    try:
        result = _cmd(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", verified_service["Unit"]], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("NO_GO: systemd start failed") from None
    if result.returncode:
        raise RuntimeError("NO_GO: systemd denied start")
    service = _service()
    if service.get("ActiveState") != "active":
        raise RuntimeError("NO_GO: V2 start not active")
    return {"status": "V2_SERVICE_ACTIVE_NOT_TRADE_VERIFIED", "main_v1_changed": False}


def _restart(args: dict) -> dict:
    """Restart one observed V2 demo service only. Can trigger Demo orders."""
    service, procs = _service_identity()
    pid, ticks = args["expected_pid"], args["expected_ticks"]
    if (type(pid) is not int or type(ticks) is not int or pid < 2 or ticks < 1
            or service.get("ActiveState") != "active" or
            service.get("MainPID") != str(pid) or
            sum(p.get("role") == "trade_demo" for p in procs) != 1 or
            not any(p["pid"] == pid and p["start_ticks"] == ticks and
                    p.get("role") == "trade_demo" for p in procs)):
        raise RuntimeError("NO_GO: V2 restart identity unverified")
    if not _demo_policy_proof():
        raise RuntimeError("NO_GO: V2 explicit OKX DEMO proof missing")
    try:
        result = _cmd(["/usr/bin/sudo", "-n", "/usr/bin/systemctl",
                       "restart", service["Unit"]], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("NO_GO: V2 systemd restart failed") from None
    if result.returncode:
        raise RuntimeError("NO_GO: V2 systemd restart denied")
    after = _service()
    if (after.get("status") != "OBSERVED" or
            after.get("ActiveState") != "active" or
            after.get("MainPID") in (str(pid), "0", "")):
        raise RuntimeError("NO_GO: V2 restart outcome not confirmed")
    return {"status": "V2_RESTARTED_DEMO_RUNNING_NOT_RECONCILED",
            "previous_pid": pid, "new_pid": after["MainPID"],
            "main_v1_changed": False}


def _promote(args: dict) -> dict:
    # v2/demo.py is independent from Telegram menu; the verified menu may stay online.
    (_ensure_trade_stopped() if args["file"] == "v2/demo.py" else _ensure_stopped())
    info = _verify(args["file"])
    if info["live_sha256"] != args["expected_sha256"] or info["staged_sha256"] != args["staged_sha256"]:
        raise RuntimeError("NO_GO: source hash mismatch")
    live = _safe_path(ROOT, args["file"])
    old = _read(live)
    new = _read(_safe_path(DEV, args["file"]))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = BACKUP / (args["file"].replace("/", "_") + "." + stamp + ".bak")
    _atomic(backup, old)
    record = {"file": args["file"], "backup": str(backup),
              "previous": _hash(old), "promoted": _hash(new)}
    try:
        _atomic(live, new, mode=0o644)
        _atomic(MARKER, json.dumps(record).encode("utf-8"))
    except Exception:
        _atomic(live, old, mode=0o644)
        raise
    return {"status": "V2_SOURCE_PROMOTED_NO_RESTART", "backup": str(backup),
            "file": args["file"], "main_v1_changed": False}


def _rollback(args: dict) -> dict:
    (_ensure_trade_stopped() if args["file"] == "v2/demo.py" else _ensure_stopped())
    if not MARKER.is_file() or MARKER.is_symlink():
        raise RuntimeError("NO_GO: deployment journal unavailable")
    record = json.loads(MARKER.read_text())
    if record.get("file") != args["file"]:
        raise RuntimeError("NO_GO: rollback file mismatch")
    b = Path(record["backup"])
    if b.parent != BACKUP:
        raise RuntimeError("NO_GO: backup path invalid")
    old = _read(b)
    live = _safe_path(ROOT, args["file"])
    current = _read(live)
    if _hash(old) != record["previous"] or _hash(current) != record["promoted"]:
        raise RuntimeError("NO_GO: backup/source hashes changed")
    _atomic(live, old, mode=0o644)
    MARKER.unlink()
    return {"status": "V2_SOURCE_ROLLED_BACK_NO_RESTART", "file": args["file"]}


def run(args: dict) -> dict:
    if type(args) is not dict or args.get("action") not in ACTIONS:
        raise ValueError("invalid V2 action")
    action = args["action"]
    shapes = {
        "status": {"action"},
        "file_inventory": {"action"},
        "exchange_audit_readonly": {"action"},
        "source_read": {"action", "file", "start_line", "line_count"},
        "dev_stage_patch": {"action", "file", "old_text", "new_text", "expected_sha256"},
        "dev_verify": {"action", "file"},
        "service_stop": {"action", "confirmation", "expected_pid", "expected_ticks"},
        "service_start": {"action", "confirmation"},
        "service_restart": {"action", "confirmation", "expected_pid", "expected_ticks"},
        "promote_dev": {"action", "confirmation", "file", "expected_sha256", "staged_sha256"},
        "rollback": {"action", "confirmation", "file"},
    }
    if set(args) != shapes[action]:
        raise ValueError("unexpected or missing V2 fields")
    if action in CONFIRM and args.get("confirmation") != CONFIRM[action]:
        raise PermissionError("explicit approval for this V2 action missing")
    if os.getuid() != 1001 or os.geteuid() != 1001:
        raise RuntimeError("expected ubuntu uid 1001")
    if ROOT.is_symlink() or BRIDGE.is_symlink() or DEV.is_symlink() or OPS.is_symlink():
        raise RuntimeError("symlink path refused")
    # Read-only actions must not create directories, lock files or backups.
    if action in ("status", "file_inventory", "source_read", "exchange_audit_readonly"):
        if action == "status":
            result = _status()
        elif action == "file_inventory":
            result = _inventory()
        elif action == "exchange_audit_readonly":
            result = _exchange_audit_readonly()
        else:
            start, count = args["start_line"], args["line_count"]
            if type(start) is not int or type(count) is not int or start < 1 or count < 1 or count > 100:
                raise ValueError("source excerpt bounds invalid")
            content = _read(_safe_path(ROOT, args["file"], discovery_read=True))
            lines = content.decode("utf-8").splitlines()
            if start > len(lines):
                raise ValueError("past end")
            selected = lines[start - 1:start - 1 + count]
            redacted = [("[REDACTED: credential-related source line]"
                         if any(term in line.lower() for term in
                                ("api_key", "apikey", "api_secret", "apisecret",
                                 "password", "secret", "private_key", "token",
                                 "authorization", "bearer")) else line)
                        for line in selected]
            result = {"status": "OK", "file": args["file"], "source_sha256": _hash(content),
                      "total_lines": len(lines), "start_line": start,
                      "excerpt": "\n".join(redacted), "read_only": True}
        return {"action": action, "scope": "FIXED_BOT_V2_ONLY", "host_attested": False,
                "main_v1_changed": False, "result": result}
    OPS.mkdir(mode=0o700, parents=False, exist_ok=True)
    BACKUP.mkdir(mode=0o700, exist_ok=True)
    os.chmod(OPS, 0o700)
    os.chmod(BACKUP, 0o700)
    with LOCK.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if action == "status":
            result = _status()
        elif action == "file_inventory":
            result = _inventory()
        elif action == "source_read":
            start, count = args["start_line"], args["line_count"]
            if type(start) is not int or type(count) is not int or start < 1 or count < 1 or count > 100:
                raise ValueError("source excerpt bounds invalid")
            content = _read(_safe_path(ROOT, args["file"], discovery_read=True))
            lines = content.decode("utf-8").splitlines()
            if start > len(lines):
                raise ValueError("past end")
            selected = lines[start - 1:start - 1 + count]
            result = {"status": "OK", "file": args["file"], "source_sha256": _hash(content),
                      "total_lines": len(lines), "start_line": start,
                      "excerpt": "\n".join(selected), "read_only": True}
        elif action == "dev_stage_patch":
            result = _stage_patch(args)
        elif action == "dev_verify":
            result = _verify(args["file"])
        elif action == "service_stop":
            result = _stop(args)
        elif action == "service_start":
            result = _start()
        elif action == "service_restart":
            result = _restart(args)
        elif action == "promote_dev":
            result = _promote(args)
        elif action == "rollback":
            result = _rollback(args)
        else:
            raise RuntimeError("unhandled operation")
    return {"action": action, "scope": "FIXED_BOT_V2_ONLY", "host_attested": False,
            "main_v1_changed": False, "result": result}
