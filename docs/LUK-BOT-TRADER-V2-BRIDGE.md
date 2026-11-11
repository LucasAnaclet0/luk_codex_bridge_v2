# LUK Bot Trader V2 — Bridge integration

This is an independent add-on to the already authenticated Codex Bridge V2
on the Oracle VPS; it does not replace the original V1 tools.

## Pinned identity and first use

Expected V2 directory: `/home/ubuntu/luk_bot_trader_v2`. The systemd unit
is discovered among `luk-*v2*.service` candidates and selected only if
its User is `ubuntu`, its WorkingDirectory is **exactly** V2 and its
ExecStart contains the V2 root. All other units are refused. If the expected
path or unit differs, `status` reports missing/unverified; it does not mutate.

The one-time installer checks its own pinned Git blob, creates a local MCP
backup, registers `vps_bot_trader_v2`, performs an AST/offline canary,
restarts **only** `luk-codex-mcp.service`, checks local HTTP 401 and attempts
MCP-only rollback on failure. No changes to MAIN V1, Bot V2 or Signals.

Refresh ChatGPT plugin tools after installation, then call `status` followed
by `file_inventory` and narrow `source_read` to understand the V2 layout.

## Actions

- `status`: service identity, observed V2 processes, SQLite table counts
  (not verified completed trades), summary-only journal error markers, files.
- `file_inventory`: filenames and sizes, excluding secrets and env files.
- `source_read`: max 100 source lines of allowlisted Python files.
- `dev_stage_patch`: match exact existing SHA-256 and unique text anchor;
  write only in `.luk-bot-v2-dev-staging`; AST syntax check, no live bot writes.
- `dev_verify`: confirm syntax and hashes of DEV and live files.
- `service_stop`: exact confirmed service main PID and start-ticks plus
  `AUTORIZO_PARAR_BOT_V2_DEMO` authorization.
- `service_start`: V2 service stopped, explicitly present DEMO code guards,
  `AUTORIZO_INICIAR_BOT_V2_DEMO`; starts fixed systemd unit, not arbitrary
  commands. Service active != trades verified.
- `promote_dev`: requires V2 stopped, matching hashes and
  `AUTORIZO_APLICAR_CODIGO_BOT_V2_DEMO`, backup and atomic source update.
- `rollback`: V2 stopped, last deployment backup hashes match,
  `AUTORIZO_RESTAURAR_BOT_V2_DEMO`.

All mutation requests must be explicitly approved per step and checked at
runtime. The .env is never exposed to ChatGPT. V1 is out of scope.

## Limits

The V2 installation directory and systemd unit are hypotheses until first
live status. DB row counts cannot establish whether 100 trades are completed:
fills, close statuses, unique order IDs, and fees need reconciliation.
`dev_verify` performs AST syntax checking, not runtime tests. Operator actions
do not constitute exchange account isolation or host attestation.

The authenticated bridge's service account still needs permissions to stop
or start the V2 unit; if restricted, the operation will fail closed without
trying arbitrary sudo or replacing sudoers.

Neither V2 nor MAIN is to be restarted merely to validate tool presence.
