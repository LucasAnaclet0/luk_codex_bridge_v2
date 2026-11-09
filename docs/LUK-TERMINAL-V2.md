# LUK Terminal V2 — controlled installation

Goal: reduce manual copying of LUK Bot Trader diagnostics by exposing a bounded
MCP tool on the existing authenticated Bridge. **This is not an arbitrary shell.**

## Scope

The new `vps_main_maintenance_v2` tool supports:
- `snapshot`: running MAIN candidates, visible .demo_bot.lock file descriptors,
  recent logs and persisted position summaries.
- `positions`: allowlisted fields only; does not return .env or exchange keys.
- `errors` / `activity`: recent bounded log samples and counters.
- `dev_status`: source-file hashes for a fixed list of Python files.
- `dev_stage_replace`: exact-hash, exact-one-occurrence text edits to isolated
  DEV staging files only; syntax parsing is required before saving.
- `dev_validate`: syntax-only check; NOT a trading regression test.
- `dev_discard`: remove a staged DEV file.

No MAIN restart, live bot source write, exchange API call, automatic deployment,
host attestation claim, arbitrary subprocess execution or credential read is
allowed by this new tool. The existing Bridge MCP auth remains required.

## Install

Run as `ubuntu` (uid=1001) on the observed VPS. Fetch the bootstrap from the
**pinned commit** and verify SHA-256 before `python3 -B ... --apply`.
Do not run with sudo. The bootstrap checks service identity, downloads a
commit-pinned module whose Git blob SHA-1 is checked, backs up MCP code,
registers the tool, runs an offline canary, restarts **only**
`luk-codex-mcp.service`, and verifies an unauthenticated request is rejected
(401). On failure, it attempts an MCP-only rollback.

Installation does not touch the running Bot MAIN or Signals.

After installation, refresh ChatGPT Bridge tool definitions and call
`vps_main_maintenance_v2(action="snapshot")`.

## Limitations

A running Python process and an open lock FD are **not proof of operational
health** or a verified kernel flock. The tool marks `host_attested=false` and
`go_for_main_mutations=false` and never upgrades them.

Snapshot can show that positions are stuck and orders are unresolved, but
exchange-side order execution must be reconciled separately. No position state
should be deleted just to free slots.

## Recovery

The installer prints the exact backup path. If health checks fail, it restores
the prior MCP code and removes the newly added module. If runtime issues arise,
recover the specific MCP backup (without modifying the bot), then restart the
MCP service; do not erase backups.

## Tests

`python3 -m unittest discover -s tests -p test_luk_main_maintenance_v2.py -v`
plus offline AST checks of the bootstrap. No exchange credentials used.
