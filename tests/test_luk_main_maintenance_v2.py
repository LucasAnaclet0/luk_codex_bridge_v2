"""Offline regression tests for LUK Terminal V2 bounded maintenance tool."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import luk_main_maintenance_v2 as m


class MaintenanceV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.bot = base / "bot"
        self.bot.mkdir()
        self.stage = base / "stage"
        self.bot.joinpath("logs").mkdir()
        self.campaign = base / "campaign"
        self.campaign.mkdir()
        self.campaign_stage = self.campaign / "runtime_staging"
        self.campaign_stage.mkdir()
        self.c1 = patch.object(m, "CAMPAIGN", self.campaign)
        self.c2 = patch.object(m, "CAMPAIGN_STAGE", self.campaign_stage)
        self.c1.start()
        self.c2.start()
        self.addCleanup(self.c1.stop)
        self.addCleanup(self.c2.stop)
        self.p1 = patch.object(m, "BOT", self.bot)
        self.p2 = patch.object(m, "STAGE", self.stage)
        self.p3 = patch.object(m, "LOCK", self.bot / ".demo_bot.lock")
        self.p1.start()
        self.p2.start()
        self.p3.start()
        self.addCleanup(self.p1.stop)
        self.addCleanup(self.p2.stop)
        self.addCleanup(self.p3.stop)

    def test_no_shell_or_arbitrary_paths(self):
        for name in ("shell", "restart", "deploy", "", True):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    m.run(name)
        with self.assertRaises(ValueError):
            m.run("snapshot", {"action": "snapshot", "command": "id"})
        with self.assertRaises(ValueError):
            m.run("dev_validate", {"action": "dev_validate", "file": ".env"})

    def test_positions_bounded_and_no_secrets(self):
        payload = {"positions": {
            "BTC-USDT": {"simbolo": "BTC-USDT", "quantidade": 0.2,
                         "apiKey": "secret-should-not-leak",
                         "preco_entrada": 40000},
        }}
        (self.bot / "posicoes_abertas.json").write_text(json.dumps(payload))
        result = m.run("positions")
        serialized = json.dumps(result)
        self.assertEqual(result["result"]["count"], 1)
        self.assertNotIn("secret-should-not-leak", serialized)

    def test_logs_errors_activity_and_redaction(self):
        (self.bot / "logs" / "bot_20261028.log").write_text(
            "09/11/2026 ERROR | [RECONCILIACAO] HYPE-USDT OrderNotFound\n"
            "09/11/2026 ERROR | Saldo livre abaixo do mínimo\n"
            "09/11/2026 INFO | Scan concluído: 12 OK, 0 falhas\n"
        )
        result = m.run("snapshot")
        self.assertEqual(result["result"]["log"]["reconciliation_error_in_tail"], 1)
        self.assertEqual(result["result"]["log"]["low_balance_in_tail"], 1)
        self.assertGreater(result["result"]["log"]["scan_completed_in_tail"], 0)

    def test_campaign_status_is_observational(self):
        (self.campaign_stage / "main.py").write_text("pass\n")
        (self.campaign_stage / "historico_operacoes.json").write_text("[]")
        (self.campaign_stage / "posicoes_abertas.json").write_text('{"positions": {}}')
        got = m.run("campaign_100_status")
        self.assertEqual(got["result"]["target_closed_trades"], 100)
        self.assertEqual(got["result"]["closed_trades_observed"], 0)
        self.assertEqual(got["result"]["python_file_count"], 1)
        self.assertFalse(got["result"]["ready_to_activate"])
        self.assertFalse(got["go_for_main_mutations"])

    def test_source_read_bounded_and_allowlisted(self):
        (self.campaign_stage / "main.py").write_text("first\nsecond\nthird\n")
        got = m.run("source_read", {"action": "source_read", "file": "main.py",
                                   "source": "campaign_staging", "start_line": 2,
                                   "line_count": 1})
        self.assertEqual(got["result"]["excerpt"], "second")
        for bad in (
            {"action": "source_read", "file": ".env",
             "source": "campaign_staging", "start_line": 1, "line_count": 1},
            {"action": "source_read", "file": "main.py",
             "source": "campaign_staging", "start_line": 1, "line_count": 101},
        ):
            with self.assertRaises(ValueError):
                m.run("source_read", bad)

    def test_bootstrap_does_not_falsely_attest_when_proc_restricted(self):
        from tools import bootstrap_luk_terminal_v2 as bootstrap
        with patch.object(bootstrap.os, "readlink", side_effect=PermissionError):
            with patch.object(bootstrap.Path, "read_text", side_effect=PermissionError):
                bootstrap.check_proc_context()

    def test_bootstrap_rejects_mismatched_namespaces(self):
        from tools import bootstrap_luk_terminal_v2 as bootstrap
        def ns_read(path):
            return "pid:[4026531836]" if path == "/proc/self/ns/pid" else "pid:[123]"
        with patch.object(bootstrap.os, "readlink", side_effect=ns_read):
            with self.assertRaises(RuntimeError):
                bootstrap.check_proc_context()

    def test_stage_replace_validates_syntax_without_touching_main(self):
        code = b"def f():\n    return 1\n"
        main = self.bot / "main.py"
        main.write_bytes(code)
        args = {
            "action": "dev_stage_replace",
            "file": "main.py",
            "expected_sha256": hashlib.sha256(code).hexdigest(),
            "old_text": "return 1",
            "new_text": "return 2",
        }
        outcome = m.run("dev_stage_replace", args)
        self.assertEqual(outcome["status"], "STAGED_DEV_ONLY")
        self.assertEqual(main.read_bytes(), code)
        self.assertIn("return 2", (self.stage / "main.py").read_text())
        self.assertEqual(
            m.run("dev_validate", {"action": "dev_validate", "file": "main.py"})["status"],
            "SYNTAX_OK_ONLY",
        )
        self.assertEqual(
            m.run("dev_discard", {"action": "dev_discard", "file": "main.py"})["status"],
            "DISCARDED_DEV_ONLY",
        )

    def test_stage_rejects_stale_and_invalid_patch(self):
        code = b"def f():\n    return 1\n"
        (self.bot / "main.py").write_bytes(code)
        args = {"action": "dev_stage_replace", "file": "main.py",
                "expected_sha256": "a" * 64, "old_text": "return 1", "new_text": "return 2"}
        with self.assertRaises(ValueError):
            m.run("dev_stage_replace", args)
        args["expected_sha256"] = hashlib.sha256(code).hexdigest()
        args["new_text"] = "return ("
        with self.assertRaises(SyntaxError):
            m.run("dev_stage_replace", args)
        self.assertFalse(self.stage.exists())


if __name__ == "__main__":
    unittest.main()
