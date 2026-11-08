# LUK Bridge — Codex quota guard (DEV prototype only)

## Why

On 2026-11-08 the active worker returned `codex_nonzero_exit`; direct stderr
confirmed the account's Codex usage limit was reached, with a *human-readable*
retry hint `Nov 11th, 2026 7:17 AM` and no timezone. Repeated attempts caused
new `uncertain` tasks. **This is an account quota, not evidence of an OS fault.**

## Implemented prototype

- `quota_guard.py` classifies *explicit Codex ERROR lines* with nonzero exit.
- A standalone SQLite ledger persists a **global dispatch pause** on quota
  failure; concurrent incident reports are serialized. Missing/corrupt gate
  state **denies dispatch**.
- No auto-retry, no auto-resume at guessed clock time, no quota bypass.
- Task results with nonzero exit must still be treated as **uncertain** until
  independently reviewed. Quota message is not proof of absence of side effects.
- No prompt, stderr or token is persisted in the prototype.

## Not implemented (NO-GO)

- **No integration with the live Bridge**: its `worker.py`, `database.py` and
  the SQLite queue were not changed by this deliverable.
- No authenticated supervisor or separate process identity yet. Same-UID DB
  tampering remains possible; SQLite does **not** confer host-level isolation.
- No trusted positive-capacity probe or safe resume procedure. `retry_hint`
  is **unverified text only**. Manual reconciliation and admission decisions
  remain required.
- No Telegram notification / production quota automation yet.
- Remote VPS version may differ from this public GitHub baseline; verify
  deployment contents, ownership, permissions, and tests before any integration.

## Offline test command

`PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_quota_guard.py' -v`

## Safe integration sequence (future, separate authorization)

1. Compare deployed modules to GitHub DEV baseline; take a consistent backup.
2. Authorize a dedicated DEV identity plus access boundaries first.
3. Add a **read-only gate check before claim** and persist quota pause on an
   actual nonzero Codex quota error. Preserve the *original* task as `uncertain`.
4. Define a trusted, independent admission/probe channel. No self-attested
   worker PID or human-readable reset time may authorize resumption.
5. Test crash/restart, concurrency, corruption, stale state, no retry, and
   one synthetic canary before any real service changes.

**Status: GO=false, auto_submit=false. Main, Signals and finances untouched.**
