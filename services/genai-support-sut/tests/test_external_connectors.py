"""外部连接器边界测试；这里的 stub 仅验证产品分支，不声称真实系统已联调。

实际服务、原生对象与端到端写回由独立 live 工具运行；依赖缺失不能用本文件替代。
"""

from copy import deepcopy
import io
import json
import time
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from genai_support.integrations.bindings import BindingRegistry
from genai_support.integrations.chatwoot import ChatwootClient, customer_messages
from genai_support.integrations.http import IntegrationError, JsonClient
from genai_support.integrations.identity import KeycloakIdentity
from genai_support.integrations.lago import LagoClient


class HttpTests(unittest.TestCase):
    def test_write_timeout_is_unknown_and_never_retried(self) -> None:
        events = []
        client = JsonClient("http://127.0.0.1:1234", {"api_access_token": "private-token"}, "chatwoot", events.append)
        client._opener = Mock()
        client._opener.open.side_effect = URLError("private-token")
        with self.assertRaises(IntegrationError) as caught:
            client.request("POST", "/messages", json_body={"content": "private-message"}, write=True)
        self.assertEqual(caught.exception.outcome, "UNKNOWN")
        self.assertEqual(client._opener.open.call_count, 1)
        self.assertNotIn("private-token", repr(events) + str(caught.exception))
        self.assertNotIn("private-message", repr(events))

    def test_remote_plain_http_and_credential_urls_are_rejected(self) -> None:
        for url in ("http://billing.example", "https://user:secret@billing.example", "http://127.0.0.1:10/api",
                    "http://127.0.0.1:10?key=secret"):
            with self.subTest(url=url), self.assertRaises(IntegrationError):
                JsonClient(url, {}, "lago")

    def test_write_http_rejection_and_unknown_are_distinct(self) -> None:
        client = JsonClient("http://127.0.0.1:1234", {}, "chatwoot")
        for status, outcome in ((422, "REJECTED"), (500, "UNKNOWN"), (302, "REJECTED")):
            client._opener = Mock()
            client._opener.open.side_effect = HTTPError("secret-url", status, "secret-error", {}, io.BytesIO(b"secret"))
            with self.subTest(status=status), self.assertRaises(IntegrationError) as caught:
                client.request("POST", "/messages", json_body={}, write=True)
            self.assertEqual(caught.exception.outcome, outcome)
            self.assertNotIn("secret", str(caught.exception))

    def test_audit_never_contains_introspection_form_or_token(self) -> None:
        events = []
        client = JsonClient("http://127.0.0.1:1234", {}, "keycloak", events.append)
        response = Mock(status=200)
        response.read.return_value = b'{"active":true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        client._opener = Mock()
        client._opener.open.return_value = response
        client.request("POST", "/introspect", form={"token": "secret-token", "client_secret": "secret-client"})
        self.assertNotIn("secret", json.dumps(events))
        self.assertEqual(events[0]["outcome"], "OBSERVED")


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = KeycloakIdentity("http://127.0.0.1:1234/realms/egap", "egap", "secret-client", "egap-api",
                                       {"subject-a": ["tenant-a"]})
        self.claims = {"active": True, "iss": self.client.issuer, "aud": ["egap-api"],
                       "exp": int(time.time()) + 60, "sub": "subject-a", "tenant_id": "tenant-b"}
        self.client.http.request = Mock(return_value=self.claims)

    def test_tenant_authority_comes_from_server_not_token(self) -> None:
        identity = self.client.authenticate("actual-token")
        self.assertEqual(identity.tenant_ids, ("tenant-a",))
        self.assertEqual(identity.subject, "subject-a")

    def test_inactive_expired_wrong_issuer_audience_and_unknown_subject_rejected(self) -> None:
        for change in ({"active": False}, {"active": "true"}, {"exp": 1}, {"exp": True},
                       {"iss": "https://other.example"}, {"aud": "other"}, {"sub": "unmapped"},
                       {"nbf": time.time() + 60}, {"exp": float("nan")}, {"nbf": float("nan")}):
            self.client.http.request.return_value = {**self.claims, **change}
            with self.subTest(change=change), self.assertRaises(IntegrationError):
                self.client.authenticate("actual-token")

    def test_fresh_token_does_not_bypass_unavailable_identity_service(self) -> None:
        self.client.authenticate("actual-token")
        self.client.http.request.side_effect = IntegrationError("EXTERNAL_UNAVAILABLE")
        with self.assertRaises(IntegrationError) as caught:
            self.client.authenticate("actual-token")
        self.assertEqual(caught.exception.code, "IDENTITY_UNAVAILABLE")
        self.assertEqual(self.client.http.request.call_count, 2)


class BindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {"tenant_id": "tenant-a", "chatwoot_account_id": 1, "chatwoot_contact_id": 2,
                    "lago_customer_id": "billing-a", "allowed_subjects": ["subject-a"]}

    def test_account_and_contact_and_subject_are_all_scoped(self) -> None:
        registry = BindingRegistry({"schema_version": 1, "customers": [self.row]})
        registry.authorize_account("tenant-a", 1, "subject-a")
        self.assertEqual(registry.resolve("tenant-a", 1, 2, "subject-a")["lago_customer_id"], "billing-a")
        for args in (("tenant-b", 1, 2, "subject-a"), ("tenant-a", 2, 2, "subject-a"),
                     ("tenant-a", 1, 3, "subject-a"), ("tenant-a", 1, 2, "subject-b")):
            with self.subTest(args=args), self.assertRaises(IntegrationError):
                registry.resolve(*args)

    def test_ambiguous_contact_mapping_rejected_at_startup(self) -> None:
        with self.assertRaises(IntegrationError):
            BindingRegistry({"schema_version": 1, "customers": [self.row, {**self.row, "tenant_id": "tenant-b"}]})


class LagoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = LagoClient("http://127.0.0.1:1234", "test-secret")
        self.invoice = {"lago_id": "invoice-a", "customer": {"external_id": "customer-a"},
                        "subscriptions": [{"external_id": "sub-a", "external_customer_id": "customer-a"}],
                        "fees": [{"external_subscription_id": "sub-a", "item": {"type": "charge"},
                                  "from_date": "2026-10-01T00:00:00Z", "to_date": "2026-10-31T23:59:59Z"}]}
        self.period = {"lago_invoice_id": "invoice-a", "from_datetime": "2026-10-01T00:00:00Z",
                       "to_datetime": "2026-10-31T23:59:59Z", "charges_usage": [{"units": "0"}]}
        self.client.get_customer = Mock(return_value={"external_id": "customer-a"})
        self.client.get_current_usage = Mock(return_value={**self.period, "lago_invoice_id": "other"})

    def test_foreign_invoice_rejected_before_detail_read(self) -> None:
        self.client.get_customer_invoices = Mock(return_value=[{"lago_id": "invoice-a"}])
        self.client.http.request = Mock()
        with self.assertRaises(IntegrationError) as caught:
            self.client.get_invoice_for_customer("customer-a", "invoice-b")
        self.assertEqual(caught.exception.code, "INVOICE_NOT_AUTHORIZED")
        self.client.http.request.assert_not_called()

    def test_detail_rechecks_owner_despite_scoped_listing(self) -> None:
        self.client.get_customer_invoices = Mock(return_value=[{"lago_id": "invoice-a"}])
        self.client.http.request = Mock(return_value={"invoice": {**self.invoice, "customer": {"external_id": "customer-b"}}})
        with self.assertRaises(IntegrationError) as caught:
            self.client.get_invoice_for_customer("customer-a", "invoice-a")
        self.assertEqual(caught.exception.code, "LAGO_INVOICE_CUSTOMER_MISMATCH")

    def test_invoice_subscription_and_period_determine_usage(self) -> None:
        self.client.get_invoice_for_customer = Mock(return_value=self.invoice)
        self.client.get_past_usage = Mock(return_value=[{**self.period, "lago_invoice_id": "other"}, self.period])
        self.client.get_current_usage = Mock()
        result = self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.client.get_past_usage.assert_called_once_with("customer-a", "sub-a")
        self.client.get_current_usage.assert_not_called()
        self.assertEqual(result["usage_source"], "PAST")
        self.assertEqual(result["usage"]["lago_invoice_id"], "invoice-a")

    def test_missing_history_does_not_substitute_current_zero(self) -> None:
        self.client.get_invoice_for_customer = Mock(return_value=self.invoice)
        self.client.get_past_usage = Mock(return_value=[])
        self.client.get_current_usage = Mock(return_value={**self.period, "lago_invoice_id": "current-invoice"})
        result = self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.assertIsNone(result["usage"])
        self.assertEqual(result["missing_evidence"], ["INVOICE_USAGE_PERIOD_NOT_FOUND"])

    def test_native_current_without_invoice_id_requires_exact_subscription_period_binding(self) -> None:
        self.client.get_invoice_for_customer = Mock(return_value=self.invoice)
        self.client.get_past_usage = Mock(return_value=[])
        native = {**self.period, "lago_invoice_id": None}
        self.client.get_current_usage = Mock(return_value=native)
        result = self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.assertEqual(result["usage_binding"], "SUBSCRIPTION_CURRENT_PERIOD")
        self.assertIsNone(result["usage"]["lago_invoice_id"], "不能伪造原生发票ID")
        self.client.get_current_usage.assert_called_once_with("customer-a", "sub-a")
        self.client.get_current_usage.return_value = {**native, "from_datetime": "2026-10-01T00:00:01Z"}
        self.assertIsNone(self.client.resolve_invoice_scope("customer-a", "invoice-a")["usage"])

    def test_wrong_past_period_may_only_use_current_when_exact_fee_period_matches(self) -> None:
        self.client.get_invoice_for_customer = Mock(return_value=self.invoice)
        self.client.get_past_usage = Mock(return_value=[{**self.period, "to_datetime": "2026-10-01T00:00:00Z"}])
        self.client.get_current_usage = Mock(return_value={**self.period, "lago_invoice_id": None})
        result = self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.assertEqual((result["usage_source"], result["usage_binding"]), ("CURRENT", "SUBSCRIPTION_CURRENT_PERIOD"))

    def test_invoice_identity_alone_cannot_hide_wrong_usage_period(self) -> None:
        self.client.get_invoice_for_customer = Mock(return_value=self.invoice)
        self.client.get_past_usage = Mock(return_value=[{**self.period, "from_datetime": "2026-10-02T00:00:00Z"}])
        result = self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.assertIsNone(result["usage"])
        self.assertEqual(result["missing_evidence"], ["INVOICE_USAGE_PERIOD_MISMATCH"])
        self.assertEqual(result["observed_usage_period"]["from_datetime"], "2026-10-02T00:00:00Z")

    def test_missing_invoice_fee_period_preserves_unknown(self) -> None:
        self.client.get_invoice_for_customer = Mock(return_value={**self.invoice, "fees": []})
        self.client.get_past_usage = Mock(return_value=[self.period])
        result = self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.assertIsNone(result["usage"])
        self.assertEqual(result["missing_evidence"], ["INVOICE_FEE_PERIOD_UNPROVEN"])

    def test_multi_subscription_invoice_refuses_arbitrary_first(self) -> None:
        invoice = deepcopy(self.invoice)
        invoice["subscriptions"].append({"external_id": "sub-b", "external_customer_id": "customer-a"})
        self.client.get_invoice_for_customer = Mock(return_value=invoice)
        self.client.get_past_usage = Mock()
        with self.assertRaises(IntegrationError) as caught:
            self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.assertEqual(caught.exception.code, "INVOICE_SUBSCRIPTION_AMBIGUOUS")
        self.client.get_past_usage.assert_not_called()

    def test_usage_failure_is_not_zero_and_wrong_period_is_not_accepted(self) -> None:
        self.client.get_invoice_for_customer = Mock(return_value=self.invoice)
        self.client.get_past_usage = Mock(side_effect=IntegrationError("EXTERNAL_UNAVAILABLE"))
        with self.assertRaises(IntegrationError):
            self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.client.get_past_usage = Mock(return_value=[{**self.period, "from_datetime": "2026-12-01T00:00:00Z"}])
        with self.assertRaises(IntegrationError) as caught:
            self.client.resolve_invoice_scope("customer-a", "invoice-a")
        self.assertEqual(caught.exception.code, "LAGO_USAGE_PERIOD_INVALID")

    def test_customer_invoice_listing_follows_native_pagination(self) -> None:
        self.client.http.request = Mock(side_effect=[
            {"invoices": [{"lago_id": "a"}], "meta": {"next_page": 2}},
            {"invoices": [{"lago_id": "b"}], "meta": {"next_page": None}},
        ])
        self.assertEqual([row["lago_id"] for row in self.client.get_customer_invoices("customer-a")], ["a", "b"])
        self.assertEqual(self.client.http.request.call_args.kwargs["query"]["page"], 2)


class ChatwootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ChatwootClient("http://127.0.0.1:1234", "test-secret", 1)

    def test_private_note_is_posted_once_and_read_back(self) -> None:
        note = {"id": 10, "conversation_id": 3, "private": True, "content": "draft"}
        self.client.http.request = Mock(return_value=note)
        self.client.get_messages = Mock(return_value=[note])
        self.assertEqual(self.client.create_private_note(3, "draft")["id"], 10)
        self.assertIs(self.client.http.request.call_args.kwargs["json_body"]["private"], True)
        self.assertEqual(self.client.http.request.call_count, 1)

    def test_write_readback_missing_or_public_is_unknown(self) -> None:
        note = {"id": 10, "conversation_id": 3, "private": True, "content": "draft"}
        for observed in ([], [{**note, "private": False}], [{**note, "content": "changed"}]):
            self.client.http.request = Mock(return_value=note)
            self.client.get_messages = Mock(return_value=observed)
            with self.subTest(observed=observed), self.assertRaises(IntegrationError) as caught:
                self.client.create_private_note(3, "draft")
            self.assertEqual(caught.exception.outcome, "UNKNOWN")
            self.assertEqual(self.client.http.request.call_count, 1)

    def test_internal_note_never_becomes_customer_input(self) -> None:
        rows = [{"id": 1, "private": False, "message_type": 0},
                {"id": 2, "private": True, "message_type": 1},
                {"id": 3, "private": False, "message_type": "outgoing"},
                {"id": 4, "private": True, "message_type": "incoming"}]
        self.assertEqual(customer_messages(rows), [rows[0]])

    def test_messages_are_paginated_without_silently_truncating(self) -> None:
        self.client.http.request = Mock(side_effect=[{"payload": [{"id": 10, "conversation_id": 3}]},
            {"payload": [{"id": 2, "conversation_id": 3}]}, {"payload": []}])
        self.assertEqual([row["id"] for row in self.client.get_messages(3)], [2, 10])
        self.assertEqual(self.client.http.request.call_args.kwargs["query"], {"before": 2})


if __name__ == "__main__":
    unittest.main()
