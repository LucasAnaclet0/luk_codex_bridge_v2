import multiprocessing
import queue
import tempfile
import unittest
from pathlib import Path

from database import initialize, create_self_test, get_task, claim_next, finish, recover_interrupted
from config import MAX_STDOUT_BYTES, MAX_STDERR_BYTES


def _claim_after_barrier(db_path, barrier, result_queue):
    try:
        barrier.wait(timeout=10)
        result_queue.put(("ok", claim_next(Path(db_path))))
    except Exception as exc:
        result_queue.put(("error", type(exc).__name__))

class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.sqlite3"
        initialize(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_task_lifecycle(self):
        task_id = create_self_test(self.db)
        self.assertEqual(get_task(self.db, task_id)["status"], "pending")
        self.assertEqual(claim_next(self.db), task_id)
        self.assertIsNone(claim_next(self.db))
        finish(self.db, task_id, "completed", result="ok")
        self.assertEqual(get_task(self.db, task_id)["status"], "completed")

    def test_interrupted_task_becomes_uncertain_and_is_not_retried(self):
        task_id = create_self_test(self.db)
        self.assertEqual(claim_next(self.db), task_id)
        self.assertEqual(recover_interrupted(self.db), 1)
        self.assertEqual(get_task(self.db, task_id)["status"], "uncertain")
        self.assertIsNone(claim_next(self.db))

    def test_invalid_final_state_rejected(self):
        task_id = create_self_test(self.db)
        claim_next(self.db)
        with self.assertRaises(ValueError):
            finish(self.db, task_id, "pending")

    def test_terminal_payloads_are_bounded_by_utf8_bytes(self):
        task_id = create_self_test(self.db)
        claim_next(self.db)
        with self.assertRaises(ValueError):
            finish(self.db, task_id, "completed", result="é" * (MAX_STDOUT_BYTES // 2 + 1))
        with self.assertRaises(ValueError):
            finish(self.db, task_id, "failed", error="é" * (MAX_STDERR_BYTES // 2 + 1))
        self.assertEqual(get_task(self.db, task_id)["status"], "running")

    def test_two_processes_cannot_claim_the_same_task(self):
        context = multiprocessing.get_context("spawn")
        task_id = create_self_test(self.db)
        barrier = context.Barrier(3)
        result_queue = context.Queue()
        processes = [
            context.Process(target=_claim_after_barrier,
                            args=(str(self.db), barrier, result_queue))
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        for process in processes:
            process.join(10)
            self.assertFalse(process.is_alive(), "processo de claim excedeu o prazo")
            self.assertEqual(process.exitcode, 0)
        try:
            outcomes = [result_queue.get(timeout=2) for _ in processes]
        except queue.Empty:
            self.fail("processos filhos não retornaram todos os resultados")
        self.assertTrue(all(kind == "ok" for kind, _value in outcomes), outcomes)
        claimed = [value for _kind, value in outcomes if value is not None]
        self.assertEqual(claimed, [task_id])
        self.assertEqual(get_task(self.db, task_id)["status"], "running")

if __name__ == "__main__":
    unittest.main()
