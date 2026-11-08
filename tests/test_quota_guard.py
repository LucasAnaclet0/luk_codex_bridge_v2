"""Offline tests only; synthetic data, no live Codex/Bridge invocation."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from quota_guard import QuotaDiagnosis, QuotaGuard, classify_codex_failure

TASK = "1234567890abcdef1234567890abcdef"
MESSAGE = "ERROR: You’ve hit your usage limit. Upgrade to Pro or try again at Nov 11th, 2026 7:17 AM."


class ClassifierTests(unittest.TestCase):
    def test_detect_real_message(self):
        d = classify_codex_failure(1, MESSAGE.encode())
        self.assertEqual(d.reason, "codex_usage_limit")
        self.assertEqual(d.retry_hint_unverified, "Nov 11th, 2026 7:17 AM")

    def test_ascii_apostrophe(self):
        self.assertTrue(classify_codex_failure(1, "ERROR: You've hit your usage limit.").detected)

    def test_unrelated_failure(self):
        self.assertFalse(classify_codex_failure(1, "ERROR: invalid credentials").detected)

    def test_no_false_positive_plain_prompt(self):
        self.assertFalse(classify_codex_failure(1, "User said: You've hit your usage limit").detected)

    def test_success_ignores_false_quota_error(self):
        self.assertFalse(classify_codex_failure(0, MESSAGE).detected)

    def test_no_time_hint(self):
        self.assertIsNone(classify_codex_failure(1, "ERROR: usage limit exceeded").retry_hint_unverified)

    def test_wrong_types(self):
        with self.assertRaises(TypeError):
            classify_codex_failure(True, MESSAGE)
        with self.assertRaises(TypeError):
            classify_codex_failure(1, 13)


class PersistentGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.gate = QuotaGuard(self.path)

    def test_uninitialized_fails_closed(self):
        self.assertFalse(self.gate.may_dispatch())

    def test_initialized_allows_dispatch(self):
        self.gate.initialize()
        self.assertTrue(self.gate.may_dispatch())

    def test_persistent_pause(self):
        self.gate.initialize()
        self.gate.record_usage_limit(TASK, classify_codex_failure(1, MESSAGE))
        self.assertFalse(QuotaGuard(self.path).may_dispatch())
        self.assertEqual(QuotaGuard(self.path).status()["reason"], "codex_usage_limit")

    def test_duplicate_idempotent(self):
        self.gate.initialize()
        d = classify_codex_failure(1, MESSAGE)
        self.gate.record_usage_limit(TASK, d)
        self.gate.record_usage_limit(TASK, d)
        self.assertFalse(self.gate.may_dispatch())

    def test_invalid_id_rejected(self):
        self.gate.initialize()
        with self.assertRaises(ValueError):
            self.gate.record_usage_limit("bad", classify_codex_failure(1, MESSAGE))

    def test_unvalidated_diagnosis_rejected(self):
        self.gate.initialize()
        with self.assertRaises(ValueError):
            self.gate.record_usage_limit(TASK, QuotaDiagnosis(False))

    def test_concurrent_incidents(self):
        self.gate.initialize()
        diag = classify_codex_failure(1, MESSAGE)
        ids = [f"{n:032x}" for n in range(1, 17)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda task_id: self.gate.record_usage_limit(task_id, diag), ids))
        self.assertFalse(self.gate.may_dispatch())

    def test_corrupt_db_denies(self):
        self.path.write_bytes(b"not-sqlite")
        self.assertFalse(self.gate.may_dispatch())

    def test_no_auto_resume_due_to_hint(self):
        self.gate.initialize()
        self.gate.record_usage_limit(TASK, classify_codex_failure(1, MESSAGE))
        self.assertFalse(self.gate.may_dispatch())
        # No resume or retry API is intentionally provided.
        self.assertFalse(hasattr(self.gate, "resume"))


if __name__ == "__main__":
    unittest.main()
