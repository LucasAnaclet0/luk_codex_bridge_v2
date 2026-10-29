"""Fila SQLite mínima com estados persistentes e transições explícitas."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import uuid
from config import MAX_STDERR_BYTES, MAX_STDOUT_BYTES, MAX_TASK_PROMPT_BYTES

VALID_STATES = {"pending", "running", "completed", "failed", "uncertain"}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        timeout=10,
        isolation_level="DEFERRED",
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")

    return conn

def initialize(db_path: Path) -> None:
    """Inicializa o banco da ponte sem apagar dados existentes."""

    db_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(connect(db_path)) as conn:
        try:
            conn.execute("PRAGMA journal_mode = WAL")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL
                        CHECK(action = 'self_test'),
                    prompt TEXT,
                    status TEXT NOT NULL
                        CHECK(status IN (
                            'pending',
                            'running',
                            'completed',
                            'failed',
                            'uncertain'
                        )),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result TEXT,
                    error TEXT
                )
                """
            )

            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "prompt" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN prompt TEXT")

            conn.commit()

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

def recover_interrupted(db_path: Path) -> int:
    """Marca tarefas interrompidas como uncertain, sem reexecutá-las."""

    now = utc_now()

    with closing(connect(db_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            changed = conn.execute(
                """
                UPDATE tasks
                SET status = 'uncertain',
                    updated_at = ?,
                    finished_at = ?,
                    error = ?
                WHERE status = 'running'
                """,
                (
                    now,
                    now,
                    "Worker interrompido; revisão manual necessária. "
                    "Sem retry automático.",
                ),
            ).rowcount

            conn.commit()
            return changed

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

            # Não continuar se a recuperação não puder ser confirmada.
            raise

def create_self_test(db_path: Path) -> str:
    return create_task(db_path, "Responda exatamente: BRIDGE_SELF_TEST_OK\nNão execute comandos. Não leia outros arquivos. Não crie, modifique ou exclua arquivos.")

def create_task(db_path: Path, prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A tarefa precisa conter texto")
    if len(prompt.encode("utf-8")) > MAX_TASK_PROMPT_BYTES:
        raise ValueError("A tarefa excede o limite permitido")
    task_id = uuid.uuid4().hex
    now = utc_now()
    with closing(connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO tasks(id, action, prompt, status, created_at, updated_at) "
            "VALUES (?, 'self_test', ?, 'pending', ?, ?)",
            (task_id, prompt, now, now),
        )
        conn.commit()
    return task_id

def get_task(
    db_path: Path,
    task_id: str,
) -> Optional[sqlite3.Row]:
    """Consulta uma tarefa sem alterar seu estado."""

    with closing(connect(db_path)) as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
def list_tasks(db_path: Path, limit: int = 20):
    with closing(connect(db_path)) as conn:
        return conn.execute(
            "SELECT id, action, status, created_at, updated_at "
            "FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

def claim_next(db_path: Path) -> str | None:
    """Reivindica uma tarefa pendente somente quando a fila está segura."""

    with closing(connect(db_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Uma tarefa incerta bloqueia novas execuções.
            uncertain = conn.execute(
                """
                SELECT 1
                FROM tasks
                WHERE status = 'uncertain'
                LIMIT 1
                """
            ).fetchone()

            if uncertain is not None:
                raise RuntimeError(
                    "Fila bloqueada: existe uma tarefa uncertain "
                    "que exige reconciliação manual."
                )

            row = conn.execute(
                """
                SELECT id
                FROM tasks
                WHERE status = 'pending'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()

            if row is None:
                conn.commit()
                return None

            now = utc_now()

            changed = conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    started_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND status = 'pending'
                """,
                (now, now, row["id"]),
            ).rowcount

            if changed != 1:
                raise RuntimeError(
                    "Falha ao reivindicar tarefa: "
                    "a quantidade de linhas alteradas foi inesperada."
                )
            conn.commit()
            return row["id"]

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    
def finish(
    db_path: Path,
    task_id: str,
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Persiste o estado final somente para tarefas em execução."""

    if status not in {"completed", "failed", "uncertain"}:
        raise ValueError("Estado final inválido")

    if result is not None:
        if (
            not isinstance(result, str)
            or len(result.encode("utf-8")) > MAX_STDOUT_BYTES
        ):
            raise ValueError("Resultado inválido ou acima do limite")

    if error is not None:
        if (
            not isinstance(error, str)
            or len(error.encode("utf-8")) > MAX_STDERR_BYTES
        ):
            raise ValueError("Erro inválido ou acima do limite")

    now = utc_now()

    with closing(connect(db_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            changed = conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    updated_at = ?,
                    finished_at = ?,
                    result = ?,
                    error = ?
                WHERE id = ?
                  AND status = 'running'
                """,
                (status, now, now, result, error, task_id),
            ).rowcount

            if changed != 1:
                raise RuntimeError(
                    "Transição inválida ou tarefa não está em execução"
                )

            conn.commit()

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise


def reconcile_uncertain(db_path, task_id, reason, confirmation):
    """Reconcilia tarefa incerta sem apagar seu histórico."""
    import sqlite3
    from contextlib import closing

    if confirmation != "CONFIRMO_REVISAO_DOS_EFEITOS":
        raise ValueError("Confirmação explícita obrigatória")

    if not isinstance(reason, str) or len(reason.strip()) < 15:
        raise ValueError("Justificativa insuficiente")

    now = utc_now()

    with closing(connect(db_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                "SELECT status, error FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()

            if row is None or row["status"] != "uncertain":
                raise ValueError("Tarefa inexistente ou não incerta")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_reconciliations (
                    task_id TEXT PRIMARY KEY,
                    reconciled_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    original_error TEXT
                )
            """)

            conn.execute(
                """
                INSERT INTO task_reconciliations
                (task_id, reconciled_at, reason, original_error)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, now, reason.strip(), row["error"]),
            )

            changed = conn.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    updated_at = ?
                WHERE id = ? AND status = 'uncertain'
                """,
                (now, task_id),
            ).rowcount

            if changed != 1:
                raise RuntimeError("Falha na transição de estado")

            conn.commit()

        except Exception:
            conn.rollback()
            raise
