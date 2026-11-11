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
