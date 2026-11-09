"""产品集成的权限/交付边界单测；真实软件联调另由multi-system/check.py记录。"""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from genai_support.business_tools import digest
from genai_support.integrations.http import IntegrationError
from genai_support.integrations.workflow import DisputeService, customer_messages, external_decision


class ExternalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.config = {"chatwoot": {"base_url": "http://127.0.0.1:58101", "api_token": "test", "account_id": 1},
            "lago": {"base_url": "http://127.0.0.1:58102", "api_key": "test"},
            "keycloak": {"issuer": "http://127.0.0.1:58103/realms/test", "client_id": "app", "client_secret": "test"},
            "bindings": {"customers": [{"tenant_id": "tenant-a", "chatwoot_account_id": 1,
                "chatwoot_contact_id": 10, "lago_customer_id": "a", "allowed_subjects": ["subject-a"]}],
                "cases": [{"tenant_id": "tenant-a", "conversation_id": 20}]}}
        self.store = Mock()
        self.store.operation.return_value = None
        self.service = DisputeService(self.store, self.config)
        self.identity = SimpleNamespace(subject="subject-a", tenant_ids=("tenant-a",))
        self.command = {"tenant_id": "tenant-a", "conversation_id": 20, "invoice_id": "invoice-a", "operation_key": "op-1"}
        self.cw, self.lago = Mock(), Mock()
        self.cw.account_id = 1
        self.cw.get_conversation.return_value = {"meta": {"sender": {"id": 10}}}
        self.messages = [{"id": 3, "private": False, "message_type": 0, "content": "账单争议", "created_at": 1_790_000_000}]
        self.cw.get_messages.return_value = self.messages
        self.clients = patch.object(self.service, "_clients", return_value=(self.cw, self.lago))
        self.clients.start()
        self.addCleanup(self.clients.stop)

    def test_other_tenant_is_rejected_before_any_external_reads(self):
        with self.assertRaisesRegex(IntegrationError, "CUSTOMER_ACCESS_DENIED"):
            self.service.submit(self.identity, {**self.command, "tenant_id": "tenant-b"})
        self.cw.get_conversation.assert_not_called()
        self.lago.resolve_invoice_scope.assert_not_called()

    def test_unregistered_conversation_denied_before_body_read(self):
        with self.assertRaisesRegex(IntegrationError, "CONVERSATION_NOT_AUTHORIZED"):
            self.service.submit(self.identity, {**self.command, "conversation_id": 999})
        self.cw.get_conversation.assert_not_called()

    def test_changed_contact_denied_before_billing_read(self):
        self.cw.get_conversation.return_value = {"meta": {"sender": {"id": 11}}}
        with self.assertRaisesRegex(IntegrationError, "CUSTOMER_ACCESS_DENIED"):
            self.service.submit(self.identity, self.command)
        self.lago.resolve_invoice_scope.assert_not_called()
        self.store.admit.assert_not_called()

    def test_private_note_and_status_never_change_business_input(self):
        expected = digest(customer_messages(self.messages))
        self.assertEqual(expected, digest(customer_messages([*self.messages,
            {"id": 4, "private": True, "message_type": 1, "content": "草稿"},
            {"id": 5, "private": False, "message_type": 2, "content": "status"}])))

    def delivery_row(self):
        return {"tenant_id": "tenant-a", "job_id": "a" * 32, "status": "DONE", "subject": "subject-a",
                "contact_id": 10, "conversation_id": 20, "ticket_revision": 1, "current_revision": 1,
                "invoice_id": "invoice-a", "draft": {"customer_reply_draft": "请补证"},
                "message_sha256": digest(customer_messages(self.messages)), "delivery_state": "PENDING",
                "external_message_id": None, "delivery_error": None}

    def test_customer_update_blocks_old_note_before_post(self):
        row = self.delivery_row()
        self.store.delivery.return_value = row
        self.cw.get_messages.return_value = [*self.messages, {"id": 4, "private": False, "message_type": 0,
            "content": "更换账单", "created_at": 1_790_000_001}]
        self.service.deliver("tenant-a", row["job_id"])
        self.cw.create_private_note.assert_not_called()
        self.store.finish_delivery.assert_called_once_with("tenant-a", row["job_id"], "STALE", error="BUSINESS_INPUT_CHANGED")

    def test_lost_write_receipt_remains_unknown_and_is_not_reposted(self):
        row = self.delivery_row()
        self.store.delivery.return_value = row
        self.store.claim_delivery.return_value = True
        self.cw.create_private_note.side_effect = IntegrationError("EXTERNAL_RECEIPT_UNKNOWN", outcome="UNKNOWN")
        self.service.deliver("tenant-a", row["job_id"])
        self.store.finish_delivery.assert_called_with("tenant-a", row["job_id"], "UNKNOWN", error="EXTERNAL_RECEIPT_UNKNOWN")
        row["delivery_state"] = "UNKNOWN"
        self.service.deliver("tenant-a", row["job_id"])
        self.assertEqual(self.cw.create_private_note.call_count, 1)

    def test_marker_copy_without_exact_content_cannot_confirm_write(self):
        row = self.delivery_row()
        row["delivery_state"] = "UNKNOWN"
        self.store.delivery.return_value = row
        self.cw.get_messages.return_value = [{"id": 9, "private": True, "conversation_id": 20,
            "content": "人工转述 [EGAP draft " + row["job_id"] + "]"}]
        self.service.deliver("tenant-a", row["job_id"])
        self.store.finish_delivery.assert_called_with("tenant-a", row["job_id"], "UNKNOWN", error="WRITEBACK_RECEIPT_UNKNOWN")
        self.cw.create_private_note.assert_not_called()

    def test_remote_zero_without_completeness_cannot_authorize_refund(self):
        context = {"billing": {"billing_record_id": "invoice-a", "status": "PAID"},
                   "subscription": {"subscription_id": "a"}, "usage": {"units": 0},
                   "missing_evidence": ["USAGE_COMPLETENESS", "RENEWAL_EVENT", "APPROVED_POLICY"]}
        result = external_decision(context)
        self.assertEqual(result["eligibility"], "NEEDS_MORE_EVIDENCE")
        self.assertIsNone(result["within_window"])
        self.assertEqual(result["usage_units"], 0)


if __name__ == "__main__":
    unittest.main()
