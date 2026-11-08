"""Offline prototype: fail-closed guard for Codex usage-limit failures.

Not connected to the live Bridge. An execution that exits nonzero remains
UNCERTAIN until reviewed; detecting a quota is never proof of no side effects.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_QUOTA_ERROR = re.compile(
    r"^ERROR:\s*(?:you['\u2019]ve hit your usage limit|"
    r"you have reached your usage limit|usage limit exceeded|"
    r"you['\u2019]ve reached your usage limit)\b",
    re.IGNORECASE | re.MULTILINE,
)
_HINT = re.compile(r"\btry again at ([^.\n]{4,75})", re.IGNORECASE)
_TASK_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class QuotaDiagnosis:
    detected: bool
    reason: str | None = None
    retry_hint_unverified: str | None = None


def classify_codex_failure(returncode: int, stderr: bytes | str) -> QuotaDiagnosis:
    """Detect explicit Codex ERROR lines only; never infer permission to retry.

    The hint has no trusted timezone. It is for human display only.
    """
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise TypeError("returncode must be an integer")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    if not isinstance(stderr, str):
        raise TypeError("stderr must be bytes or str")
    if returncode == 0 or not _QUOTA_ERROR.search(stderr):
        return QuotaDiagnosis(False)
    hints = _HINT.findall(stderr)
    hint = hints[-1].strip() if hints else None
    return QuotaDiagnosis(True, "codex_usage_limit", hint)


class QuotaGuard:
    """Durable global pause. Prototype has no live authority to resume.

    SQLite transactions enforce consistent reads/writes among cooperating
    clients. The runtime must protect the DB against same-UID tampering.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def initialize(self):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS quota_pause (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                paused INTEGER NOT NULL CHECK (paused IN (0,1)),
                reason TEXT NOT NULL,
                last_task_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS quota_incidents (
                task_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )""")
            db.commit()

    def _paused(self, db):
        row = db.execute("SELECT paused FROM quota_pause WHERE singleton=1").fetchone()
        return bool(row and row[0] == 1)

    def may_dispatch(self) -> bool:
        """Failure to read or validate DB is a denial (not an exception)."""
        try:
            with self._connect() as db:
                check = db.execute("PRAGMA quick_check").fetchone()
                if check is None or check[0] != "ok":
                    return False
                # A missing schema means the guard was never configured.
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"quota_pause", "quota_incidents"} <= tables:
                    return False
                return not self._paused(db)
        except (OSError, sqlite3.Error):
            return False

    def record_usage_limit(self, task_id: str, diagnosis: QuotaDiagnosis) -> None:
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task id")
        if diagnosis != QuotaDiagnosis(True, "codex_usage_limit", diagnosis.retry_hint_unverified):
            raise ValueError("not a validated usage-limit diagnosis")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reason FROM quota_incidents WHERE task_id=?", (task_id,)).fetchone()
            if row is not None and row[0] != diagnosis.reason:
                raise ValueError("conflicting duplicate incident")
            db.execute("INSERT OR IGNORE INTO quota_incidents VALUES (?,?,?)", (task_id, diagnosis.reason, now))
            db.execute("""INSERT INTO quota_pause VALUES (1,1,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET paused=1,
                reason=excluded.reason,last_task_id=excluded.last_task_id,
                updated_at=excluded.updated_at""", (diagnosis.reason, task_id, now))
            db.commit()

    def status(self) -> dict:
        """Read-only diagnostic; do not include prompts, stderr or secrets."""
        with self._connect() as db:
            row = db.execute("SELECT paused, reason, last_task_id, updated_at FROM quota_pause WHERE singleton=1").fetchone()
        if row is None:
            return {"paused": False, "reason": None}
        return dict(zip(("paused", "reason", "last_task_id", "updated_at"), (bool(row[0]), *row[1:])))
