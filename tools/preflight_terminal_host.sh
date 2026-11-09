#!/usr/bin/env bash
# LUK Terminal Bridge: independent SSH-side preflight, no mutations.
# Run as the usual unprivileged SSH user. No sudo, secrets, writes or restarts.
set -uo pipefail

echo "=== LUK TERMINAL PRE-FLIGHT v1 (READ_ONLY) ==="
echo "mode=read_only"
echo "mutation_authorized=false"
echo "host_attested=false"
echo "note=SSH-side observations are not cryptographic host attestation"
echo

echo "=== IDENTITY ==="
printf 'hostname='
hostname 2>/dev/null || echo "UNAVAILABLE"
printf 'user='
id -un 2>/dev/null || echo "UNAVAILABLE"
printf 'uid='
id -u 2>/dev/null || echo "UNAVAILABLE"
printf 'architecture='
uname -m 2>/dev/null || echo "UNAVAILABLE"
printf 'pid1_comm='
cat /proc/1/comm 2>/dev/null || echo "UNAVAILABLE"
printf 'self_pid_ns='
readlink /proc/self/ns/pid 2>/dev/null || echo "UNAVAILABLE"
printf 'pid1_pid_ns='
readlink /proc/1/ns/pid 2>/dev/null || echo "UNAVAILABLE"
echo

echo "=== SERVICE METADATA ==="
for service in luk-codex-mcp.service luk-codex-dashboard.service luk-codex-notifier.service luk-host-agent.service; do
    printf '[%s]\n' "$service"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl show --no-pager \
            -p LoadState -p ActiveState -p SubState -p MainPID \
            -p User -p FragmentPath -p ControlGroup \
            "$service" 2>&1 | head -n 12 || true
    else
        echo "systemctl=UNAVAILABLE"
    fi
done
echo

echo "=== BRIDGE LOCAL WORKTREE (NO NETWORK) ==="
bridge_dir="/home/ubuntu/luk_codex_bridge_v2"
if [[ -d "$bridge_dir" ]]; then
    printf 'bridge_dir=present\n'
    stat -c 'bridge_dir_owner=%U group=%G mode=%a' "$bridge_dir" 2>/dev/null || true
    if [[ -d "$bridge_dir/.git" ]] && command -v git >/dev/null 2>&1; then
        printf 'bridge_local_commit='
        git -C "$bridge_dir" rev-parse --verify HEAD 2>/dev/null || echo "UNAVAILABLE"
        printf 'bridge_local_branch='
        git -C "$bridge_dir" symbolic-ref --quiet --short HEAD 2>/dev/null || echo "DETACHED"
    else
        echo 'bridge_git=UNAVAILABLE'
    fi
    for name in mcp_server.py terminal_readonly.py worker.py; do
        if [[ -f "$bridge_dir/$name" ]]; then
            printf '[%s]\n' "$name"
            stat -c 'owner=%U group=%G mode=%a size=%s' "$bridge_dir/$name" 2>/dev/null || true
            if command -v sha256sum >/dev/null 2>&1; then
                sha256sum "$bridge_dir/$name"
            fi
        else
            printf '%s=NOT_FOUND\n' "$name"
        fi
    done
else
    echo 'bridge_dir=NOT_FOUND'
fi
echo

echo "=== PROCESS VISIBILITY (METADATA ONLY) ==="
printf 'proc_1_cgroup='
head -n 1 /proc/1/cgroup 2>/dev/null || echo "UNAVAILABLE"
printf 'proc_self_cgroup='
head -n 1 /proc/self/cgroup 2>/dev/null || echo "UNAVAILABLE"

echo
echo "=== RESULT ==="
echo "NO_GO_FOR_MUTATIONS"
echo "NO_SERVICES_CHANGED"
echo "REPORT_DONE"
