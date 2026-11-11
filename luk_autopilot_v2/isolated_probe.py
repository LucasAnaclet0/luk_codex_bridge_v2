"""Attest the narrow, offline diagnostic sandbox from INSIDE its namespace."""
import hashlib
import json
import os
import socket
from pathlib import Path

SOURCE = Path("/input/diagnostic.json")
FIELDS = frozenset({
    "schema", "generated_utc", "mode", "severity", "error_codes",
    "unknown_error_count", "services_active", "db_integrity",
    "pending_orders", "closed_trades", "open_positions",
    "quarantined_count", "restricted_pair_count",
    "network_authorized", "trading_authorized",
    "codex_autonomous_authorized",
})
CODES = frozenset({
    "V2_SERVICE_DOWN", "MCP_BRIDGE_DOWN", "V2_CYCLES_STALE",
    "V2_CYCLES_NOT_SEEN", "V2_UNRESOLVED_ORDERS_OLDER_10_MIN",
    "V2_DATABASE_UNVERIFIED",
    "OPEN_POSITIONS_CANNOT_AUTO_EXIT_EXCHANGE_RESTRICTION",
})

def fail(reason):
    raise RuntimeError("ISOLATION_NO_GO_" + reason)

def validate(payload):
    if type(payload) is not dict or set(payload) != FIELDS:
        fail("SNAPSHOT_FIELDS")
    if type(payload["schema"]) is not int or payload["schema"] != 1:
        fail("SNAPSHOT_SCHEMA")
    if payload["mode"] != "OFFLINE_ISOLATED_DIAGNOSIS_ONLY":
        fail("SNAPSHOT_MODE")
    if payload["severity"] not in ("OK", "WARNING", "CRITICAL"):
        fail("SNAPSHOT_SEVERITY")
    if payload["db_integrity"] not in ("ok", "unverified"):
        fail("DB_INTEGRITY")
    if any(payload[k] is not False for k in
           ("network_authorized", "trading_authorized", "codex_autonomous_authorized")):
        fail("MUTATION_AUTHORIZATION")
    code_list = payload["error_codes"]
    if type(code_list) is not list or code_list != sorted(set(code_list)):
        fail("ERROR_CODES")
    if any(type(x) is not str or x not in CODES for x in code_list):
        fail("UNTRUSTED_ERRORS")
    services = payload["services_active"]
    if type(services) is not dict or set(services) != {
        "trade", "bridge", "telegram_menu", "notifier"
    } or any(type(v) is not bool for v in services.values()):
        fail("SERVICE_TYPES")
    for key in ("unknown_error_count", "pending_orders", "closed_trades",
                "open_positions", "quarantined_count", "restricted_pair_count"):
        value = payload[key]
        if type(value) is not int or not 0 <= value <= 100000:
            fail("INVALID_COUNT")
    return payload

def main():
    if (set(os.environ) != {"PATH", "HOME", "LANG", "PWD"}
            or os.environ["HOME"] != "/tmp" or os.environ["PWD"] != "/app"):
        fail("ENVIRONMENT")
    for name in (
        "/home/ubuntu/.codex",
        "/home/ubuntu/.ssh",
        "/home/ubuntu/luk_bot_trader_v2",
        "/home/ubuntu/luk_codex_bridge_v2",
        "/etc/passwd", "/etc/shadow",
        "/proc/1/root/home/ubuntu/.codex",
    ):
        if Path(name).exists():
            fail("HOST_FILES_EXPOSED")
    with Path("/proc/self/status").open() as proc:
        caps = [line.split(":", 1)[1].strip() for line in proc
                if line.startswith("CapEff:")]
    if len(caps) != 1 or int(caps[0], 16) != 0:
        fail("CAPABILITIES")
    devices = Path("/proc/net/dev").read_text().splitlines()[2:]
    if any(row.split(":", 1)[0].strip() != "lo" for row in devices if ":" in row):
        fail("NETWORK_INTERFACE")
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("1.1.1.1", 443)) == 0:
            fail("NETWORK_EGRESS")
    if SOURCE.is_symlink() or not SOURCE.is_file():
        fail("SNAPSHOT_NOT_READONLY")
    try:
        with SOURCE.open("ab"):
            pass
    except OSError:
        pass
    else:
        fail("WRITABLE_SNAPSHOT")
    content = SOURCE.open("rb").read(4097)
    if len(content) > 4096:
        fail("SNAPSHOT_OVERSIZED")
    data = validate(json.loads(content))
    print(json.dumps({
        "result": "SANDBOX_ISOLATION_VERIFIED",
        "network": "namespace_disabled",
        "host_home": "not_mounted",
        "host_etc": "not_mounted",
        "source": "explicit_read_only_snapshot",
        "severity": data["severity"],
        "snapshot_sha256": hashlib.sha256(content).hexdigest(),
        "autonomous_code_execution": False,
        "codex_enabled": False,
    }, sort_keys=True))

if __name__ == "__main__":
    main()

