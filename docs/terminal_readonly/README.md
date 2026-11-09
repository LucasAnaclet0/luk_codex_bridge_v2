# LUK terminal bridge — read-only offline prototype (2026-11-08)

## Purpose

Prepare a **fixed diagnostic tool** for ChatGPT without Codex calls or a shell
service. This is NOT a full terminal, not a deployment, and not an authorization
to run arbitrary Linux commands. Running an LLM as root is prohibited.

New MCP tool: `vps_diagnostics_readonly`, with a single enum input:
`system`, `resources`, or `bridge_services`. Only predefined subprocess argv
lists execute. Never use `shell=True`, caller-provided paths, flags, variables,
or generic `exec`/shell strings. Captured output is bounded. Each process has
a 5-second timeout.

## Hard NO-GO conditions

- MCP's observed process namespace **is not independently attested as the VPS
  host**. The tool always returns `host_attested=false`, `go_for_mutations=false`.
- The public GitHub DEV branch may differ from the live VPS deployment.
- No modifications or restarts of Main, Signals, Bridge services, or host.
- Do **not** add this tool to the live bridge unless a human independently
  verifies runtime identity, source/commit alignment, service ownership,
  least-privilege Unix identity, and networking/authentication.
- This tool does not expand production permissions. A prompt asking for a
  "full terminal" does not authorize a broad remote-code-execution endpoint.

## Testing

From the branch checkout:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_terminal_readonly.py' -v
```

Run existing bridge tests as well, but do not treat successful unit tests as
proof of host binding, security or deployment health.

## Safe rollout later (separately reviewed)

1. Compare deployed code to the exact commit and collect host attestation.
2. Back up current configuration, audit MCP auth and network exposure.
3. Run as dedicated non-root user with no bot write permissions.
4. Canary only `system` and `resources` first; reconcile data to the SSH
   terminal; validate namespace/ownership and log redaction.
5. Expose only if all evidence is positive. Subsequent limited write workflows
   require separate approvals, allowlists, backups and rollback evidence.
