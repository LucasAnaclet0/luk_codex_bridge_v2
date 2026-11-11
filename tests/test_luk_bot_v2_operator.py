"""Offline safety tests: never connect to OKX or manage a real service."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import luk_bot_v2_operator as mod
from tools import bootstrap_luk_bot_v2 as install


class V2OperatorOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bot = root / "bot_v2"
        self.bot.mkdir()
        self.bridge = root / "bridge"
        self.bridge.mkdir()
        self.dev = self.bridge / ".luk-bot-v2-dev-staging"
        self.ops = self.bridge / ".luk-bot-v2-operator"
        self.backups = self.ops / "backups"
        self.file = self.bot / "main.py"
        self.old = b"def f():\n    return 1\n"
        self.new = b"def f():\n    return 2\n"
        self.file.write_bytes(self.old)
        for name, value in {"ROOT": self.bot, "BRIDGE": self.bridge,
                            "DEV": self.dev, "OPS": self.ops,
                            "BACKUP": self.backups,
                            "MARKER": self.ops / "last_deployment.json",
                            "LOCK": self.ops / "operator.lock"}.items():
            p = patch.object(mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(mod.os, "getuid", return_value=1001)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(mod.os, "geteuid", return_value=1001)
        p.start()
        self.addCleanup(p.stop)

    def _patch(self, expected=None):
        return {"action": "dev_stage_patch", "file": "main.py",
                "old_text": "return 1", "new_text": "return 2",
                "expected_sha256": expected or hashlib.sha256(self.old).hexdigest()}

    def test_stage_only_and_syntax(self):
        result = mod.run(self._patch())
        self.assertEqual(result["result"]["status"], "STAGED_ONLY")
        self.assertTrue((self.dev / "main.py").is_file())
        self.assertEqual(self.file.read_bytes(), self.old)
        verified = mod.run({"action": "dev_verify", "file": "main.py"})
        self.assertTrue(verified["result"]["syntax_ok"])
        self.assertFalse(verified["result"]["tests_run"])

    def test_stale_hash_blocks_staging(self):
        with self.assertRaises(RuntimeError):
            mod.run(self._patch("0"*64))
        self.assertEqual(self.file.read_bytes(), self.old)

    def test_read_only_discovery_of_new_v2_python_file(self):
        file = self.bot / "improved_v2_strategy.py"
        file.write_text("def strategy():\n    return 42\n")
        observed = mod.run({"action": "source_read", "file": file.name,
                            "start_line": 1, "line_count": 2})
        self.assertIn("return 42", observed["result"]["excerpt"])
        with self.assertRaises(ValueError):
            mod.run({"action": "dev_stage_patch", "file": file.name,
                     "old_text": "return 42", "new_text": "return 43",
                     "expected_sha256": hashlib.sha256(file.read_bytes()).hexdigest()})

    def test_incremental_staging_is_hash_locked_and_live_unchanged(self):
        first = mod.run(self._patch())["result"]
        second = mod.run({
            "action": "dev_stage_patch", "file": "main.py",
            "expected_sha256": first["staged_sha256"],
            "old_text": "return 2", "new_text": "return 3",
        })["result"]
        self.assertEqual(second["origin"], "STAGED_TO_DEV")
        self.assertEqual(self.file.read_bytes(), self.old)
        self.assertIn(b"return 3", (self.dev / "main.py").read_bytes())
        with self.assertRaisesRegex(RuntimeError, "hash changed"):
            mod.run({
                "action": "dev_stage_patch", "file": "main.py",
                "expected_sha256": first["staged_sha256"],
                "old_text": "return 3", "new_text": "return 4",
            })
        self.assertIn(b"return 3", (self.dev / "main.py").read_bytes())

    def test_demo_promotion_allows_only_verified_menu_after_trading_stopped(self):
        (self.bot / "v2").mkdir()
        demo = self.bot / "v2" / "demo.py"
        demo.write_text("def run():\n    return 1\n")
        current = hashlib.sha256(demo.read_bytes()).hexdigest()
        stage = mod.run({
            "action": "dev_stage_patch", "file": "v2/demo.py",
            "old_text": "return 1", "new_text": "return 2",
            "expected_sha256": current,
        })["result"]
        with (patch.object(mod, "_ensure_trade_stopped") as scoped,
              patch.object(mod, "_ensure_stopped") as strict):
            out = mod.run({
                "action": "promote_dev", "file": "v2/demo.py",
                "expected_sha256": current,
                "staged_sha256": stage["staged_sha256"],
                "confirmation": mod.CONFIRM["promote_dev"],
            })
            self.assertEqual(out["result"]["status"], "V2_SOURCE_PROMOTED_NO_RESTART")
            scoped.assert_called_once()
            strict.assert_not_called()

    def test_private_and_unknown_files_rejected(self):
        for name in (".env", "../bot.py", "secrets.py", "/etc/passwd"):
            with self.assertRaises(ValueError):
                mod.run({"action": "source_read", "file": name,
                         "start_line": 1, "line_count": 1})

    def test_promote_requires_confirmation_and_service_stopped(self):
        staged = mod.run(self._patch())["result"]
        args = {"action": "promote_dev", "file": "main.py",
                "expected_sha256": staged["live_sha256"],
                "staged_sha256": staged["staged_sha256"],
                "confirmation": "yes"}
        with self.assertRaises(PermissionError):
            mod.run(args)
        args["confirmation"] = mod.CONFIRM["promote_dev"]
        with patch.object(mod, "_service_identity", return_value=(
                {"ActiveState": "active", "MainPID": "42"}, [])):
            with self.assertRaisesRegex(RuntimeError, "not stopped"):
                mod.run(args)
        self.assertEqual(self.file.read_bytes(), self.old)

    def test_promote_rollback_with_hashes_and_backup(self):
        staged = mod.run(self._patch())["result"]
        args = {"action": "promote_dev", "file": "main.py",
                "expected_sha256": staged["live_sha256"],
                "staged_sha256": staged["staged_sha256"],
                "confirmation": mod.CONFIRM["promote_dev"]}
        with patch.object(mod, "_ensure_stopped"):
            result = mod.run(args)
            self.assertEqual(result["result"]["status"], "V2_SOURCE_PROMOTED_NO_RESTART")
            self.assertEqual(self.file.read_bytes(), self.new)
            rolled = mod.run({"action": "rollback", "file": "main.py",
                              "confirmation": mod.CONFIRM["rollback"]})
            self.assertEqual(rolled["result"]["status"], "V2_SOURCE_ROLLED_BACK_NO_RESTART")
            self.assertEqual(self.file.read_bytes(), self.old)

    def test_source_read_line_bound(self):
        r = mod.run({"action": "source_read", "file": "main.py", "start_line": 2,
                     "line_count": 1})
        self.assertEqual(r["result"]["excerpt"], "    return 1")
        with self.assertRaises(ValueError):
            mod.run({"action": "source_read", "file": "main.py",
                     "start_line": 1, "line_count": 101})

    def test_sqlite_counts_are_read_only(self):
        db = self.bot / "campaign.sqlite3"
        dbcon = sqlite3.connect(str(db))
        dbcon.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, closed INTEGER)")
        dbcon.execute("INSERT INTO trades(closed) VALUES (1)")
        dbcon.commit()
        dbcon.close()
        records = mod._sqlite_summary()
        self.assertEqual(records[0]["row_counts"]["trades"], 1)
        self.assertEqual(records[0]["status"], "OBSERVED")

    def test_readonly_campaign_status_has_bounded_pending_and_positions(self):
        data = self.bot / "data"
        data.mkdir()
        dbpath = data / "v2_okx_demo.sqlite3"
        con = sqlite3.connect(dbpath)
        con.execute("CREATE TABLE orders(status TEXT,side TEXT,symbol TEXT,created_at TEXT)")
        con.execute("CREATE TABLE positions(symbol TEXT,cost REAL,entry REAL,stop REAL,take REAL)")
        con.execute("CREATE TABLE closed_trades(symbol TEXT)")
        con.execute("INSERT INTO orders VALUES ('PREPARED','buy','INJ/USDT','2026-11-10T15:20:00Z')")
        con.execute("INSERT INTO orders VALUES ('FILLED','buy','SOL/USDT','2026-11-10T14:20:00Z')")
        con.execute("INSERT INTO positions VALUES ('SOL/USDT',25.0,50.0,48.0,54.0)")
        con.execute("INSERT INTO closed_trades VALUES ('BTC/USDT')")
        con.commit()
        con.close()
        rows = mod._sqlite_summary()
        result = next(x for x in rows if x["database"] == "data/v2_okx_demo.sqlite3")
        self.assertEqual(result["campaign_closed_trades"], 1)
        self.assertEqual(result["campaign_target_closed_trades"], 100)
        self.assertEqual(result["unresolved_orders"][0]["symbol"], "INJ/USDT")
        self.assertTrue(result["positions_summary"][0]["stop_take_valid"])
        self.assertEqual(sum(x["count"] for x in result["orders_by_status"]), 2)
        self.assertNotIn("cid", str(result))

    def test_unique_service_validation(self):
        expected = (
            "LoadState=loaded\nActiveState=active\nSubState=running\n"
            "MainPID=444\nUser=ubuntu\n"
            f"WorkingDirectory={self.bot}\n"
            "ExecStart={ path=/home/ubuntu/luk_bot_trader/venv/bin/python ; argv[]=/home/ubuntu/luk_bot_trader/venv/bin/python -m v2.demo --daemon ; ignore_errors=no }\n"
            "FragmentPath=/etc/systemd/system/luk-bot-trader-v2.service\n"
        )
        def call(argv, timeout=8):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, expected, "")
            return subprocess.CompletedProcess(argv, 0,
                "luk-bot-trader-v2.service enabled\n", "")
        with patch.object(mod, "_cmd", side_effect=call):
            found = mod._service()
            self.assertEqual(found["status"], "OBSERVED")
            self.assertEqual(found["Unit"], "luk-bot-trader-v2.service")
            redacted = mod._public_service_status(found)
            self.assertNotIn("ExecStart", redacted)
            self.assertTrue(redacted["exec_start_v2_module_verified"])

    def test_reject_incorrect_service_target(self):
        expected = (
            "LoadState=loaded\nActiveState=active\nMainPID=444\nUser=ubuntu\n"
            "WorkingDirectory=/home/ubuntu/luk_bot_trader\n"
            "ExecStart=/home/ubuntu/luk_bot_trader/main.py\n"
            "FragmentPath=/etc/systemd/system/luk-bot-trader-v2.service\n"
        )
        def call(argv, timeout=8):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, expected, "")
            return subprocess.CompletedProcess(argv, 0, "luk-bot-trader-v2.service enabled\n", "")
        with patch.object(mod, "_cmd", side_effect=call):
            with self.assertRaises(RuntimeError):
                mod._service_identity()

    def test_stop_checks_pid_identity_and_no_unsafe_signals(self):
        service = {"status": "OBSERVED", "Unit": "luk-bot-trader-v2.service",
                   "ActiveState": "active", "MainPID": "777"}
        procs = [{"pid": 777, "start_ticks": 77, "cwd": str(self.bot)}]
        with patch.object(mod, "_service_identity", return_value=(service, procs)):
            with patch.object(mod, "_cmd") as command:
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    mod.run({"action": "service_stop",
                             "confirmation": mod.CONFIRM["service_stop"],
                             "expected_pid": 777, "expected_ticks": 78})
                command.assert_not_called()

    def test_inventory_discovers_v2_package_and_no_readonly_writes(self):
        package = self.bot / "v2"
        package.mkdir()
        (package / "demo.py").write_text("def main():\n    return 1\n")
        result = mod.run({"action": "file_inventory"})["result"]
        self.assertIn("v2/demo.py", [x["file"] for x in result["files"]])
        self.assertFalse(self.ops.exists(), "read-only inventory must not create ops")
        source = mod.run({"action":"source_read", "file":"v2/demo.py",
                          "start_line":1, "line_count":2})["result"]
        self.assertIn("return 1", source["excerpt"])
        self.assertFalse(self.ops.exists(), "read-only source must not create ops")

    def test_exchange_readonly_audit_shape_and_no_log_disclosure(self):
        import json
        service = {"ActiveState": "active", "MainPID": "567"}
        processes = [{"pid": 567, "role": "trade_demo", "start_ticks": 123}]
        result = json.dumps({"status": "READONLY_AUDIT_COMPLETED",
                             "order_inj": "UNVERIFIED",
                             "sol_balance": "QUERY_FAILED",
                             "get_requests_only": True,
                             "api_key": "MUST_NOT_RETURN"})
        with (patch.object(mod, "_service_identity", return_value=(service, processes)),
              patch.object(mod, "_demo_policy_proof", return_value=True),
              patch.object(mod.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 0, result, "SECRET_STDERR")) as runner):
            answer = mod.run({"action": "exchange_audit_readonly"})
            self.assertEqual(answer["result"]["status"], "READONLY_AUDIT_COMPLETED")
            self.assertNotIn("api_key", str(answer))
            self.assertNotIn("SECRET_STDERR", str(answer))
            runner.assert_called_once()
            args, kwargs = runner.call_args
            self.assertEqual(args[0][:3],
                             ["/home/ubuntu/luk_bot_trader/venv/bin/python", "-B", "-c"])
            self.assertEqual(kwargs["cwd"], str(self.bot))
            self.assertEqual(kwargs["timeout"], 45)
            self.assertNotIn("shell", kwargs)

    def test_exchange_readonly_requires_verified_trade_process(self):
        with (patch.object(mod, "_service_identity", return_value=(
                  {"ActiveState": "active", "MainPID": "567"},
                  [{"pid": 568, "role": "telegram_menu"}])),
              patch.object(mod.subprocess, "run") as command):
            response = mod.run({"action": "exchange_audit_readonly"})
            self.assertEqual(response["result"]["status"], "NO_GO")
            command.assert_not_called()

    def test_exchange_get_only_child_static_guards(self):
        import ast
        child = ast.parse(mod._EXCHANGE_READONLY_CHILD)
        all_functions = {n.name for n in ast.walk(child) if isinstance(n, ast.FunctionDef)}
        self.assertIn("get_only", all_functions)
        self.assertIn("main", all_functions)
        self.assertIn("safe_number", all_functions)
        forbidden = {"create_order", "cancel_order", "create_market_order",
                     "transfer", "withdraw", "place"}
        called = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                  for n in ast.walk(child) if isinstance(n, ast.Call)}
        self.assertFalse(forbidden.intersection(called))
        self.assertIn("private_get_trade_order", called)
        self.assertIn("private_get_account_balance", called)
        self.assertIn("urlsplit", called)
        self.assertIn("query_only", mod._EXCHANGE_READONLY_CHILD)
        self.assertIn("NON_ALLOWED_READONLY_REQUEST", mod._EXCHANGE_READONLY_CHILD)

    def test_source_read_redacts_credential_lines(self):
        (self.bot / "core").mkdir()
        (self.bot / "core" / "engine.py").write_text(
            "API_KEY = \"should_not_be_shown\"\ndef f():\n    return 7\n")
        r = mod.run({"action":"source_read", "file":"core/engine.py",
                     "start_line":1, "line_count":3})
        self.assertNotIn("should_not_be_shown", r["result"]["excerpt"])
        self.assertIn("[REDACTED", r["result"]["excerpt"])

    def test_v2_demo_module_exec_attestation(self):
        raw = ("{ path=/home/ubuntu/luk_bot_trader/venv/bin/python ; "
               "argv[]=/home/ubuntu/luk_bot_trader/venv/bin/python "
               "-m v2.demo --daemon ; ignore_errors=no }")
        self.assertTrue(mod._valid_v2_exec(raw))
        self.assertFalse(mod._valid_v2_exec(raw.replace("v2.demo","v2.telegram_menu")))
        self.assertFalse(mod._valid_v2_exec(raw.replace("--daemon","--live")))
        self.assertFalse(mod._valid_v2_exec(raw.replace("luk_bot_trader/venv",
                                                    "evil/venv")))

    def test_service_restart_exact_confirmation_and_process_identity(self):
        svc = {"status":"OBSERVED", "Unit":mod.UNIT, "ActiveState":"active",
               "MainPID":"777"}
        ps = [{"pid":777, "start_ticks":999, "role":"trade_demo"},
              {"pid":778, "start_ticks":1000, "role":"telegram_menu"}]
        args = {"action":"service_restart", "expected_pid":777,
                "expected_ticks":999, "confirmation":"NO"}
        with self.assertRaises(PermissionError):
            mod.run(args)
        args["confirmation"] = mod.CONFIRM["service_restart"]
        with patch.object(mod,"_service_identity",return_value=(svc, ps)), \
             patch.object(mod,"_demo_policy_proof",return_value=False), \
             patch.object(mod,"_cmd") as cmd:
            with self.assertRaisesRegex(RuntimeError,"DEMO proof missing"):
                mod.run(args)
            cmd.assert_not_called()
        with patch.object(mod,"_service_identity",return_value=(svc, ps)), \
             patch.object(mod,"_demo_policy_proof",return_value=True), \
             patch.object(mod,"_cmd",return_value=subprocess.CompletedProcess([],0,"","")) as cmd, \
             patch.object(mod,"_service",return_value={
                 "status":"OBSERVED","ActiveState":"active","MainPID":"888"}):
            answer = mod.run(args)
            self.assertEqual(answer["result"]["status"],
                             "V2_RESTARTED_DEMO_RUNNING_NOT_RECONCILED")
            cmd.assert_called_once_with(["/usr/bin/sudo", "-n",
                                         "/usr/bin/systemctl", "restart", mod.UNIT],
                                        timeout=15)

    def test_canary_patch_of_existing_mcp_registration(self):
        server = (
            "# LUK_CONTROLLED_OPERATOR_V1\n"
            "def _tools():\n"
            "    items = [{'name': 'base_tool'}]\n"
            "    return items\n"
            "def _tool_payload(name, arguments):\n"
            "    return None\n"
            "class Handler: pass\n"
            "def main(): pass\n"
            'if __name__ == "__main__":\n    main()\n'
        )
        updated = install.patch_mcp(server)
        self.assertIn(b"LUK_BOT_TRADER_V2_PLUGIN", updated)
        compile(updated, "mcp_server.py", "exec")
        with self.assertRaises(RuntimeError):
            install.patch_mcp(updated.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
