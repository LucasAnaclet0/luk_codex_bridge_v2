"""Linux-only integration tests. Never launch Codex or open a live database."""
import concurrent.futures
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import config
import database as db
import mcp_server as mcp
import worker as w


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "test.sqlite3"
        db.initialize(self.db)
        self.grace = patch.object(w, "PROCESS_TERMINATE_GRACE_SECONDS", 0.02)
        self.grace.start()
        self.addCleanup(self.grace.stop)

    def run_child(self, code, **limits):
        return w._run_bounded_process(
            [sys.executable, "-I", "-c", code], input_text="",
            cwd=self.root, env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=limits.get("timeout", 4),
            stdout_limit=limits.get("stdout", config.MAX_STDOUT_BYTES),
            stderr_limit=limits.get("stderr", config.MAX_STDERR_BYTES),
        )

    def task(self):
        task = db.create_task(self.db, "fictional test")
        self.assertEqual(db.claim_next(self.db), task)
        return task

    def assert_uncertain(self, code, fn):
        with self.assertRaises(w.ExecutionUncertain) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_reproduces_old_12000_byte_failure(self):
        self.assert_uncertain("codex_output_limit_exceeded", lambda:
            self.run_child("import os; os.write(1,b'x'*12001)", stdout=12000))

    def test_large_result_persisted_and_next_task_claimable(self):
        first = self.task()
        output = ("á🚀 audit\n" * 30000).encode()
        def runner():
            stdout, stderr, rc = self.run_child(
                "import os; data=('á🚀 audit\\n'*30000).encode(); "
                "f=os.fdopen(1,'wb'); f.write(data); f.flush()")
            self.assertEqual((stderr, rc), (b"", 0))
            self.assertEqual(stdout, output)
            return stdout.decode()
        self.assertEqual(w.process_claimed_task(self.db, first, runner), "completed")
        second = db.create_task(self.db, "second fictional task")
        self.assertEqual(db.claim_next(self.db), second)
        self.assertEqual(db.get_task(self.db, first)["result"].encode(), output)

    def test_exact_stdout_ceiling(self):
        out, _, rc = self.run_child(
            f"import sys; sys.stdout.buffer.write(b'x'*{config.MAX_STDOUT_BYTES})")
        self.assertEqual(len(out), config.MAX_STDOUT_BYTES)
        self.assertEqual(rc, 0)

    def test_hard_stdout_ceiling_is_explicit(self):
        self.assert_uncertain("codex_output_limit_exceeded", lambda:
            self.run_child(f"import sys; sys.stdout.buffer.write(b'x'*{config.MAX_STDOUT_BYTES+1})"))

    def test_stderr_above_old_limit_is_complete(self):
        out, err, rc = self.run_child("import os; os.write(2,b'e'*20000); os.write(1,b'ok')")
        self.assertEqual((out, len(err), rc), (b"ok", 20000, 0))

    def test_exact_stderr_ceiling(self):
        _, err, rc = self.run_child(
            f"import sys; sys.stderr.buffer.write(b'e'*{config.MAX_STDERR_BYTES})")
        self.assertEqual((len(err), rc), (config.MAX_STDERR_BYTES, 0))

    def test_stderr_never_silently_truncated(self):
        self.assert_uncertain("codex_stderr_limit_exceeded", lambda:
            self.run_child(f"import sys; sys.stderr.buffer.write(b'e'*{config.MAX_STDERR_BYTES+1})"))

    def test_both_pipes_do_not_deadlock(self):
        out, err, rc = self.run_child(
            "import os\nfor _ in range(100):\n os.write(1,b'o'*2000)\n os.write(2,b'e'*2000)")
        self.assertEqual((len(out), len(err), rc), (200000, 200000, 0))

    def test_timeout_remains_uncertain_and_blocks_queue(self):
        task = self.task()
        def runner():
            self.run_child("import time; time.sleep(30)", timeout=0.1)
        self.assertEqual(w.process_claimed_task(self.db, task, runner), "uncertain")
        self.assertEqual(db.get_task(self.db, task)["error"], "codex_timeout")
        db.create_task(self.db, "must not execute")
        with self.assertRaises(RuntimeError):
            db.claim_next(self.db)

    def test_descendant_holding_pipe_times_out_and_is_killed(self):
        started = time.monotonic()
        self.assert_uncertain("codex_timeout", lambda: self.run_child(
            "import os,time\nif os.fork()==0:\n time.sleep(30)\nelse:\n os._exit(0)", timeout=0.15))
        self.assertLess(time.monotonic() - started, 2)

    def test_nonzero_exit_observed(self):
        self.assertEqual(self.run_child("import sys; sys.exit(7)")[2], 7)

    def test_run_task_nonzero_is_uncertain(self):
        fd = os.open(__file__, os.O_RDONLY)
        with patch.object(w, "_approved_executable_fd", return_value=(fd, Path(sys.executable))), \
             patch.object(w, "_run_bounded_process", return_value=(b"", b"", 7)):
            self.assert_uncertain("codex_nonzero_exit", lambda: w.run_task("fictional"))

    def test_invalid_utf8_is_uncertain(self):
        fd = os.open(__file__, os.O_RDONLY)
        with patch.object(w, "_approved_executable_fd", return_value=(fd, Path(sys.executable))), \
             patch.object(w, "_run_bounded_process", return_value=(b"\xff", b"", 0)):
            self.assert_uncertain("codex_result_invalid", lambda: w.run_task("fictional"))

    def test_start_failure_is_safe_failed(self):
        task = self.task()
        def runner():
            raise w.ExecutionNotStarted("codex_process_start_failed")
        self.assertEqual(w.process_claimed_task(self.db, task, runner), "failed")
        self.assertIsNone(db.claim_next(self.db))

    def test_cleanup_failure_is_uncertain(self):
        original = w._stop_process_group
        def cleanup(proc):
            original(proc)
            return False
        with patch.object(w, "_stop_process_group", side_effect=cleanup):
            self.assert_uncertain("codex_process_cleanup_unconfirmed", lambda: self.run_child("pass"))

    def test_concurrent_claim_same_task_once(self):
        task = db.create_task(self.db, "fictional concurrent task")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: db.claim_next(self.db), range(8)))
        self.assertEqual(results.count(task), 1)
        self.assertEqual(results.count(None), 7)

    def test_worker_exclusive_lock(self):
        lock = self.root / "worker.lock"
        with w.exclusive_worker_lock(lock):
            with self.assertRaises(w.WorkerAlreadyRunning):
                with w.exclusive_worker_lock(lock):
                    self.fail("second worker entered")

    def test_recovery_never_requeues(self):
        task = self.task()
        self.assertEqual(db.recover_interrupted(self.db), 1)
        self.assertEqual(db.recover_interrupted(self.db), 0)
        self.assertEqual(db.get_task(self.db, task)["status"], "uncertain")
        with self.assertRaises(RuntimeError):
            db.claim_next(self.db)

    def test_commit_error_after_success_is_confirmed(self):
        task = self.task()
        original = db.finish
        def committed(*a, **kw):
            original(*a, **kw)
            raise OSError("simulated lost acknowledgement")
        with patch.object(w, "finish", side_effect=committed):
            self.assertEqual(w.process_claimed_task(self.db, task, lambda: "ok"), "completed")

    def test_persistence_failure_remains_running_then_recoverable(self):
        task = self.task()
        with patch.object(w, "finish", side_effect=OSError("simulated disk failure")):
            self.assertEqual(w.process_claimed_task(self.db, task, lambda: "ok"), "persistence_unconfirmed")
        self.assertEqual(db.get_task(self.db, task)["status"], "running")
        self.assertEqual(db.recover_interrupted(self.db), 1)

    def test_database_rejects_oversize_and_duplicate_finish(self):
        task = self.task()
        with self.assertRaises(ValueError):
            db.finish(self.db, task, "completed", result="x"*(config.MAX_STDOUT_BYTES+1))
        db.finish(self.db, task, "completed", result="ok")
        with self.assertRaises(RuntimeError):
            db.finish(self.db, task, "completed", result="overwrite")

    def test_mcp_pages_reassemble_unicode_result(self):
        task = self.task()
        result = "á🚀\"\\\n" * 5000
        db.finish(self.db, task, "completed", result=result)
        pages = []
        offset = 0
        with patch.object(mcp, "DB_PATH", self.db):
            while True:
                args = {"task_id": task, "result_offset": offset}
                response = mcp._tool_payload("get_task", args)
                page = json.loads(response["content"][0]["text"])
                pages.append(page["result"])
                self.assertLessEqual(len(page["result"]), config.RESULT_PAGE_CHARS)
                self.assertEqual(page["result_total_bytes"], len(result.encode()))
                self.assertEqual(page["result_sha256"], hashlib.sha256(result.encode()).hexdigest())
                if page["result_complete"]:
                    self.assertIsNone(page["next_result_offset"])
                    break
                offset = page["next_result_offset"]
        self.assertEqual("".join(pages), result)

    def test_mcp_short_result_backwards_compatible_and_offsets_validated(self):
        task = self.task()
        db.finish(self.db, task, "completed", result="ok")
        with patch.object(mcp, "DB_PATH", self.db):
            page = json.loads(mcp._tool_payload("get_task", {"task_id": task})["content"][0]["text"])
            self.assertEqual(page["result"], "ok")
            for invalid in (-1, True, "1", 3):
                with self.assertRaises(ValueError):
                    mcp._tool_payload("get_task", {"task_id": task, "result_offset": invalid})


if __name__ == "__main__":
    unittest.main()
