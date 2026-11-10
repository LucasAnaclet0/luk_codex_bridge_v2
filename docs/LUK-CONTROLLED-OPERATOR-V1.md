# LUK Controlled Operator V1 (Terminal V2 extension)

A one-command MCP bootstrap adds `vps_controlled_operator_v1` to the existing
authenticated codex-bridge-v2 connection. The installer backs up and restarts
only the MCP service; it does **not** touch MAIN or Signals.

## Why

The existing `vps_main_maintenance_v2` can already send exact code edits into
`/home/ubuntu/luk_codex_bridge_v2/.luk-main-dev-staging` with
`dev_stage_replace`. This operator supplies the missing controlled path from
validated DEV edits to production MAIN source files, and guarded stop/start
operations. It does not depend on a Codex quota.

## Allowed operations

- `status`: MAIN PID/start-ticks, kernel FLOCK proof, campaign process count,
  DEMO flag status. Observation is still **not host attestation**.
- `dev_verify`: AST syntax check for one already-staged allowed Python file.
  Does not execute tests or modify MAIN.
- `main_stop`: requires confirmation `AUTORIZO_PARAR_MAIN_DEMO`, exact PID and
  process start ticks, verified cwd/argv and kernel lock ownership. Sends
  SIGINT, never SIGKILL. Never kills an unverified process.
- `promote_dev`: requires confirmation `AUTORIZO_ALTERAR_CODIGO_MAIN_DEMO`,
  exact live and staged SHA-256 hashes, and MAIN stopped with no kernel lock.
  Backs up original with restricted permissions, writes only one allowlisted
  `.py` source file atomically and journals the change. Does **not** restart.
- `rollback_dev`: requires `AUTORIZO_RESTAURAR_CODIGO_MAIN_DEMO`, exact
  journal/file and source hashes, and MAIN stopped.
- `main_start`: requires `AUTORIZO_INICIAR_MAIN_DEMO`, MAIN absent, kernel
  lock absent, campaign absent, explicit `USAR_TESTNET=true` and
  `MODO_TESTE=true` in MAIN .env and static OKX sandbox guards. Starts only
  the fixed MAIN Python entry point without arbitrary shell or sudo.
  A successful process launch is **not proof of successful trades**.

The bot is restricted to specific file names. No .env/credentials, arbitrary
paths or shell commands can be sent through this tool. Changes to files are
implemented through the existing allowlisted V2 DEV editor.

## Limits & safety gates

- This is not root access, full host attestation or blanket authorization.
- No restart, mutation or trading action should be invoked until the user has
  explicitly approved that specific operation and its expected effect.
- `dev_verify` is **syntax only** (`tests_executed=false`). Tests, source
  reconciliation, OKX Demo exchange-side state, and HYPE uncertainty require
  separate evidence before deploying a bot patch.
- The original MAIN has 7 locally registered positions and a HYPE-USDT
  unresolved order. Stopping it pauses the watchdog; schedule this only after
  checking exchange positions and transition safety. The operator never
  clears positions and does not launch the 100-trade campaign.
- Use the existing campaign snapshot/status tool until a separate
  isolated-activation gate is tested.

## After bootstrap

Refresh the codex-bridge-v2 plugin tool definitions. Invoke only
`vps_controlled_operator_v1(action="status")` initially. It reports the
PID + start ticks needed for a later approved stop. Do not run
`main_stop`, `promote_dev` or `main_start` simply because they are available.

## Recovery

Installer prints its MCP backup path and attempts rollback after a failed
canary/restart/heartbeat. File promotions record per-change backup paths
under the restricted `.luk-controlled-ops/backups` directory.

No long-running background agent is installed. Actions execute only when
triggered via the authenticated Bridge.
