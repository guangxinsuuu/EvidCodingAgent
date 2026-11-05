"""真实PostgreSQL事务/租约验证；需要专用EGAP_SUPPORT_TEST_DATABASE_URL。

不以SQLite或mock代替事务语义；无真实数据库时明确SKIP，交付报告不得将SKIP计为通过。
每例使用独立测试工单；失败注入只在本测试进程补丁或专用DB中，生产DAO无任意SQL入口。
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from unittest.mock import patch
import unittest
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from genai_support.config import DEFAULT_FIXTURE_PATH
from genai_support.fixtures import load_fixture_bundle
from genai_support.protocol import ProtocolValidationError
from genai_support.store import Store, StoreError


TEST_DSN = os.environ.get("EGAP_SUPPORT_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DSN, "requires dedicated real PostgreSQL test database")
class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = TEST_DSN
        cls.fixtures = load_fixture_bundle(DEFAULT_FIXTURE_PATH)
        cls.store = Store(cls.dsn)
        cls.store.migrate_and_seed(cls.fixtures)

    def setUp(self) -> None:
        self.ticket = "DBTEST-" + uuid4().hex
        with psycopg.connect(self.dsn) as conn:
            conn.execute("INSERT INTO egap_support.support_tickets(tenant_id,ticket_id,current_revision) VALUES('tenant-a',%s,3)", (self.ticket,))

    def create(self):
        return self.store.create_job("tenant-a", self.ticket, 3)

    def draft(self):
        return {"resolution_type": "REFUND_ELIGIBILITY_REVIEW", "ticket_id": self.ticket, "ticket_revision": 3,
                "applicable_policy_ref": "POLICY-OCT-14D", "billing_record_ref": "BILL-a1042",
                "eligibility": "NEEDS_MORE_EVIDENCE", "proposed_action": "REQUEST_INFORMATION",
                "evidence_refs": ["BILL-a1042"], "customer_reply_draft": "合成事务测试：请补充使用和合同资料。"}

    def scalar(self, sql, args=()):
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(sql, args).fetchone()[0]

    def expire(self, job):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE egap_support.support_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=%s", (job,))

    def test_reopen_sees_accepted_job_and_outbox_after_commit(self):
        submitted = self.create()
        reopened = Store(self.dsn)
        job = reopened.get_job("tenant-a", submitted["job_id"])
        self.assertEqual(job["status"], "QUEUED")
        self.assertIsNone(job["draft"])
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_dispatch_outbox WHERE job_id=%s", (job["job_id"],)), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_job_events WHERE job_id=%s AND kind='ACCEPTED'", (job["job_id"],)), 1)

    def test_acceptance_failure_rolls_back_job_and_outbox(self):
        with patch.object(Store, "_event", side_effect=RuntimeError("injected event failure")):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_jobs WHERE ticket_id=%s", (self.ticket,)), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_dispatch_outbox o JOIN egap_support.support_jobs j USING(job_id) WHERE j.ticket_id=%s", (self.ticket,)), 0)

    def test_tenant_and_revision_are_checked_before_acceptance(self):
        job = self.create()["job_id"]
        self.assertIsNone(self.store.get_job("tenant-b", job))
        with self.assertRaisesRegex(StoreError, "TICKET_NOT_FOUND"):
            self.store.create_job("tenant-b", self.ticket, 3)
        with self.assertRaisesRegex(StoreError, "TICKET_REVISION_CONFLICT"):
            self.store.create_job("tenant-a", self.ticket, 2)

    def test_concurrent_notifications_claim_once(self):
        job = self.create()["job_id"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda owner: self.store.claim_job(job, owner), ("worker-1", "worker-2")))
        self.assertEqual(sum(claim is not None for claim in results), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_job_attempts WHERE job_id=%s", (job,)), 1)

    def test_expired_attempt_cannot_overwrite_recovered_attempt(self):
        job = self.create()["job_id"]
        old = self.store.claim_job(job, "worker-old")
        self.expire(job)
        recovered = Store(self.dsn).claim_job(job, "worker-new")
        self.assertEqual(recovered.lease_token, old.lease_token + 1)
        self.assertNotEqual(recovered.attempt_id, old.attempt_id)
        self.assertFalse(self.store.renew_claim(old))
        self.assertFalse(self.store.complete_job(old, self.draft()))
        self.assertTrue(self.store.complete_job(recovered, self.draft()))
        self.assertEqual(self.scalar("SELECT outcome FROM egap_support.support_job_attempts WHERE attempt_id=%s", (old.attempt_id,)), "UNKNOWN")

    def test_draft_and_done_are_atomic_and_duplicate_completion_is_noop(self):
        job = self.create()["job_id"]
        claim = self.store.claim_job(job, "worker-atomic")
        with patch.object(Store, "_event", side_effect=RuntimeError("injected commit-path failure")):
            with self.assertRaises(RuntimeError):
                self.store.complete_job(claim, self.draft())
        actual = self.store.get_job("tenant-a", job)
        self.assertEqual(actual["status"], "PROCESSING")
        self.assertIsNone(actual["draft"])
        self.assertEqual(self.scalar("SELECT outcome FROM egap_support.support_job_attempts WHERE attempt_id=%s", (claim.attempt_id,)), "RUNNING")
        # JSON整数3.0与3语义相同；DB不能用字符串比较错误拒绝共享Schema已接受的值。
        self.assertTrue(self.store.complete_job(claim, {**self.draft(), "ticket_revision": 3.0}))
        self.assertFalse(self.store.complete_job(claim, self.draft()))
        self.assertIsNone(self.store.claim_job(job, "worker-duplicate"))
        self.assertEqual(Store(self.dsn).get_job("tenant-a", job)["draft"], self.draft())
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_job_events WHERE job_id=%s AND kind='DONE'", (job,)), 1)

    def test_changed_ticket_revision_prevents_old_draft_commit(self):
        job = self.create()["job_id"]
        claim = self.store.claim_job(job, "worker-revision")
        with psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE egap_support.support_tickets SET current_revision=4 WHERE tenant_id='tenant-a' AND ticket_id=%s", (self.ticket,))
        self.assertFalse(self.store.complete_job(claim, self.draft()))
        actual = self.store.get_job("tenant-a", job)
        self.assertEqual(actual["status"], "FAILED")
        self.assertIsNone(actual["draft"])
        self.assertEqual(self.scalar("SELECT outcome FROM egap_support.support_job_attempts WHERE attempt_id=%s", (claim.attempt_id,)), "STALE")

    def test_unknown_keeps_processing_and_revokes_old_claim_with_backoff(self):
        job = self.create()["job_id"]
        claim = self.store.claim_job(job, "worker-unknown")
        self.assertTrue(self.store.release_unknown(claim, retry_after_seconds=5))
        self.assertEqual(self.store.get_job_status(job), "PROCESSING")
        self.assertFalse(self.store.renew_claim(claim))
        self.assertFalse(self.store.fail_job(claim))
        self.assertIsNone(self.store.claim_job(job, "worker-too-early"))
        self.expire(job)
        resumed = self.store.claim_job(job, "worker-reconcile")
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.job_id, claim.job_id)

    def test_fail_is_terminal_and_schema_consistent(self):
        job = self.create()["job_id"]
        claim = self.store.claim_job(job, "worker-fail")
        self.assertTrue(self.store.fail_job(claim))
        actual = self.store.get_job("tenant-a", job)
        self.assertEqual((actual["status"], actual["draft"], actual["error_code"]), ("FAILED", None, "PROCESSING_FAILED"))
        self.assertFalse(self.store.fail_job(claim))
        self.assertIsNone(self.store.claim_job(job, "worker-after-failure"))

    def test_bad_draft_and_database_null_check_cannot_create_done(self):
        job = self.create()["job_id"]
        claim = self.store.claim_job(job, "worker-bad-draft")
        with self.assertRaises(ProtocolValidationError):
            self.store.complete_job(claim, {**self.draft(), "ticket_revision": 2})
        for invalid_draft in ({}, {"ticket_id": self.ticket}, {"ticket_id": self.ticket, "ticket_revision": None}):
            with self.assertRaises(psycopg.errors.CheckViolation):
                with psycopg.connect(self.dsn) as conn:
                    conn.execute("UPDATE egap_support.support_jobs SET status='DONE',draft=%s,error_code=NULL WHERE job_id=%s", (Jsonb(invalid_draft), job))
        self.assertEqual(self.store.get_job_status(job), "PROCESSING")

    def test_outbox_acknowledgment_is_repeatable_and_does_not_create_new_job(self):
        job = self.create()["job_id"]
        outbox_id = self.scalar("SELECT outbox_id FROM egap_support.support_dispatch_outbox WHERE job_id=%s", (job,))
        self.store.mark_dispatched(outbox_id)
        self.store.mark_dispatched(outbox_id)
        self.assertIsNotNone(self.scalar("SELECT dispatched_at FROM egap_support.support_dispatch_outbox WHERE outbox_id=%s", (outbox_id,)))
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_jobs WHERE job_id=%s", (job,)), 1)
        self.assertEqual(self.store.get_job_status(job), "QUEUED")

    def test_database_recovers_lost_queue_notification_and_expired_worker(self):
        job = self.create()["job_id"]
        outbox_id = self.scalar("SELECT outbox_id FROM egap_support.support_dispatch_outbox WHERE job_id=%s", (job,))
        self.store.mark_dispatched(outbox_id)
        self.assertIn(job, self.store.reclaimable_jobs(limit=100))
        claim = self.store.claim_job(job, "worker-recovery")
        self.assertTrue(self.store.renew_claim(claim))
        self.assertNotIn(job, self.store.reclaimable_jobs(limit=100))
        self.expire(job)
        self.assertIn(job, self.store.reclaimable_jobs(limit=100))
        self.assertFalse(self.store.renew_claim(claim))

    def test_restart_cannot_replace_fixture_binding(self):
        with self.assertRaisesRegex(StoreError, "FIXTURE_BINDING_MISMATCH"):
            self.store.migrate_and_seed(replace(self.fixtures, content_sha256="f" * 64))
        self.assertEqual(self.scalar("SELECT fixture_sha256 FROM egap_support.support_fixture_binding"), self.fixtures.content_sha256)

    def test_seed_is_repeatable_and_heartbeat_has_explicit_roles(self):
        self.store.migrate_and_seed(self.fixtures)
        self.assertTrue(self.store.health())
        self.store.heartbeat("relay", "relay-test")
        self.store.heartbeat("worker", "worker-test")
        self.assertEqual(self.store.service_health(), {"relay": True, "worker": True})
        with self.assertRaisesRegex(StoreError, "STORE_ARGUMENT_INVALID"):
            self.store.heartbeat("model", "not-a-worker")


if __name__ == "__main__":
    unittest.main()
