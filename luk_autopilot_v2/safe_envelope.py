"""LUK V2 — construct a deliberately minimal, non-secret diagnostic envelope."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone

SCHEMA = 1
CODES = frozenset({
    "V2_SERVICE_DOWN", "MCP_BRIDGE_DOWN", "V2_CYCLES_STALE",
    "V2_CYCLES_NOT_SEEN", "V2_UNRESOLVED_ORDERS_OLDER_10_MIN",
    "V2_DATABASE_UNVERIFIED", "OPEN_POSITIONS_CANNOT_AUTO_EXIT_EXCHANGE_RESTRICTION",
})
SEVERITIES = frozenset({"OK", "WARNING", "CRITICAL"})
ROOT = Path(__file__).resolve().parent / "isolated"
FILE = ROOT / "diagnostic.json"

def bounded_count(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("UNVERIFIED_COUNT")
    if not 0 <= value <= 100000:
        raise ValueError("OUT_OF_RANGE_COUNT")
    return value

def envelope(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("severity") not in SEVERITIES:
        raise ValueError("SNAPSHOT_SCHEMA_INVALID")
    db = snapshot.get("database")
    services = snapshot.get("services")
    if not isinstance(db, dict) or not isinstance(services, dict):
        raise ValueError("SNAPSHOT_COMPONENTS_MISSING")
    reported = snapshot.get("errors", [])
    if not isinstance(reported, list):
        raise ValueError("ERROR_SCHEMA_INVALID")
    errors = sorted({
        entry.split(":", 1)[0] for entry in reported
        if isinstance(entry, str) and entry.split(":", 1)[0] in CODES
    })
    service_health = {}
    for key in ("trade", "bridge", "telegram_menu", "notifier"):
        item = services.get(key)
        if not isinstance(item, dict) or type(item.get("active")) is not bool:
            raise ValueError("SERVICE_HEALTH_UNVERIFIED")
        service_health[key] = item["active"]
    severity = snapshot["severity"]
    return {
        "schema": SCHEMA,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "OFFLINE_ISOLATED_DIAGNOSIS_ONLY",
        "severity": severity,
        "error_codes": errors,
        "unknown_error_count": bounded_count(len(reported) - len(errors)),
        "services_active": service_health,
        "db_integrity": "ok" if db.get("integrity") == "ok" else "unverified",
        "pending_orders": bounded_count(db.get("pending_count")),
        "closed_trades": bounded_count(db.get("closed_trades")),
        "open_positions": bounded_count(db.get("positions")),
        "quarantined_count": bounded_count(db.get("quarantined_count")),
        "restricted_pair_count": bounded_count(len(db.get("restricted_pairs", []))),
        "network_authorized": False,
        "trading_authorized": False,
        "codex_autonomous_authorized": False,
    }

def validate_payload(value):
    if not isinstance(value, dict):
        raise ValueError("DIAGNOSTIC_SCHEMA_INVALID")
    keys = {
        "schema", "generated_utc", "mode", "severity", "error_codes",
        "unknown_error_count", "services_active", "db_integrity",
        "pending_orders", "closed_trades", "open_positions",
        "quarantined_count", "restricted_pair_count",
        "network_authorized", "trading_authorized",
        "codex_autonomous_authorized",
    }
    if set(value) != keys:
        raise ValueError("DIAGNOSTIC_UNEXPECTED_FIELD")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("DIAGNOSTIC_SCHEMA_MISMATCH")
    try:
        generated = datetime.fromisoformat(value["generated_utc"])
        if generated.tzinfo is None:
            raise ValueError("UNZONED_TIMESTAMP")
    except (TypeError, ValueError):
        raise ValueError("DIAGNOSTIC_TIMESTAMP_INVALID") from None
    if value["mode"] != "OFFLINE_ISOLATED_DIAGNOSIS_ONLY":
        raise ValueError("DIAGNOSTIC_MODE_INVALID")
    if value["severity"] not in SEVERITIES:
        raise ValueError("DIAGNOSTIC_SEVERITY_INVALID")
    errors = value["error_codes"]
    if (not isinstance(errors, list) or any(type(x) is not str or x not in CODES for x in errors)
            or errors != sorted(set(errors))):
        raise ValueError("DIAGNOSTIC_CODES_INVALID")
    services = value["services_active"]
    if (not isinstance(services, dict)
            or set(services) != {"trade", "bridge", "telegram_menu", "notifier"}
            or any(type(x) is not bool for x in services.values())):
        raise ValueError("DIAGNOSTIC_SERVICE_INVALID")
    if value["db_integrity"] not in ("ok", "unverified"):
        raise ValueError("DIAGNOSTIC_DB_INVALID")
    for key in ("unknown_error_count", "pending_orders", "closed_trades",
                "open_positions", "quarantined_count", "restricted_pair_count"):
        bounded_count(value[key])
    for key in ("network_authorized", "trading_authorized", "codex_autonomous_authorized"):
        if value[key] is not False:
            raise ValueError("DIAGNOSTIC_PRIVILEGE_VIOLATION")
    return value

def write_envelope(payload, target=FILE):
    validate_payload(payload)
    target = Path(target)
    directory = target.parent
    ancestor = directory
    while True:
        if ancestor.is_symlink():
            raise RuntimeError("UNSAFE_SANDBOX_ANCESTOR")
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    if target.is_symlink():
        raise RuntimeError("UNSAFE_SANDBOX_PATH")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4096:
        raise RuntimeError("DIAGNOSTIC_TOO_LARGE")
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, target)
        descriptor = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target

