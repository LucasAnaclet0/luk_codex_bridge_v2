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
- `campaign_100_status`: inspect the isolated 100-trade campaign staging directory and count persisted trades without writing.
- `source_read`: bounded (up to 100 lines) reading of selected `.py` files from the MAIN, campaign staging or DEV staging. This is independent of the earlier staging-read adapter that returned `{}`.
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

## Protected /proc/1 namespace

On this VPS, user `ubuntu` received `PermissionError` opening `/proc/1/ns/pid`. The updated installer treats this as `PID_NAMESPACE_EQUIVALENCE=UNKNOWN_PERMISSION_DENIED`, never as a verified match. It still checks the expected MCP systemd unit, keeps `host_attested=false` and refuses MAIN mutations. A **readable mismatch** remains a hard failure.

## Campaign integration (read-only)

Fixed staging: `/home/ubuntu/luk_campaigns/campanha_100_20261108_230704_124781/runtime_staging`. Prior observation: 20 Python files and a zero-trade new campaign (not yet activated). The old MAIN retained 7 positions in local records and an unresolved historical HYPE order. The new tool does not delete, reclassify, reconcile or trade on these records. Zero Demo exchange exposure and isolation must be rechecked before any activation.
