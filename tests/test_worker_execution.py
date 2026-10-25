import sqlite3
import sys
import tempfile
import textwrap
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import worker
from database import claim_next, create_self_test, get_task, initialize


class WorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fake_executable(self, body: str) -> Path:
        path = self.root / f"fake-codex-{len(list(self.root.iterdir()))}"
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o700)
        return path.resolve()

    def run_fake_codex(self, path: Path, *, stdout_limit=None, stderr_limit=None, timeout=None):
        patches = [
            mock.patch.object(worker, "CODEX_BIN", str(path)),
            mock.patch.object(worker, "APPROVED_CODEX_BIN", str(path)),
        ]
        if stdout_limit is not None:
            patches.append(mock.patch.object(worker, "MAX_STDOUT_BYTES", stdout_limit))
        if stderr_limit is not None:
            patches.append(mock.patch.object(worker, "MAX_STDERR_BYTES", stderr_limit))
        if timeout is not None:
            patches.append(mock.patch.object(worker, "TASK_TIMEOUT_SECONDS", timeout))
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            return worker.run_self_test()

    def test_both_pipes_are_drained_and_only_fixed_result_is_returned(self):
        fake = self.fake_executable("""
            import sys
            prompt = sys.stdin.read()
            if "BRIDGE_SELF_TEST_OK" not in sys.argv and "BRIDGE_SELF_TEST_OK" not in prompt:
                raise SystemExit(9)
            sys.stdout.write("o" * 100000)
            sys.stdout.flush()
            sys.stderr.write("e" * 100000)
            sys.stderr.flush()
            sys.stdout.write("\\nBRIDGE_SELF_TEST_OK\\n")
        """)
        db_path, task_id = self.make_claimed_task()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(worker, "CODEX_BIN", str(fake)))
            stack.enter_context(mock.patch.object(worker, "APPROVED_CODEX_BIN", str(fake)))
            stack.enter_context(mock.patch.object(worker, "MAX_STDOUT_BYTES", 150000))
            stack.enter_context(mock.patch.object(worker, "MAX_STDERR_BYTES", 150000))
            result = worker.process_claimed_task(db_path, task_id)
        self.assertEqual(result, "completed")
        self.assertEqual(get_task(db_path, task_id)["result"], "BRIDGE_SELF_TEST_OK")

    def test_output_limit_terminates_fake_process_without_retaining_full_output(self):
        fake = self.fake_executable("""
            import sys
            while True:
                sys.stdout.write("o" * 8192)
                sys.stdout.flush()
                sys.stderr.write("e" * 8192)
                sys.stderr.flush()
        """)
        with self.assertRaises(worker.ExecutionUncertain) as raised:
            self.run_fake_codex(fake, stdout_limit=1024, stderr_limit=1024, timeout=5)
        self.assertEqual(raised.exception.code, "codex_output_limit_exceeded")

    def test_timeout_is_uncertain(self):
        fake = self.fake_executable("""
            import time
            time.sleep(5)
        """)
        with self.assertRaises(worker.ExecutionUncertain) as raised:
            self.run_fake_codex(fake, timeout=0.15)
        self.assertEqual(raised.exception.code, "codex_timeout")

    def test_invalid_result_is_uncertain(self):
        fake = self.fake_executable("print('NOT_THE_EXPECTED_RESULT')")
        with self.assertRaises(worker.ExecutionUncertain) as raised:
            self.run_fake_codex(fake)
        self.assertEqual(raised.exception.code, "codex_result_invalid")

    def test_nonzero_exit_is_uncertain(self):
        fake = self.fake_executable("raise SystemExit(7)")
        with self.assertRaises(worker.ExecutionUncertain) as raised:
            self.run_fake_codex(fake)
        self.assertEqual(raised.exception.code, "codex_nonzero_exit")

    def test_symlink_and_unapproved_executable_are_rejected(self):
        target = self.fake_executable("print('BRIDGE_SELF_TEST_OK')")
        link = self.root / "codex-link"
        link.symlink_to(target)
        with mock.patch.object(worker, "CODEX_BIN", str(link)), \
                mock.patch.object(worker, "APPROVED_CODEX_BIN", str(target)):
            with self.assertRaises(worker.ExecutionNotStarted) as raised:
                worker.run_self_test()
        self.assertEqual(raised.exception.code, "codex_executable_validation_failed")

        other = self.fake_executable("print('BRIDGE_SELF_TEST_OK')")
        with mock.patch.object(worker, "CODEX_BIN", str(other)), \
                mock.patch.object(worker, "APPROVED_CODEX_BIN", str(target)):
            with self.assertRaises(worker.ExecutionNotStarted):
                worker.run_self_test()

    def test_unexecutable_file_is_rejected(self):
        fake = self.fake_executable("print('BRIDGE_SELF_TEST_OK')")
        fake.chmod(0o600)
        with mock.patch.object(worker, "CODEX_BIN", str(fake)), \
                mock.patch.object(worker, "APPROVED_CODEX_BIN", str(fake)):
            with self.assertRaises(worker.ExecutionNotStarted):
                worker.run_self_test()

    def make_claimed_task(self):
        db_path = self.root / "tasks.sqlite3"
        initialize(db_path)
        task_id = create_self_test(db_path)
        self.assertEqual(claim_next(db_path), task_id)
        return db_path, task_id

    def test_pre_execution_failure_is_failed_but_started_failures_are_uncertain(self):
        db_path, task_id = self.make_claimed_task()
        status = worker.process_claimed_task(
            db_path, task_id,
            runner=lambda: (_ for _ in ()).throw(worker.ExecutionNotStarted("codex_executable_validation_failed")),
        )
        self.assertEqual(status, "failed")
        self.assertEqual(get_task(db_path, task_id)["status"], "failed")

        db_path, task_id = self.make_claimed_task()
        status = worker.process_claimed_task(
            db_path, task_id,
            runner=lambda: (_ for _ in ()).throw(worker.ExecutionUncertain("codex_timeout")),
        )
        self.assertEqual(status, "uncertain")
        self.assertEqual(get_task(db_path, task_id)["status"], "uncertain")
        self.assertIsNone(claim_next(db_path))

    def test_result_persistence_failure_is_reported_without_raw_exception(self):
        db_path, task_id = self.make_claimed_task()
        with mock.patch.object(worker, "finish", side_effect=sqlite3.OperationalError("private db detail")):
            status = worker.process_claimed_task(db_path, task_id, runner=lambda: "BRIDGE_SELF_TEST_OK")
        self.assertEqual(status, "persistence_unconfirmed")
        self.assertEqual(get_task(db_path, task_id)["status"], "running")
        self.assertIsNone(claim_next(db_path))

    def test_only_sanitized_marker_is_persisted(self):
        db_path, task_id = self.make_claimed_task()
        status = worker.process_claimed_task(db_path, task_id, runner=lambda: "BRIDGE_SELF_TEST_OK")
        row = get_task(db_path, task_id)
        self.assertEqual(status, "completed")
        self.assertEqual(row["result"], "BRIDGE_SELF_TEST_OK")
        self.assertNotIn("private", row["result"])


if __name__ == "__main__":
    unittest.main()
