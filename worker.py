"""Worker manual: dry-run por padrão; ação única e fixa."""
import argparse
import selectors
import fcntl
import os
import signal
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path


from config import (
    APPROVED_CODEX_BIN,
    BRIDGE_DIR,
    CODEX_BIN,
    DB_PATH,
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    PROCESS_TERMINATE_GRACE_SECONDS,
    TASK_TIMEOUT_SECONDS,
    WORKDIR,
)
from database import (
    initialize,
    claim_next,
    finish,
    get_task,
    recover_interrupted,
)

class WorkerAlreadyRunning(RuntimeError):
    """Outro processo já mantém o lock exclusivo do worker."""


class ExecutionNotStarted(RuntimeError):
    """Falha segura antes de criar o processo Codex."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ExecutionUncertain(RuntimeError):
    """O processo começou, mas o resultado não pode ser confirmado."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@contextmanager
def exclusive_worker_lock(lock_path: Path):
    """Impede workers cooperantes de alterar a fila simultaneamente."""

    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    handle = None

    try:
        # O arquivo deve ficar acessível somente ao usuário proprietário.
        fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        handle = os.fdopen(fd, "r+")

        os.chmod(lock_path, 0o600)

        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            raise WorkerAlreadyRunning(
                "Já existe outro worker com o lock exclusivo; "
                "nenhuma tarefa foi alterada."
            ) from None

        yield

    finally:
        if handle is not None:
            handle.close()


FIXED_PROMPT = (
    "Responda exatamente: BRIDGE_SELF_TEST_OK\n"
    "Não execute comandos. Não leia outros arquivos. "
    "Não crie, modifique ou exclua arquivos."
)

class _BoundedCapture:
    def __init__(self, limit: int):
        if limit <= 0:
            raise ValueError("O limite de captura deve ser positivo")

        self.limit = limit
        self.data = bytearray()
        self.exceeded = False

    def drain(
        self,
        stream,
        stop_event: threading.Event,
        error_event: threading.Event,
    ) -> None:
        try:
            while not stop_event.is_set():
                read = getattr(stream, "read1", stream.read)
                chunk = read(4096)

                if not chunk:
                    return

                remaining = self.limit - len(self.data)
                accepted = min(len(chunk), remaining)

                if accepted > 0:
                    self.data.extend(chunk[:accepted])

                if len(chunk) > accepted:
                    self.exceeded = True
                    stop_event.set()
                    return

        except Exception:
            error_event.set()


def _approved_executable_fd(configured: str, approved: str) -> tuple[int, Path]:
    """Open the allowlisted executable without following a final symlink.

    Passing the open descriptor to exec pins this invocation to the opened
    inode. A hostile same-UID process can still replace the approved file
    before it is opened; deployment must protect that directory separately.
    """
    fd = None
    try:
        configured_path = Path(configured)
        approved_path = Path(approved)
        if not configured_path.is_absolute() or not approved_path.is_absolute():
            raise ValueError
        canonical = configured_path.resolve(strict=True)
        approved_canonical = approved_path.resolve(strict=True)
        if str(configured_path) != str(canonical) or str(approved_path) != str(approved_canonical):
            raise ValueError
        if canonical != approved_canonical:
            raise ValueError
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(configured_path), flags)
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or not (file_stat.st_mode & 0o111):
            raise ValueError
        descriptor_path = Path(f"/proc/self/fd/{fd}")
        if descriptor_path.resolve(strict=True) != approved_canonical:
            raise ValueError
        if not os.access(str(descriptor_path), os.X_OK):
            raise ValueError
        return fd, canonical
    except (OSError, RuntimeError, ValueError):
        if fd is not None:
            os.close(fd)
        raise ExecutionNotStarted("codex_executable_validation_failed") from None


def _stop_process_group(proc: subprocess.Popen) -> bool:
    """
    Tenta encerrar o grupo sem recolher o processo líder antes do SIGKILL.

    Retorna False se não for possível confirmar o encerramento do líder
    ou se houver falha de sinalização que impeça confirmar o resultado.
    """
    signal_error = False
    group_missing = False

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        group_missing = True
    except OSError:
        signal_error = True

    if not group_missing:
        time.sleep(PROCESS_TERMINATE_GRACE_SECONDS)

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            signal_error = True

    # Só recolhe o líder depois da tentativa de SIGKILL ao grupo.
    try:
        proc.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return False

    return not signal_error


def _run_bounded_process(
    argv: list[str],
    *,
    input_text: str,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
    executable: str | None = None,
    pass_fds: tuple[int, ...] = (),
) -> tuple[bytes, bytes, int]:
    if timeout_seconds <= 0 or stdout_limit <= 0 or stderr_limit <= 0:
        raise ExecutionNotStarted("invalid_process_limits")

    # Não iniciar se a plataforma não permitir observar o líder
    # sem recolhê-lo imediatamente.
    if not all(
        hasattr(os, attr)
        for attr in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
    ):
        raise ExecutionNotStarted("safe_process_observation_unavailable")

    deadline = time.monotonic() + timeout_seconds

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
            executable=executable,
            pass_fds=pass_fds,
            bufsize=0,
        )
    except (OSError, ValueError):
        raise ExecutionNotStarted("codex_process_start_failed") from None

    selector = None
    stdout_data = bytearray()
    stderr_data = bytearray()
    input_data = memoryview(input_text.encode("utf-8"))
    input_offset = 0
    stdin_open = True
    stdout_open = True
    stderr_open = True
    cleanup_attempted = False

    def close_registered(stream) -> None:
        try:
            selector.unregister(stream)
        except (KeyError, ValueError):
            pass

        try:
            stream.close()
        except OSError:
            pass

    try:
        selector = selectors.DefaultSelector()

        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)

        if input_data:
            selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            proc.stdin.close()
            stdin_open = False

        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")

        while True:
            try:
                info = os.waitid(
                    os.P_PID,
                    proc.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except InterruptedError:
                continue
            except OSError:
                raise ExecutionUncertain(
                    "codex_process_state_unavailable"
                ) from None

            leader_exited = info is not None and info.si_pid == proc.pid

            if (
                leader_exited
                and not stdin_open
                and not stdout_open
                and not stderr_open
            ):
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExecutionUncertain("codex_timeout")

            events = selector.select(min(remaining, 0.05))

            for key, _mask in events:
                stream = key.fileobj
                kind = key.data

                if kind == "stdin":
                    try:
                        written = os.write(
                            stream.fileno(),
                            input_data[
                                input_offset:input_offset + 4096
                            ],
                        )
                    except BlockingIOError:
                        continue
                    except OSError:
                        raise ExecutionUncertain(
                            "codex_input_delivery_uncertain"
                        ) from None

                    if written <= 0:
                        raise ExecutionUncertain(
                            "codex_input_delivery_uncertain"
                        )

                    input_offset += written

                    if input_offset >= len(input_data):
                        close_registered(stream)
                        stdin_open = False

                    continue

                capture = (
                    stdout_data if kind == "stdout" else stderr_data
                )
                limit = (
                    stdout_limit if kind == "stdout" else stderr_limit
                )

                try:
                    # stderr excedente deve continuar sendo drenado,
                    # mas nunca armazenado além do limite.
                    read_size = (
                        4096
                        if kind == "stderr" and len(capture) >= limit
                        else min(4096, limit - len(capture) + 1)
                    )
                    chunk = os.read(stream.fileno(), read_size)
                except OSError:
                    raise ExecutionUncertain(
                        "codex_output_read_error"
                    ) from None

                if not chunk:
                    close_registered(stream)

                    if kind == "stdout":
                        stdout_open = False
                    else:
                        stderr_open = False

                    continue

                if len(capture) + len(chunk) > limit:
                    if kind == "stderr":
                        remaining = limit - len(capture)
                        if remaining > 0:
                            capture.extend(chunk[:remaining])
                        continue

                    raise ExecutionUncertain("codex_output_limit_exceeded")

                capture.extend(chunk)

        # O líder foi observado como terminado, mas ainda não foi
        # recolhido. Tenta encerrar descendentes que permaneçam no grupo.
        cleanup_attempted = True
        if not _stop_process_group(proc):
            raise ExecutionUncertain(
                "codex_process_cleanup_unconfirmed"
            )

        return (
            bytes(stdout_data),
            bytes(stderr_data),
            proc.returncode,
        )

    except BaseException as original:
        if not cleanup_attempted:
            cleanup_attempted = True

            if not _stop_process_group(proc):
                raise ExecutionUncertain(
                    "codex_process_cleanup_unconfirmed"
                ) from original

        raise

    finally:
        if selector is not None:
            selector.close()

        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

def run_task(prompt: str | None = None) -> str:
    if WORKDIR.resolve() != BRIDGE_DIR.resolve():
        raise ExecutionNotStarted("workdir_validation_failed")
    fd, codex_path = _approved_executable_fd(CODEX_BIN, APPROVED_CODEX_BIN)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/ubuntu"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        stdout, _stderr, returncode = _run_bounded_process(
            [
                str(codex_path), "exec", "--ephemeral",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--cd", str(WORKDIR),
                "-",
            ],
            input_text=prompt or FIXED_PROMPT,
            cwd=BRIDGE_DIR,
            env=env,
            timeout_seconds=TASK_TIMEOUT_SECONDS,
            stdout_limit=MAX_STDOUT_BYTES,
            stderr_limit=MAX_STDERR_BYTES,
            executable=f"/proc/self/fd/{fd}",
            pass_fds=(fd,),
        )
    finally:
        os.close(fd)
    if returncode != 0:
        raise ExecutionUncertain("codex_nonzero_exit")
    try:
        decoded = stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        raise ExecutionUncertain("codex_result_invalid") from None
    if prompt is None or prompt == FIXED_PROMPT:
        if b"BRIDGE_SELF_TEST_OK" not in stdout.splitlines():
            raise ExecutionUncertain("codex_result_invalid")
        return "BRIDGE_SELF_TEST_OK"
    return decoded

run_self_test = lambda: run_task(FIXED_PROMPT)


def process_claimed_task(db_path: Path, task_id: str, runner=None) -> str:
    """Executa uma tarefa e trata falhas de persistência com cautela."""
    task = get_task(db_path, task_id)
    prompt = task["prompt"] if task is not None else None
    execute = runner or (lambda: run_task(prompt))

    def persist_and_confirm(status: str, result=None, error=None) -> str:
        try:
            finish(
                db_path,
                task_id,
                status,
                result=result,
                error=error,
            )
            return status

        except Exception:
            # A falha de commit pode deixar o resultado da gravação ambíguo.
            try:
                task = get_task(db_path, task_id)

                if task is None:
                    return "persistence_unconfirmed"

                if (
                    task["status"] == status
                    and task["result"] == result
                    and task["error"] == error
                ):
                    return status

            except Exception:
                pass

            # Não foi possível confirmar a gravação.
            return "persistence_unconfirmed"

    try:
        result = execute()

        if not isinstance(result, str):
            return persist_and_confirm(
                "uncertain",
                error="invalid_execution_result",
            )

    except ExecutionNotStarted as exc:
        return persist_and_confirm("failed", error=exc.code)

    except ExecutionUncertain as exc:
        return persist_and_confirm("uncertain", error=exc.code)

    except Exception:
        # Uma exceção inesperada não prova que a execução não começou.
        return persist_and_confirm(
            "uncertain",
            error="unexpected_execution_error",
        )

    return persist_and_confirm("completed", result=result)

def main():
    parser = argparse.ArgumentParser(description="Worker local da ponte")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Processa no máximo uma tarefa",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Autoriza executar a única ação permitida (self_test)",
    )
    args = parser.parse_args()

    # Validar os argumentos antes de inicializar o banco ou recuperar tarefas.
    if args.execute and not args.once:
        parser.error(
            "A execução real exige --once --execute; "
            "worker contínuo não está habilitado."
        )

    # O lock cobre inicialização, recuperação e execução.
    # Um segundo worker não pode interferir no primeiro.
    try:
        with exclusive_worker_lock(BRIDGE_DIR / "worker.lock"):
            initialize(DB_PATH)

            recovered = recover_interrupted(DB_PATH)
            if recovered:
                print(
                    f"Aviso: {recovered} tarefa(s) interrompida(s) "
                    "marcada(s) uncertain; sem retry automático."
                )

            if not args.execute:
                print("DRY-RUN: nenhuma tarefa será executada.")
                print(
                    "Para inspecionar tarefas pendentes, "
                    "use bridge.py list."
                )
                print(
                    "A execução exige --once --execute explícitos."
                )
                return

            try:
                task_id = claim_next(DB_PATH)
            except RuntimeError as exc:
                # Não imprimir detalhes potencialmente sensíveis do banco.
                print(f"Execução bloqueada: {type(exc).__name__}")
                raise SystemExit(2)

            if task_id is None:
                print("Nenhuma tarefa pendente.")
                return

            status = process_claimed_task(DB_PATH, task_id)
            print(f"Tarefa {task_id}: {status}")

            if status.endswith("_unpersisted") or status == "persistence_unconfirmed":
                print(
                    "Não foi possível confirmar o estado final no banco. "
                    "Não reenfileire nem tente novamente; "
                    "é necessária reconciliação do estado."
                )
                raise SystemExit(2)

            if status == "uncertain":
                print(
                    "Resultado incerto; não há retry automático. "
                    "Revise o estado antes de permitir novas tarefas."
                )
                raise SystemExit(2)

            if status == "failed":
                print(
                    "A execução não foi iniciada; "
                    "a falha foi registrada sem conteúdo bruto."
                )
                raise SystemExit(1)

    except WorkerAlreadyRunning:
        print("Worker bloqueado: já existe outra instância ativa.")
        raise SystemExit(3)


if __name__ == "__main__":
    main()
