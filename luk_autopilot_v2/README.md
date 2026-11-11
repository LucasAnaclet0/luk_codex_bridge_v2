# LUK Autopilot V2 — Phase 1 (monitor only)

A small independent background observer on the user's Oracle VPS. It uses Python standard library only and the existing V2 Telegram sender. No local language model, Codex API key, or exchange credentials are needed by the monitor. Memory footprint is small.

## Implemented
- Health-check every 300 s for Demo Trader V2, Telegram menu, MCP Bridge, and notifier.
- SQLite database integrity and counts; stuck unknown orders older than 10 minutes, quarantined orders, and target campaign progress.
- Checks recent cycle timing and error counts from `logs/v2_demo.log`.
- Deduplicated Telegram alerts on changes; repeat critical failures at most every 4 hours.
- Atomic state file `state.json` (0600), log file `monitor.log` (0600), exclusive kernel lock against duplicate daemons.
- Explicit `MONITOR_ONLY` mode: never starts/stops/restarts trade services, never creates orders, never changes a trading DB.

## Current VPS deployment
```text
/home/ubuntu/luk_codex_bridge_v2/luk_autopilot_v2/monitor.py
screen session: luk_autopilot_v2_monitor
process: /home/ubuntu/luk_bot_trader/venv/bin/python -B monitor.py --daemon
```

## Diagnostic interface
`python -B monitor.py --status` prints last recorded health state.
`python -B monitor.py --once --no-telegram` performs a read-only check, updating only its own state.json.
`screen -ls` can confirm the monitor session.

## Limitations
The session survives ChatGPT disconnects but may not survive reboot, and does not restart itself if the entire screen session exits. Systemd system service installation requires separate, authorized setup. No periodic Codex invocation, auto code patching, root privileges, or trade order remediation yet.

The presence of INJ/USDT in `QUARANTINED` is an intentional WARNING, not proof that the order was canceled by OKX.

## Next phase
Introduce a durable job inbox with approval states, bounded runbooks for safe recoveries, dry-run tests, rollback, and optional Codex/API diagnostics. An IA workflow may propose actions, but high-impact actions need explicit authorization.


## Phase 2 deployed (diagnostic queue, no automatic Codex yet)

A separate durable SQLite job queue now runs as `diagnostic_jobs.py --daemon` inside the VPS `screen` session `luk_autopilot_v2_jobs`. Its state lives in `diagnostics.sqlite3`, independent of the trading SQLite ledger. The only accepted job types are `health`, `campaign`, and `triage`: each produces a read-only snapshot. `campaign` shows realized PnL, win count and 100-trade progress, explicitly excluding unrealized PnL. The daemon schedules a health check every hour, campaign review every six hours, and warning/critical triage no more than hourly. Jobs are bounded (max 12 pending/running), serialized by SQLite BEGIN IMMEDIATE, crash-interrupted work becomes `uncertain` with no automatic retry, and an exclusive flock prevents a second daemon. Manual `--once` cannot run alongside a daemon owner.

Read-only CLI examples:
```bash
python -B diagnostic_jobs.py --status
python -B diagnostic_jobs.py --enqueue health
python -B diagnostic_jobs.py --enqueue campaign
python -B diagnostic_jobs.py --enqueue triage
```

### Safety and remaining phases

The monitor and queue never execute trades, repairs, or restarts. The new bounded Codex gateway can submit **read-only diagnostic** tasks on critical failures only, after persistence checks; it never applies changes. Codex audits may be invoked separately by the user-facing assistant; future API integration requires explicit approvals, usage quotas, and bounded actions. A screen process may terminate on VPS reboot; a persistent systemd deployment is still unimplemented. Telegram alerts are handled by the existing `monitor.py`, not by the diagnostic queue. Quarantined INJ and venue-restricted OP and GRAM are preserved in the Demo trading ledger. A healthy service does not imply a profitable strategy.


## Phase 3 — Intelligent diagnostic gateway (operational)

`intelligent.py` is called by the running `diagnostic_jobs.py` service. It recognizes only fixed error codes from `monitor.py` (not untrusted log lines), requires a *fresh* snapshot, two distinct observations and at least ten minutes of persistent criticality, and reserves at most **one Codex diagnosis per UTC day**. It creates the task in `bridge.sqlite3` using one SQLite `BEGIN IMMEDIATE` transaction with a check for conflicting, running or uncertain tasks. An ambiguous submission permanently blocks a retry until an explicit review. Recovery resets unsent incident observation counts.

The preexisting Codex bridge worker processes the task and its Telegram notifier sends status/results. The installed VPS `worker.py` has an additional sandbox pin: prompts beginning with the exact `LUK_AUTOPILOT_READONLY_V2\\n` marker are forced to `codex exec --sandbox read-only`. The live-only worker integration is preserved as `worker_readonly_integration.patch`, because the root `worker.py` currently found on this GitHub development branch is an older, *different* version: **do not overwrite the live worker with that old file**. Worker regression tests (17) passed on VPS. The gateway uses Codex only for diagnosis/recommendations; no code deployments or trading orders are permitted by this integration.

The full autonomous code-changing/restarting vision remains unimplemented and requires an explicit action authorization and a tested DEV-to-live transaction. The service state `OK` is not proof of profitable strategies or exchange order fills.

## Persistence after VPS reboot — prepared, not yet enabled

Both monitor and jobs are running in detached `screen` sessions. This survives closing ChatGPT but **not a VPS reboot**. These deployment-ready unit templates were prepared and checked:
- `systemd/luk-autopilot-v2-monitor.service`
- `systemd/luk-autopilot-v2-jobs.service`
- `systemd/install_once.sh`

A one-time normal SSH session with root/sudo privileges is needed to enable the services, because the ChatGPT terminal uses `no-new-privs` and cannot modify `/etc/systemd/system` or enable system lingering. The installer **enables for the next boot only**, without starting duplicate daemons or restarting trading services. The Codex worker's existing `screen` supervisor also needs its own boot-persistent service if automatic Codex task processing must survive reboot. Do not claim that component is boot-safe until installed and tested.

### Useful status commands (read-only)
```
python3 -B /home/ubuntu/luk_codex_bridge_v2/luk_autopilot_v2/monitor.py --status
python3 -B /home/ubuntu/luk_codex_bridge_v2/luk_autopilot_v2/diagnostic_jobs.py --status
```


## Final security gate — 2026-11-11

The autonomous Codex task submission is now deliberately **DISABLED** (`AUTOMATIC_CODEX_ENABLED=False`). The latest independent review found that Codex CLI's `--sandbox read-only` blocks writes but does not prevent reading other user-owned files, including potentially sensitive local credentials, and cannot by itself exclude configured external MCP capabilities. A prompt is not a security boundary. Automatic critical-error analysis via Codex MUST remain off until a separate, attested and secret-isolated execution environment is available and tested.

Installed and active: independent monitoring every 5 min, Telegram warning/critical alerts, durable local diagnostic queue, automated health/campaign reports, incident triage, fixed exchange restrictions, code backups and tests. Codex can still be invoked manually under the user's supervision. No production code patches/restarts/trades are performed by the Autopilot.

Both detached `screen` services survive closing ChatGPT but not reboot. The prepared systemd files still need a one-time privileged installation, not possible through the bridge's `no-new-privs` Ubuntu terminal.

Verified on VPS: 32 Autopilot tests and 17 Bridge worker tests passed. The GitHub CI must also pass before calling the repository release validated.


## Phase 4 — Offline sandbox isolation (2026-11-11)

The VPS now has a verified **Bubblewrap** diagnostic sandbox. `safe_envelope.py` creates a minimal, strict allowlist summary of current health state. It never includes credentials, process environment, trade IDs, arbitrary logs, or original error text. Snapshot files are private (directory `0700`, JSON `0600`). The snapshot is mounted read-only into a separate PID/user/network/mount namespace by `isolated_runner.py`.

The isolated process can see only a read-only snapshot plus its own read-only probe script and the minimum system Python runtime. `/home`, `/root`, and `/tmp` are private temporary filesystems; no host `/etc` is mounted. The sandbox has no network access (`--unshare-all`), Linux capabilities are dropped, and all externally derived content is reduced to fixed numeric/boolean/enumerated fields. The on-VPS tests confirmed hidden credential and Trader paths, read-only input, failed outbound network connectivity, and 6 dedicated isolation regression tests.

A new diagnostic job `isolation` runs this proof automatically, scheduled every six hours in the existing durable jobs queue, and records `SANDBOX_ISOLATION_VERIFIED` or fails closed. Run manually through the queue by enqueueing the `isolation` job; do not bypass queue locks.

**Important limitation:** This sandbox is deliberately **OFFLINE**. It does not run Codex, and automated Codex dispatch remains disabled. Using it for model-powered diagnostics will require a separately verified authentication broker or remote provider that does not expose tokens or sensitive host paths to the model, and security regression tests for that broker. Do **not** simply mount `~/.codex/auth.json` or pass an API key into an agent-enabled sandbox. No trading order, production code patch, or restart permission has been added.
