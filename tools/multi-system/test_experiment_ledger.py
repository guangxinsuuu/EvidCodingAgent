"""真实 PostgreSQL 的账本边界测试，不将受控适配器当成真实 Lago 业务验收。

运行者通过 EGAP_LEDGER_TEST_DSN_FILE 提供已有测试数据库；本文件只创建随机 schema，
不启动/重启数据库或容器。并发和崩溃测试使用真实 PG 事务、连接与进程退出。
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from experiment_ledger import (ExperimentLedger, IdentityConflict, LedgerError, NotSentError,
                               OperationBusy, OperationUnknown)


class ExperimentLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """仅使用调用方指定的数据库；没有 DSN 时明确跳过，不降级为 mock 通过。"""
        cls.dsn_file = os.environ.get("EGAP_LEDGER_TEST_DSN_FILE")
        if not cls.dsn_file:
            raise unittest.SkipTest("EGAP_LEDGER_TEST_DSN_FILE required for real PostgreSQL tests")
        cls.dsn_file = str(Path(cls.dsn_file).resolve())
        cls.dsn = Path(cls.dsn_file).read_text().strip()
        cls.schema = "test_late_ledger_" + uuid4().hex[:20]
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        cls.ledger = ExperimentLedger(cls.dsn, cls.directory / "evidence", schema=cls.schema)
        with psycopg.connect(cls.dsn) as connection:
            connection.execute(sql.SQL("CREATE TABLE {}.controlled_adapter_effects (operation_id text PRIMARY KEY, value integer NOT NULL)").format(sql.Identifier(cls.schema)))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        cls.temporary.cleanup()

    def setUp(self):
        self.experiment = "experiment-" + uuid4().hex
        self.identity = {"candidate": {"source_sha": "a" * 40, "build_digest": "b" * 64},
                         "configuration": {"model": "local-test", "source_instance": "source-test"},
                         "inputs": {"case": "late_usage", "event_id": "event-" + self.experiment}}
        self.ledger.register_experiment(self.experiment, **self.identity)
        self.request = {"method": "POST", "event_id": "usage-" + self.experiment, "units": "1"}

    def execute(self, callback, *, operation_key="send_usage", effect="WRITE", request=None):
        return self.ledger.execute(self.experiment, operation_key, kind="send_usage", effect=effect,
                                   request=request or self.request, callback=callback)

    def test_experiment_identity_freezes_candidate_configuration_and_inputs(self):
        original = self.ledger.get_experiment(self.experiment)
        self.assertEqual(original, self.ledger.register_experiment(self.experiment, **self.identity))
        for field in self.identity:
            changed = {**self.identity, field: {**self.identity[field], "changed": True}}
            with self.subTest(field=field), self.assertRaises(IdentityConflict):
                self.ledger.register_experiment(self.experiment, **changed)
        self.assertEqual(self.ledger.get_experiment(self.experiment), original)

    def test_reserved_is_not_sent_and_can_be_claimed_once(self):
        row = self.ledger.reserve(self.experiment, "send_usage", kind="send_usage", effect="WRITE", request=self.request)
        self.assertEqual(row["status"], "RESERVED")
        self.assertIs(row["request_sent"], False)
        self.assertEqual(row["dispatch_count"], 0)
        summary = self.ledger.resume_summary(self.experiment)
        self.assertEqual(summary["safe_to_dispatch"], ["send_usage"])
        result = self.execute(lambda dispatch: {"event_id": self.request["event_id"]})
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIsNone(result["request_sent"])
        self.assertEqual(result["dispatch_count"], 1)

    def test_completed_replay_returns_original_receipt_without_callback(self):
        calls = []
        def callback(dispatch):
            calls.append("sent")
            dispatch.record_sent({"status": 202, "body": {"event_id": self.request["event_id"]}})
            return {"event_id": self.request["event_id"], "accepted": True}
        original = self.execute(callback)
        restored = ExperimentLedger(self.dsn, self.directory / "evidence", schema=self.schema)
        replay = restored.execute(self.experiment, "send_usage", kind="send_usage", effect="WRITE",
                                  request=self.request, callback=callback)
        self.assertEqual(calls, ["sent"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(original["result"], replay["result"])
        self.assertEqual(original["evidence_refs"], replay["evidence_refs"])
        self.assertIs(replay["request_sent"], True)
        self.assertEqual(replay["dispatch_count"], 1)
        self.assertEqual([item["state"] for item in self.ledger.history(self.experiment, "send_usage")],
                         ["RESERVED", "DISPATCHED", "DISPATCHED", "COMPLETED"])
        self.assertTrue(all(self.ledger.verify_evidence(ref) for ref in replay["evidence_refs"]))
        self.assertTrue(all(Path(ref["path"]).stat().st_mode & 0o077 == 0 for ref in replay["evidence_refs"]))

    def test_replay_rejects_request_kind_or_effect_conflict(self):
        self.execute(lambda dispatch: {"ok": True})
        variants = [{"request": {**self.request, "units": "2"}}, {"kind": "reevaluate"}, {"effect": "READ"}]
        for change in variants:
            values = {"kind": "send_usage", "effect": "WRITE", "request": self.request, **change}
            with self.subTest(change=change), self.assertRaises(IdentityConflict):
                self.ledger.execute(self.experiment, "send_usage", callback=lambda _: self.fail("must not dispatch"), **values)

    def test_concurrent_callers_have_one_exclusive_dispatch(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def callback(dispatch):
            calls.append("called")
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test timed out")
            return {"ok": True}
        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(self.execute, callback)
            self.assertTrue(entered.wait(10))
            try:
                with self.assertRaises(OperationBusy):
                    self.execute(callback)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10)["status"], "COMPLETED")
        self.assertEqual(calls, ["called"])
        self.assertEqual(self.ledger.get_operation(self.experiment, "send_usage")["dispatch_count"], 1)

    def test_dispatched_does_not_claim_sent_without_http_receipt(self):
        observed = []
        def callback(dispatch):
            row = self.ledger.get_operation(self.experiment, "send_usage")
            observed.append((row["status"], row["request_sent"]))
            dispatch.record_sent({"success": True})
            return {"ok": True}
        with self.assertRaises(OperationUnknown):
            self.execute(callback)
        self.assertEqual(observed, [("DISPATCHED", None)])
        row = self.ledger.get_operation(self.experiment, "send_usage")
        self.assertIsNone(row["request_sent"])
        self.assertEqual(row["status"], "UNKNOWN")

    def test_callback_timeout_is_unknown_and_cannot_redispatch(self):
        calls = []
        def callback(dispatch):
            calls.append(True)
            raise TimeoutError("sensitive transport detail is not emitted")
        with self.assertRaises(OperationUnknown) as first:
            self.execute(callback)
        with self.assertRaises(OperationUnknown):
            self.execute(callback)
        self.assertEqual(calls, [True])
        self.assertNotIn("sensitive", json.dumps(first.exception.operation))
        self.assertEqual(first.exception.operation["unknown_receipt"]["error"]["exception_type"], "TimeoutError")

    def test_proven_pre_send_failure_is_failed_without_sent_claim(self):
        def callback(dispatch):
            raise NotSentError("ADAPTER_LOCAL_PRECONDITION_FAILED")
        with self.assertRaisesRegex(LedgerError, "FAILED_NOT_SENT"):
            self.execute(callback)
        row = self.ledger.get_operation(self.experiment, "send_usage")
        self.assertEqual(row["status"], "FAILED")
        self.assertIs(row["request_sent"], False)
        with self.assertRaisesRegex(LedgerError, "OPERATION_FAILED"):
            self.execute(lambda dispatch: self.fail("failed operations require an explicit new plan"))

    def test_sent_receipt_cannot_be_overwritten_as_not_sent(self):
        def callback(dispatch):
            dispatch.record_sent({"status": 503, "body": {"error": "unavailable"}})
            raise NotSentError("NOT_SENT")
        with self.assertRaises(OperationUnknown):
            self.execute(callback)
        row = self.ledger.get_operation(self.experiment, "send_usage")
        self.assertIs(row["request_sent"], True)
        self.assertEqual(row["unknown_receipt"]["error"]["code"], "CONTRADICTORY_NOT_SENT")

    def test_real_process_crash_preserves_unknown_and_readonly_reconciliation(self):
        """受控副作用是真实 PG 写入；崩溃后禁止重写，再用 SELECT 取得对账证据。"""
        source = '''
import os
from pathlib import Path
import psycopg
from psycopg import sql
from experiment_ledger import ExperimentLedger
dsn = Path(os.environ["EGAP_LEDGER_TEST_DSN_FILE"]).read_text().strip()
schema = os.environ["EGAP_LEDGER_TEST_SCHEMA"]
ledger = ExperimentLedger(dsn, Path(os.environ["EGAP_LEDGER_TEST_EVIDENCE"]), schema=schema)
def callback(dispatch):
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("INSERT INTO {}.controlled_adapter_effects VALUES (%s,1)").format(sql.Identifier(schema)),
                           (dispatch.operation["operation_id"],))
    os._exit(75)
ledger.execute(os.environ["EGAP_LEDGER_TEST_EXPERIMENT"], "crash", kind="controlled_write", effect="WRITE",
               request={"value": 1}, callback=callback)
'''
        environment = {**os.environ, "EGAP_LEDGER_TEST_SCHEMA": self.schema,
                       "EGAP_LEDGER_TEST_DSN_FILE": self.dsn_file,
                       "EGAP_LEDGER_TEST_EVIDENCE": str(self.directory / "evidence"),
                       "EGAP_LEDGER_TEST_EXPERIMENT": self.experiment}
        completed = subprocess.run([sys.executable, "-c", source], env=environment, cwd=Path(__file__).parent,
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 75)
        crashed = self.ledger.get_operation(self.experiment, "crash")
        self.assertEqual(crashed["status"], "DISPATCHED")
        with self.assertRaises(OperationUnknown):
            self.ledger.execute(self.experiment, "crash", kind="controlled_write", effect="WRITE",
                                request={"value": 1}, callback=lambda _: self.fail("must not resend after crash"))
        unknown = self.ledger.get_operation(self.experiment, "crash")
        self.assertEqual(unknown["status"], "UNKNOWN")
        before_history = self.ledger.history(self.experiment, "crash")
        for method in ("POST", "PATCH", "DELETE"):
            with self.subTest(method=method), self.assertRaisesRegex(LedgerError, "READ_ONLY"):
                self.ledger.resolve_unknown(self.experiment, "crash", reconciliation_key="readback",
                                            request={"method": method}, reason="read existing effect",
                                            callback=lambda _: self.fail("write reconciliation prohibited"))
        def observe(dispatch):
            with psycopg.connect(self.dsn, options="-c default_transaction_read_only=on") as connection:
                value = connection.execute(sql.SQL("SELECT value FROM {}.controlled_adapter_effects WHERE operation_id=%s").format(sql.Identifier(self.schema)),
                                           (unknown["operation_id"],)).fetchone()[0]
            dispatch.record_evidence("sql_readback", {"operation_id": unknown["operation_id"], "value": value})
            return {"outcome": "COMPLETED", "result": {"persisted_value": value}}
        resolved = self.ledger.resolve_unknown(self.experiment, "crash", reconciliation_key="readback",
                                              request={"method": "SELECT", "operation_id": unknown["operation_id"]},
                                              reason="read the committed controlled-adapter effect", callback=observe)
        self.assertEqual(resolved["status"], "COMPLETED")
        self.assertEqual(resolved["result"], {"persisted_value": 1})
        self.assertEqual(resolved["unknown_receipt"], unknown["unknown_receipt"])
        self.assertEqual(self.ledger.history(self.experiment, "crash")[:len(before_history)], before_history)
        self.assertEqual(resolved["dispatch_count"], 1)
        self.assertIsNone(resolved["request_sent"])
        self.assertTrue(any(row["kind"] == "reconcile" and row["effect"] == "READ"
                            for row in self.ledger.list_operations(self.experiment)))
        replay = self.ledger.execute(self.experiment, "crash", kind="controlled_write", effect="WRITE",
                                     request={"value": 1}, callback=lambda _: self.fail("must reuse reconciliation"))
        self.assertEqual(replay["result"], {"persisted_value": 1})
        self.assertTrue(replay["replayed"])
        observation_ref = next(ref for ref in resolved["evidence_refs"] if ref["label"] == "sql_readback")
        Path(observation_ref["path"]).write_text("tampered observation")
        with self.assertRaisesRegex(LedgerError, "EVIDENCE_INTEGRITY_FAILED"):
            self.ledger.execute(self.experiment, "crash", kind="controlled_write", effect="WRITE",
                                request={"value": 1}, callback=lambda _: self.fail("tampered reconciliation must not replay"))

    def test_unresolved_observation_preserves_unknown_then_confirmed_absence_closes_failed(self):
        def callback(dispatch):
            raise ConnectionError("unknown transport")
        with self.assertRaises(OperationUnknown):
            self.execute(callback)
        unknown = self.ledger.get_operation(self.experiment, "send_usage")["unknown_receipt"]
        first = self.ledger.resolve_unknown(self.experiment, "send_usage", reconciliation_key="first",
                                           request={"method": "GET", "path": "/events/event"}, reason="inspect receipt",
                                           callback=lambda _: {"outcome": "UNKNOWN", "result": {"visible": False}})
        self.assertEqual(first["status"], "UNKNOWN")
        last = self.ledger.resolve_unknown(self.experiment, "send_usage", reconciliation_key="second",
                                          request={"method": "GET", "path": "/events/event"}, reason="authoritative absence",
                                          callback=lambda _: {"outcome": "NOT_APPLIED", "result": {"absence_confirmed": True}})
        self.assertEqual(last["status"], "FAILED")
        self.assertEqual(last["unknown_receipt"], unknown)
        self.assertEqual(last["dispatch_count"], 1)

    def test_evidence_tampering_blocks_completed_replay(self):
        result = self.execute(lambda _: {"event_id": self.request["event_id"]})
        reference = result["evidence_refs"][0]
        Path(reference["path"]).write_text("tampered")
        self.assertFalse(self.ledger.verify_evidence(reference))
        with self.assertRaisesRegex(LedgerError, "EVIDENCE_INTEGRITY_FAILED"):
            self.execute(lambda _: self.fail("must not dispatch"))

    def test_read_effect_and_resume_statistics_are_persisted(self):
        row = self.execute(lambda dispatch: {"aggregate": 0}, operation_key="observe", effect="READ",
                           request={"method": "GET", "metric": "units"})
        self.assertEqual(row["effect"], "READ")
        summary = self.ledger.resume_summary(self.experiment)
        self.assertEqual(summary["counts"]["COMPLETED"], 1)
        self.assertEqual(summary["reusable"], ["observe"])
        self.assertEqual(summary["dispatch_count"], 1)


if __name__ == "__main__":
    unittest.main()
