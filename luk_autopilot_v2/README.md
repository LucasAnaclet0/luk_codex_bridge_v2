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

Neither the monitor nor the job daemon executes trades, repairs code, restarts services, or calls Codex/LLMs automatically. Codex audits may be invoked separately by the user-facing assistant; future API integration requires explicit approvals, usage quotas, and bounded actions. A screen process may terminate on VPS reboot; a persistent systemd deployment is still unimplemented. Telegram alerts are handled by the existing `monitor.py`, not by the diagnostic queue. Quarantined INJ and venue-restricted OP and GRAM are preserved in the Demo trading ledger. A healthy service does not imply a profitable strategy.
