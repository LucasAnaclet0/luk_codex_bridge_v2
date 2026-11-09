"""Fixed, read-only diagnostic probes for an *unverified* process namespace.

This module is NOT an arbitrary command executor or a proof of host identity.
Only constant argv vectors are allowed. Never accept paths, commands, flags,
or environment variables from remote callers.
"""
from __future__ import annotations

import subprocess

# Constant commands, no shell and no user-controlled arguments.
PROBES = {
    "system": (
        ("architecture", ("/usr/bin/uname", "-sm")),
        ("uptime", ("/usr/bin/uptime", "-p")),
    ),
    "resources": (
        ("memory", ("/usr/bin/free", "-h")),
        ("disk", ("/usr/bin/df", "-h", "/")),
    ),
    "bridge_services": (
        ("bridge_states", (
            "/usr/bin/systemctl", "is-active",
            "luk-codex-mcp.service",
            "luk-codex-dashboard.service",
            "luk-codex-notifier.service",
            "luk-host-agent.service",
        )),
    ),
}
MAX_STDOUT_CHARS = 2400
MAX_STDERR_CHARS = 300
TIMEOUT_SECONDS = 5


def _bounded(value: str, limit: int) -> str:
    """Keep the beginning of diagnostic output and mark truncation explicitly."""
    return value[:limit] + ("\n[TRUNCATED]" if len(value) > limit else "")


def run_diagnostic(operation: str, *, runner=None) -> dict:
    """Run a fixed, read-only probe in the caller's CURRENT namespace.

    Nothing here attests that the caller is on the host, or that the namespaced
    systemd/process view corresponds to the real VPS. GO remains false.
    """
    if type(operation) is not str or operation not in PROBES:
        raise ValueError("unsupported diagnostic operation")
    if runner is None:
        runner = subprocess.run
    observations = []
    for label, argv in PROBES[operation]:
        try:
            completed = runner(
                list(argv),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_SECONDS,
                cwd="/",
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            observations.append({
                "name": label,
                "returncode": completed.returncode,
                "stdout": _bounded(completed.stdout, MAX_STDOUT_CHARS),
                "stderr": _bounded(completed.stderr, MAX_STDERR_CHARS),
            })
        except (OSError, subprocess.TimeoutExpired) as exc:
            observations.append({
                "name": label,
                "returncode": None,
                "stdout": "",
                "stderr": type(exc).__name__,
            })
    return {
        "operation": operation,
        "scope": "CURRENT_PROCESS_NAMESPACE_ONLY",
        "host_attested": False,
        "host_health_verified": False,
        "go_for_mutations": False,
        "observations": observations,
    }
