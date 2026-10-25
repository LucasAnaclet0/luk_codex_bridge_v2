import multiprocessing
import queue
import tempfile
import unittest
from pathlib import Path

from database import create_self_test, get_task, initialize, recover_interrupted, claim_next
from worker import WorkerAlreadyRunning, exclusive_worker_lock


def _try_recover_under_lock(lock_path, db_path, result_queue):
    try:
        with exclusive_worker_lock(Path(lock_path)):
            result_queue.put(("acquired", recover_interrupted(Path(db_path))))
    except WorkerAlreadyRunning:
        result_queue.put(("blocked", None))


class WorkerLockTests(unittest.TestCase):
    def test_second_worker_cannot_acquire_existing_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "worker.lock"
            with exclusive_worker_lock(lock_path):
                with self.assertRaisesRegex(RuntimeError, "outro worker"):
                    with exclusive_worker_lock(lock_path):
                        self.fail("o segundo worker não deveria adquirir o lock")

    def test_lock_is_released_after_context(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "worker.lock"
            with exclusive_worker_lock(lock_path):
                pass
            with exclusive_worker_lock(lock_path):
                pass

    def test_separate_process_cannot_recover_active_task(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "queue.sqlite3"
            lock_path = root / "worker.lock"
            initialize(db_path)
            task_id = create_self_test(db_path)
            self.assertEqual(claim_next(db_path), task_id)
            result_queue = context.Queue()

            with exclusive_worker_lock(lock_path):
                process = context.Process(
                    target=_try_recover_under_lock,
                    args=(str(lock_path), str(db_path), result_queue),
                )
                process.start()
                process.join(10)
                self.assertFalse(process.is_alive(), "processo de teste excedeu o prazo")
                self.assertEqual(process.exitcode, 0)
                try:
                    outcome = result_queue.get(timeout=2)
                except queue.Empty:
                    self.fail("processo filho não retornou resultado")
                self.assertEqual(outcome, ("blocked", None))
                self.assertEqual(get_task(db_path, task_id)["status"], "running")

            self.assertEqual(recover_interrupted(db_path), 1)
            self.assertEqual(get_task(db_path, task_id)["status"], "uncertain")

if __name__ == "__main__":
    unittest.main()
