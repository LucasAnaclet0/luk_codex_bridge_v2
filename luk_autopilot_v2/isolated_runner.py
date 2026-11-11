"""Hardened Bubblewrap offline diagnostic proof; never invokes model/venue APIs."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
BWRAP = Path("/usr/bin/bwrap")
PRLIMIT = Path("/usr/bin/prlimit")
PROBE = ROOT / "isolated_probe.py"
RESULT_LIMIT = 4096

def safe_file(source):
    p = Path(source)
    if not p.is_absolute() or p.is_symlink() or not p.is_file():
        raise RuntimeError("SNAPSHOT_UNSAFE")
    ancestor = p.parent
    while True:
        if ancestor.is_symlink():
            raise RuntimeError("SNAPSHOT_PARENT_SYMLINK")
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    if p.resolve(strict=True) != p:
        raise RuntimeError("SNAPSHOT_PATH_REWRITTEN")
    s = p.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 1001 or (s.st_mode & 0o777) != 0o600:
        raise RuntimeError("SNAPSHOT_OWNER_OR_MODE_INVALID")
    return p

def cmd_for(snapshot_path):
    source = safe_file(snapshot_path)
    if not BWRAP.is_file() or not PRLIMIT.is_file():
        raise RuntimeError("ISOLATION_DEPENDENCY_MISSING")
    if not PROBE.is_file() or PROBE.is_symlink():
        raise RuntimeError("PROBE_SOURCE_UNVERIFIED")
    return [
        str(PRLIMIT), "--as=268435456", "--cpu=12", "--fsize=8192",
        "--nofile=64", "--nproc=512", "--core=0", "--",
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
        "--ro-bind", str(source), "/input/diagnostic.json",
        "--chdir", "/app",
        "--", "/usr/bin/python3", "-I", "-B", "/app/probe.py",
    ]

def validate_result(raw, expected):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > RESULT_LIMIT:
        raise RuntimeError("ISOLATION_OUTPUT_TOO_LARGE")
    output = json.loads(raw)
    required = {
        "result": "SANDBOX_ISOLATION_VERIFIED",
        "network": "namespace_disabled",
        "host_home": "not_mounted",
        "host_etc": "not_mounted",
        "source": "explicit_read_only_snapshot",
        "autonomous_code_execution": False,
        "codex_enabled": False,
        "severity": expected["severity"],
        "snapshot_sha256": expected["hash"],
    }
    if (type(output) is not dict or set(output) != set(required)
            or any(type(output[k]) is not type(v) or output[k] != v
                   for k, v in required.items())):
        raise RuntimeError("ISOLATION_EVIDENCE_NOT_TRUSTED")
    return output

def verify(snapshot_path):
    source = safe_file(snapshot_path)
    with source.open("rb") as f:
        content = f.read(4097)
    if len(content) > 4096:
        raise RuntimeError("SNAPSHOT_TOO_LARGE")
    from safe_envelope import validate_payload
    data = validate_payload(json.loads(content))
    expected = {
        "severity": data["severity"],
        "hash": hashlib.sha256(content).hexdigest(),
    }
    with tempfile.TemporaryFile() as stdout:
        process = subprocess.run(
            cmd_for(source),
            cwd="/",
            env={"PATH": "/usr/bin", "HOME": "/tmp"},
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError("ISOLATION_PROCESS_NO_GO")
        stdout.seek(0)
        raw = stdout.read(RESULT_LIMIT + 1)
        if len(raw) > RESULT_LIMIT:
            raise RuntimeError("ISOLATION_OUTPUT_TOO_LARGE")
        output = validate_result(raw.decode("utf-8"), expected)
    with source.open("rb") as f:
        observed = f.read(4097)
    if not source.is_file() or hashlib.sha256(observed).hexdigest() != expected["hash"]:
        raise RuntimeError("SNAPSHOT_REPLACED_DURING_CHECK")
    return output

def snapshot_proof(snapshot):
    from safe_envelope import ROOT as SNAPSHOT_ROOT
    from safe_envelope import envelope, write_envelope
    path = SNAPSHOT_ROOT / ("probe-" + secrets.token_hex(16) + ".json")
    write_envelope(envelope(snapshot), path)
    try:
        return verify(path)
    finally:
        path.unlink(missing_ok=True)

def main():
    if sys.argv[1:] != ["--probe"]:
        raise SystemExit("Usage: python3 isolated_runner.py --probe")
    if os.getuid() != 1001:
        raise RuntimeError("HOST_IDENTITY_UNVERIFIED")
    sys.path.insert(0, str(ROOT))
    import monitor
    print(json.dumps(snapshot_proof(monitor.snapshot()), sort_keys=True), flush=True)

if __name__ == "__main__":
    main()

