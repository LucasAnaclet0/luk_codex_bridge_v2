"""LUK V2: run an offline, zero-credentials diagnostic in Bubblewrap."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
BWRAP = Path("/usr/bin/bwrap")
PROBE = ROOT / "isolated_probe.py"
RESULT_LIMIT = 4096

def cmd_for(snapshot_path):
    p = Path(snapshot_path).resolve(strict=True)
    if not p.is_file() or p.is_symlink():
        raise RuntimeError("SNAPSHOT_INVALID")
    if not BWRAP.is_file() or not PROBE.is_file() or PROBE.is_symlink():
        raise RuntimeError("ISOLATION_RUNTIME_MISSING")
    return [
        str(BWRAP),
        "--die-with-parent", "--unshare-all", "--cap-drop", "ALL",
        "--clearenv", "--setenv", "PATH", "/usr/bin",
        "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--tmpfs", "/home", "--tmpfs", "/root",
        "--dir", "/app", "--dir", "/input",
        "--ro-bind", str(PROBE), "/app/probe.py",
        "--ro-bind", str(p), "/input/diagnostic.json",
        "--chdir", "/app",
        "--", "/usr/bin/python3", "-I", "-B", "/app/probe.py",
    ]

def validate_result(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > RESULT_LIMIT:
        raise RuntimeError("ISOLATION_OUTPUT_INVALID")
    obj = json.loads(raw)
    expected = {
        "result": "SANDBOX_ISOLATION_VERIFIED",
        "network": "namespace_disabled",
        "host_home": "not_mounted",
        "host_etc": "not_mounted",
        "source": "explicit_read_only_snapshot",
        "autonomous_code_execution": False,
        "codex_enabled": False,
    }
    if not isinstance(obj, dict) or any(obj.get(k) != v for k, v in expected.items()):
        raise RuntimeError("ISOLATION_EVIDENCE_UNVERIFIED")
    return obj

def verify(snapshot_path):
    p = subprocess.run(
        cmd_for(snapshot_path),
        cwd="/", env={"PATH": "/usr/bin", "HOME": "/tmp"},
        capture_output=True, text=True, timeout=15, check=False,
    )
    if p.returncode != 0:
        raise RuntimeError("ISOLATION_PROCESS_NO_GO")
    return validate_result(p.stdout)

def main():
    if len(sys.argv) != 2 or sys.argv[1] != "--probe":
        raise SystemExit("Usage: python3 isolated_runner.py --probe")
    if os.getuid() != 1001:
        raise RuntimeError("HOST_IDENTITY_UNVERIFIED")
    sys.path.insert(0, str(ROOT))
    from safe_envelope import envelope, write_envelope
    import monitor
    report = envelope(monitor.snapshot())
    path = write_envelope(report)
    print(json.dumps(verify(path), sort_keys=True), flush=True)

if __name__ == "__main__":
    main()

