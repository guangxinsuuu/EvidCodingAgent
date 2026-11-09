"""真实PostgreSQL验证外部输入受理与私有备注交付账本。

测试入口 → IntegrationStore.admit → 输入快照/Job/outbox/操作别名事务；
原有Store.complete_job → claim_delivery → finish_delivery验证交付状态转换。
不启动第三方产品，也不把本测试当作跨系统E2E；缺专用DB明确SKIP，不能计为PASS。
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
import os
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from genai_support.business_tools import digest
from genai_support.config import DEFAULT_FIXTURE_PATH
from genai_support.fixtures import load_fixture_bundle
from genai_support.integrations.persistence import ExternalSnapshotTools, IntegrationStore
from genai_support.store import StoreError


TEST_DSN = os.environ.get("EGAP_SUPPORT_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DSN, "requires dedicated real PostgreSQL test database")
class ExternalPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = TEST_DSN
        # 环境不可达与协议失败分开：只有连接依赖缺失可SKIP，迁移错误必须失败。
        try:
            with psycopg.connect(cls.dsn, connect_timeout=3):
                pass
        except psycopg.OperationalError:
            raise unittest.SkipTest("dedicated PostgreSQL dependency is not ready") from None
        cls.store = IntegrationStore(cls.dsn)
        cls.store.migrate_and_seed(load_fixture_bundle(DEFAULT_FIXTURE_PATH))
        cls.store.migrate()

    def setUp(self) -> None:
        # 每例独立租户/会话，允许与原有Store测试并行，不删除他人实验数据。
        self.tenant = "external-test-" + uuid4().hex
        self.conversation = uuid4().int % 1_000_000_000 + 1
        self.ticket = f"cw:71:{self.conversation}"
        self.context = {
            "source_kind": "CHATWOOT_LAGO",
            "ticket": {"ticket_id": "untrusted-placeholder", "tenant_id": "placeholder", "revision": 77},
            "billing": {"billing_record_id": "lago-invoice:invoice-a"},
            "subscription": {"subscription_id": "lago-sub:subscription-a"},
            "usage": {"units": 0},
        }
        self.receipts = [{"service": "lago", "method": "GET", "path": "/api/v1/invoices/invoice-a",
                          "status": 200, "response_sha256": "a" * 64}]
        self.command = {
            "tenant": self.tenant, "key": "operation-original", "request_sha": digest({"invoice": "invoice-a"}),
            "subject": "synthetic-operator", "account": 71, "conversation": self.conversation,
            "contact": 81, "invoice": "invoice-a", "message_sha": digest({"message": 1}),
            "context": self.context, "receipts": self.receipts,
        }

    def admit(self, **changes):
        return self.store.admit(**{**self.command, **changes})

    def rows(self, sql, arguments=()):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return conn.execute(sql, arguments).fetchall()

    def count(self, table: str) -> int:
        # table仅来自下方测试常量；用户输入不参与SQL标识符拼接。
        return self.rows(f"SELECT count(*) AS n FROM egap_support.{table} WHERE tenant_id=%s", (self.tenant,))[0]["n"]

    def complete(self, job: str) -> dict:
        """沿真实Worker完成协议准备可交付Job，不直接SQL伪造DONE。"""
        claim = self.store.claim_job(job, "external-db-test")
        self.assertIsNotNone(claim)
        draft = {"resolution_type": "REFUND_ELIGIBILITY_REVIEW", "ticket_id": claim.ticket_id,
                 "ticket_revision": claim.ticket_revision, "applicable_policy_ref": None,
                 "billing_record_ref": "lago-invoice:invoice-a", "eligibility": "NEEDS_MORE_EVIDENCE",
                 "proposed_action": "REQUEST_INFORMATION", "evidence_refs": ["lago-invoice:invoice-a"],
                 "customer_reply_draft": "真实数据库测试草稿，仍需人工核对资料；未退款。"}
        self.assertTrue(self.store.complete_job(claim, draft))
        return draft

    def concurrent(self, call):
        """同时释放两个真实数据库连接，验证数据库锁而非Python线程锁。"""
        barrier = Barrier(2)

        def invoke(index):
            barrier.wait(timeout=5)
            return call(index)

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(invoke, (0, 1)))

    def test_migration_is_repeatable_and_receipts_are_persistent(self):
        self.store.migrate()
        self.store.observe(self.tenant, self.receipts[0])
        observations = self.rows("SELECT event FROM egap_support.external_api_observations WHERE correlation_id=%s", (self.tenant,))
        self.assertEqual([row["event"] for row in observations], self.receipts)

    def test_acceptance_commits_snapshot_job_outbox_and_operation_together(self):
        original = deepcopy(self.context)
        result = self.admit()
        self.assertFalse(result["reused"])
        reopened = IntegrationStore(self.dsn)
        job = reopened.get_job(self.tenant, result["job_id"])
        self.assertEqual((job["status"], job["ticket_id"], job["ticket_revision"]), ("QUEUED", self.ticket, 1))
        self.assertEqual(self.context, original, "冻结输入不能反向修改调用方快照")
        snapshot = self.rows("SELECT context,source_receipts FROM egap_support.external_inputs WHERE tenant_id=%s", (self.tenant,))[0]
        self.assertEqual(snapshot["context"]["ticket"], {"ticket_id": self.ticket, "tenant_id": self.tenant, "revision": 1})
        self.assertEqual(snapshot["source_receipts"], self.receipts)
        self.assertEqual(self.count("external_operations"), 1)
        self.assertEqual(self.count("external_request_keys"), 1)
        self.assertEqual(self.rows("SELECT count(*) AS n FROM egap_support.support_dispatch_outbox WHERE job_id=%s", (job["job_id"],))[0]["n"], 1)
        self.assertEqual(self.rows("SELECT count(*) AS n FROM egap_support.support_job_events WHERE job_id=%s AND kind='ACCEPTED'", (job["job_id"],))[0]["n"], 1)

    def test_failure_after_outbox_insert_rolls_back_all_acceptance_records(self):
        with patch.object(IntegrationStore, "_event", side_effect=RuntimeError("injected acceptance failure")):
            with self.assertRaisesRegex(RuntimeError, "injected acceptance failure"):
                self.admit()
        for table in ("support_tickets", "external_inputs", "support_jobs", "external_operations", "external_request_keys"):
            with self.subTest(table=table):
                self.assertEqual(self.count(table), 0)
        # outbox以外键关联Job，不能留下没有Job的派发任务。
        orphan_count = self.rows("SELECT count(*) AS n FROM egap_support.support_dispatch_outbox o LEFT JOIN egap_support.support_jobs j USING(job_id) WHERE j.job_id IS NULL")[0]["n"]
        self.assertEqual(orphan_count, 0)

    def test_same_key_concurrent_requests_create_one_job(self):
        results = self.concurrent(lambda _: self.admit())
        self.assertEqual(len({row["job_id"] for row in results}), 1)
        self.assertEqual(sorted(row["reused"] for row in results), [False, True])
        self.assertEqual(self.count("support_jobs"), 1)
        self.assertEqual(self.count("external_inputs"), 1)
        self.assertEqual(self.count("external_operations"), 1)

    def test_same_key_with_different_request_is_rejected_before_revision_changes(self):
        job = self.admit()["job_id"]
        with self.assertRaisesRegex(StoreError, "OPERATION_KEY_CONFLICT"):
            self.admit(request_sha=digest({"invoice": "invoice-b"}), invoice="invoice-b")
        with self.assertRaisesRegex(StoreError, "OPERATION_KEY_CONFLICT"):
            self.store.operation(self.tenant, self.command["key"], "different-request")
        self.assertEqual(self.store.operation(self.tenant, self.command["key"], self.command["request_sha"])["job_id"], job)
        self.assertEqual(self.count("support_jobs"), 1)
        self.assertEqual(self.store.delivery(self.tenant, job)["current_revision"], 1)

    def test_different_keys_same_input_reuse_job_and_alias_cannot_change_request(self):
        original = self.admit()["job_id"]
        alias_sha = digest({"operation": "alias", "invoice": "invoice-a"})
        alias = self.admit(key="operation-alias", request_sha=alias_sha)
        self.assertEqual(alias, {"job_id": original, "reused": True})
        self.assertEqual(self.store.operation(self.tenant, "operation-alias", alias_sha)["job_id"], original)
        self.assertEqual(self.count("support_jobs"), 1)
        self.assertEqual(self.count("external_operations"), 1)
        self.assertEqual(self.count("external_request_keys"), 2)
        with self.assertRaisesRegex(StoreError, "OPERATION_KEY_CONFLICT"):
            self.admit(key="operation-alias", request_sha="changed-alias-request", invoice="invoice-b")
        self.assertEqual(self.count("external_inputs"), 1)

    def test_different_keys_concurrent_same_input_still_create_one_business_job(self):
        results = self.concurrent(lambda index: self.admit(key=f"concurrent-alias-{index}", request_sha=digest({"key": index})))
        self.assertEqual(len({row["job_id"] for row in results}), 1)
        self.assertEqual(sorted(row["reused"] for row in results), [False, True])
        self.assertEqual(self.count("external_operations"), 1)
        self.assertEqual(self.count("external_request_keys"), 2)

    def test_invoice_then_customer_message_changes_increment_revision(self):
        first = self.admit()["job_id"]
        second = self.admit(key="invoice-change", invoice="invoice-b", request_sha="invoice-change")["job_id"]
        third = self.admit(key="message-change", invoice="invoice-b", request_sha="message-change",
                           message_sha=digest({"message": 2}))["job_id"]
        self.assertEqual([self.store.get_job(self.tenant, job)["ticket_revision"] for job in (first, second, third)], [1, 2, 3])
        self.assertEqual(self.store.delivery(self.tenant, first)["current_revision"], 3)
        # 仅下游用量刷新不伪造新的客户操作，既有冻结快照不被悄悄覆盖。
        changed_context = deepcopy(self.context)
        changed_context["usage"]["units"] = 99
        reused = self.admit(key="usage-refresh", invoice="invoice-b", request_sha="usage-refresh",
                            message_sha=digest({"message": 2}), context=changed_context)
        self.assertEqual(reused, {"job_id": third, "reused": True})
        self.assertEqual(self.count("external_inputs"), 3)

    def test_old_completed_job_cannot_claim_delivery_after_input_change(self):
        old = self.admit()["job_id"]
        self.complete(old)
        self.admit(key="changed-invoice", invoice="invoice-b", request_sha="changed-invoice")
        self.assertFalse(self.store.claim_delivery(self.tenant, old))
        self.assertEqual(self.store.delivery(self.tenant, old)["delivery_state"], "PENDING")

    def test_only_one_concurrent_delivery_claim_and_no_unfinished_or_other_tenant_claim(self):
        job = self.admit()["job_id"]
        self.assertFalse(self.store.claim_delivery(self.tenant, job))
        self.complete(job)
        self.assertFalse(self.store.claim_delivery("different-tenant", job))
        claims = self.concurrent(lambda _: self.store.claim_delivery(self.tenant, job))
        self.assertEqual(sorted(claims), [False, True])
        self.assertEqual(self.store.delivery(self.tenant, job)["delivery_state"], "DISPATCHING")
        self.assertFalse(IntegrationStore(self.dsn).claim_delivery(self.tenant, job))

    def test_unknown_is_not_automatically_reclaimed_and_complete_receipt_is_durable(self):
        job = self.admit()["job_id"]
        self.complete(job)
        self.assertTrue(self.store.claim_delivery(self.tenant, job))
        self.store.finish_delivery(self.tenant, job, "UNKNOWN", error="WRITEBACK_RECEIPT_UNKNOWN")
        reopened = IntegrationStore(self.dsn)
        self.assertFalse(reopened.claim_delivery(self.tenant, job))
        self.assertNotIn(job, {row["job_id"] for row in reopened.pending_deliveries()})
        self.assertEqual(reopened.delivery(self.tenant, job)["delivery_state"], "UNKNOWN")
        # 独立对账找到原生消息后可确认COMPLETE；COMPLETE不得被迟到错误覆盖。
        reopened.finish_delivery(self.tenant, job, "COMPLETE", message_id=912345)
        reopened.finish_delivery(self.tenant, job, "UNKNOWN", error="LATE_ERROR")
        row = IntegrationStore(self.dsn).delivery(self.tenant, job)
        self.assertEqual((row["delivery_state"], row["external_message_id"], row["delivery_error"]), ("COMPLETE", 912345, None))
        self.assertFalse(reopened.claim_delivery(self.tenant, job))

    def test_claim_waits_for_ticket_update_and_rechecks_committed_revision(self):
        job = self.admit()["job_id"]
        self.complete(job)
        entered = Event()

        def claim():
            entered.set()
            return self.store.claim_delivery(self.tenant, job)

        # 模拟admit已持工单更新锁。claim必须等事务提交后读新revision；
        # 只在UPDATE...FROM中读取工单快照会错误地绕过此锁取得旧版本资格。
        with ThreadPoolExecutor(max_workers=1) as executor:
            with psycopg.connect(self.dsn) as conn:
                conn.execute("SELECT current_revision FROM egap_support.support_tickets WHERE tenant_id=%s AND ticket_id=%s FOR UPDATE", (self.tenant, self.ticket))
                future = executor.submit(claim)
                self.assertTrue(entered.wait(timeout=5))
                with self.assertRaises(FutureTimeoutError):
                    future.result(timeout=0.2)
                conn.execute("UPDATE egap_support.support_tickets SET current_revision=current_revision+1 WHERE tenant_id=%s AND ticket_id=%s", (self.tenant, self.ticket))
            self.assertFalse(future.result(timeout=5))
        self.assertEqual(self.store.delivery(self.tenant, job)["delivery_state"], "PENDING")

    def test_completion_requires_native_receipt_and_cannot_skip_dispatch(self):
        job = self.admit()["job_id"]
        self.complete(job)
        for invalid in (None, 0, -1, True, "123", 1.5):
            with self.subTest(message_id=invalid):
                with self.assertRaisesRegex(StoreError, "DELIVERY_RECEIPT_REQUIRED"):
                    self.store.finish_delivery(self.tenant, job, "COMPLETE", message_id=invalid)
        self.store.finish_delivery(self.tenant, job, "COMPLETE", message_id=123)
        self.store.finish_delivery(self.tenant, job, "UNKNOWN", error="UNCLAIMED")
        self.assertEqual(self.store.delivery(self.tenant, job)["delivery_state"], "PENDING")
        self.assertTrue(self.store.claim_delivery(self.tenant, job))
        self.store.finish_delivery(self.tenant, job, "COMPLETE", message_id=123)
        self.assertEqual(self.store.delivery(self.tenant, job)["external_message_id"], 123)

    def test_stale_and_failed_terminal_deliveries_cannot_be_revived(self):
        for terminal in ("STALE", "FAILED"):
            with self.subTest(state=terminal):
                job = self.admit(key=terminal, request_sha=terminal, invoice=terminal)["job_id"]
                self.complete(job)
                self.store.finish_delivery(self.tenant, job, terminal, error="EXPECTED_BOUNDARY")
                self.assertFalse(self.store.claim_delivery(self.tenant, job))
                self.store.finish_delivery(self.tenant, job, "COMPLETE", message_id=123)
                self.store.finish_delivery(self.tenant, job, "UNKNOWN", error="LATE_RECONCILIATION")
                row = self.store.delivery(self.tenant, job)
                self.assertEqual((row["delivery_state"], row["external_message_id"], row["delivery_error"]),
                                 (terminal, None, "EXPECTED_BOUNDARY"))

    def test_snapshot_reader_preserves_evidence_and_does_not_fallback(self):
        from genai_support.business_tools import BusinessToolError
        self.admit()
        events = []
        reader = ExternalSnapshotTools(self.dsn, events.append)
        context = reader.collect(self.tenant, self.ticket, 1)
        self.assertEqual(context["billing"], self.context["billing"])
        self.assertEqual(events[0]["source_kind"], "CHATWOOT_LAGO")
        self.assertEqual(events[0]["source_receipt_count"], 1)
        with self.assertRaisesRegex(BusinessToolError, "EXTERNAL_SNAPSHOT_NOT_FOUND"):
            reader.collect("different-tenant", self.ticket, 1)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
