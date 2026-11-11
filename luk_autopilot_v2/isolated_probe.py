"""Run INSIDE a networkless, filesystem-isolated Linux namespace."""
import json
import os
from pathlib import Path

SOURCE = Path("/input/diagnostic.json")

def check():
    if os.environ.get("HOME") != "/tmp":
        raise RuntimeError("SANDBOX_HOME_UNVERIFIED")
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_HOME"):
        raise RuntimeError("UNEXPECTED_AUTH_ENV")
    denied = (
        "/home/ubuntu/.codex",
        "/home/ubuntu/.ssh",
        "/home/ubuntu/luk_bot_trader_v2",
        "/home/ubuntu/luk_codex_bridge_v2",
        "/etc/passwd",
        "/etc/shadow",
    )
    if any(Path(name).exists() for name in denied):
        raise RuntimeError("HOST_FILES_EXPOSED")
    if not SOURCE.is_file() or SOURCE.is_symlink():
        raise RuntimeError("SNAPSHOT_UNAVAILABLE")
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    if (
        type(payload) is not dict
        or payload.get("schema") != 1
        or payload.get("network_authorized") is not False
        or payload.get("trading_authorized") is not False
        or payload.get("codex_autonomous_authorized") is not False
    ):
        raise RuntimeError("DIAGNOSTIC_CONTRACT_BROKEN")
    severity = payload.get("severity")
    if severity not in ("OK", "WARNING", "CRITICAL"):
        raise RuntimeError("DIAGNOSTIC_SEVERITY_INVALID")
    print(json.dumps({
        "result": "SANDBOX_ISOLATION_VERIFIED",
        "network": "namespace_disabled",
        "host_home": "not_mounted",
        "host_etc": "not_mounted",
        "source": "explicit_read_only_snapshot",
        "severity": severity,
        "autonomous_code_execution": False,
        "codex_enabled": False,
    }, sort_keys=True))

if __name__ == "__main__":
    check()
