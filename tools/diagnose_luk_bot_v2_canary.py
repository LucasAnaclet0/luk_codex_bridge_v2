#!/usr/bin/env python3
"""Read-only local replay of the V2 installer canary in a temporary directory.

Does NOT modify bridge, V1 or V2; does not restart services.
Only emits error type and bounded exception summary (no environment/secret values).
"""
from __future__ import annotations
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

BRIDGE = Path("/home/ubuntu/luk_codex_bridge_v2")
INSTALLER = Path("/tmp/luk-bot-v2-install.py")
INSTALLER_SHA = "01c6aadf5deec48384ac8dc5a2ccde2208ccf2eb4e19ba55cc672caf25bb9b96"
CANARY = (
    "from mcp_server import _tools, _tool_payload\n"
    "assert len([x for x in _tools() if x['name']=='vps_bot_trader_v2']) == 1\n"
    "for payload in ({'action':'shell'}, {'action':'service_stop','command':'kill -9 1'}):\n"
    "  try: _tool_payload('vps_bot_trader_v2', payload)\n"
    "  except ValueError: pass\n"
    "  else: raise AssertionError('unsafe arguments accepted')\n"
)
def _safe_error(s: str) -> str:
    s = s.strip().splitlines()[-1] if s.strip() else "stderr_not_available"
    s = re.sub(r"(?i)(bearer|token|secret|password|api[_-]?key)\s*[:=]\s*\S+",
               r"\1=[REDACTED]", s)
    return s[:240]

def main():
    if os.getuid() != 1001:
        raise RuntimeError("wrong_user_uid")
    if not INSTALLER.is_file() or hashlib.sha256(INSTALLER.read_bytes()).hexdigest() != INSTALLER_SHA:
        raise RuntimeError("original_installer_checksum_mismatch")
    spec = importlib.util.spec_from_file_location("luk_v2_bootstrap", str(INSTALLER))
    if spec is None or spec.loader is None:
        raise RuntimeError("bootstrap_module_unavailable")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    original = (BRIDGE / "mcp_server.py").read_text()
    if installer.MARKER in original:
        raise RuntimeError("v2_plugin_already_present")
    patched = installer.patch_mcp(original)
    with urllib.request.urlopen(installer.URL, timeout=10) as response:
        payload = response.read(150_000)
    if installer.blob_hash(payload) != installer.EXPECTED_BLOB:
        raise RuntimeError("module_git_blob_integrity_failure")
    with tempfile.TemporaryDirectory(prefix="luk-v2-offline-canary-") as temp:
        root = Path(temp)
        (root / "mcp_server.py").write_bytes(patched)
        (root / "luk_bot_v2_operator.py").write_bytes(payload)
        env = {"HOME": "/home/ubuntu", "PATH": "/usr/local/bin:/usr/bin:/bin",
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C", "PYTHONPATH": str(BRIDGE)}
        out = subprocess.run([sys.executable, "-B", "-c", CANARY], cwd=root,
                             env=env, capture_output=True, text=True, timeout=15)
    if out.returncode:
        print("OFFLINE_CANARY_FAIL", "exit=" + str(out.returncode),
              "reason=" + _safe_error(out.stderr))
    else:
        print("OFFLINE_CANARY_OK", "existing_service_not_modified=true")
    print("MCP_CHANGED=false V1_CHANGED=false V2_CHANGED=false")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("DIAGNOSTIC_BLOCKED", type(exc).__name__,
              _safe_error(str(exc)))
        sys.exit(1)
