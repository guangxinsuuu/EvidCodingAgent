"""来源协议、真实 PostgreSQL 与实际 loopback HTTP 验证。

不使用 SQLite/in-memory 替代持久协议；EGAP_COMMERCIAL_TEST_DATABASE_URL
未设置时明确 SKIP 数据库/HTTP 测试。每例独立租户，不删除其他实验的数据。
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
from threading import Barrier, Thread
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
import uvicorn

from commercial_source.protocol import Settings, SourceError, digest, parse_command, source_document
from commercial_source.server import create_app
from commercial_source.store import CommercialStore


TEST_DSN = os.environ.get("EGAP_COMMERCIAL_TEST_DATABASE_URL")


def document(text="仅用于实验的退款附加条款，不代表企业批准。"):
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"source_document": {"text": text, "reference": "test-source", "origin": "SYNTHETIC", "sha256": sha},
            "structured_source_sha256": sha, "review_status": "LOCAL_CALIBRATION"}


class ProtocolTests(unittest.TestCase):
    def test_original_and_structured_hash_must_match(self):
        payload = document()
        self.assertEqual(source_document(payload)["sha256"], payload["structured_source_sha256"])
        payload["source_document"]["text"] += "修改"
        with self.assertRaisesRegex(SourceError, "SOURCE_DOCUMENT_HASH_MISMATCH"):
            source_document(payload)

    def test_no_forged_approval_or_answer_in_commands(self):
        for field in ("expected", "expected_eligibility", "eligibility", "approved"):
            with self.subTest(field=field), self.assertRaisesRegex(SourceError, "FORBIDDEN"):
                parse_command({"operation_key": "op", "type": "put_policy", "payload": {"tenant_id": "tenant", "nested": {field: True}}})

    def test_review_status_and_token_roles_are_explicit(self):
        value = document()
        value["review_status"] = "COMPANY_APPROVED"
        with self.assertRaisesRegex(SourceError, "INVALID_REVIEW_STATUS"):
            source_document(value)
        with self.assertRaisesRegex(SourceError, "SERVICE_ROLES_MUST_DIFFER"):
            Settings("unused", "a" * 32, "a" * 32, ("tenant",), "instance", "2099-01-01T00:00:00Z")

    def test_private_runtime_requires_0600_and_rejects_symlink(self):
        config = {"database_url": "unused", "admin_token": "a" * 32, "read_token": "b" * 32,
                  "tenant_scopes": ["tenant"], "instance_id": "instance", "expires_at": "2099-01-01T00:00:00Z"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            path.write_text(json.dumps(config))
            path.chmod(0o644)
            with self.assertRaisesRegex(SourceError, "PRIVATE_CONFIG_MODE_REQUIRED"):
                Settings.from_private_file(str(path))
            path.chmod(0o600)
            self.assertEqual(Settings.from_private_file(str(path)).tenant_scopes, ("tenant",))
            alias = Path(directory) / "alias"
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                Settings.from_private_file(str(alias))


@unittest.skipUnless(TEST_DSN, "requires EGAP_COMMERCIAL_TEST_DATABASE_URL (real dedicated PostgreSQL)")
class DatabaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = CommercialStore(TEST_DSN)
        # 已明确配置却不可达时必须 FAIL，不把部署错误隐匿为成功或 SKIP。
        cls.store.migrate()

    def setUp(self):
        self.tenant = "commercial-test-" + uuid4().hex
        self.sequence = 0

    def call(self, kind, payload, key=None):
        self.sequence += 1
        return self.store.execute({"operation_key": key or f"op-{self.sequence}", "type": kind,
                                   "payload": {"tenant_id": self.tenant, **payload}})

    def policy(self, **changes):
        return {"policy_id": "refund", "version": "v1", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
                "terms": {"selection": "RENEWAL_EFFECTIVE_AT", "window_seconds": 1209600, "window_endpoint": "INCLUSIVE",
                          "usage_scope": "RENEWAL_TO_CLAIM", "metric_code": "units-test", "unit": "units",
                          "maximum_usage_units": "0", "require_payment": True, "allowed_payment_source": "SIMULATED_PAYMENT",
                          "output_scope": "LOCAL_LAB_ADVISORY_ONLY"}, **document(), **changes}

    def prepare(self, *, renewal=True):
        self.call("put_policy", self.policy())
        self.call("put_contract", {"contract_id": "contract", "version": "v1", "customer_id": "customer", "subscription_id": "subscription",
            "effective_from": "2026-01-01T00:00:00Z", "effective_to": None, "status": "ACTIVE",
            "policy_ref": {"policy_id": "refund", "version": None}, "terms": {"policy_selection": "RENEWAL_EFFECTIVE_AT", "special_terms": False},
            "acceptance": {"kind": "SIMULATED_ACCEPTANCE", "accepted_at": "2026-01-01T00:00:00Z"}, **document()})
        return self.call("activate_subscription", {"customer_id": "customer", "subscription_id": "subscription", "contract_ref": {"contract_id": "contract", "version": "v1"},
            "period_start": "2026-10-01T00:00:00Z", "period_end": "2026-10-30T23:59:59Z", "renewal_enabled": renewal})["result"]

    def linked(self):
        self.prepare()
        renewal = self.call("advance_clock", {"subscription_id": "subscription", "to": "2026-10-31T00:00:00Z"})["result"]["events"][0]
        self.call("link_invoice", self.invoice_payload(renewal))
        return renewal

    def invoice_payload(self, event):
        return {"customer_id": "customer", "subscription_id": "subscription", "invoice_id": "invoice", "event_id": event["event_id"],
            "native_invoice_kind": "INITIAL", "native_period_start": event["period_start"], "native_period_end": event["period_end"],
            "source_receipt": {"method": "GET", "path": "/api/v1/invoices/invoice", "status": 200, "observed_at": "2026-11-01T00:00:00Z",
                "invoice": {"lago_id": "invoice", "customer": {"external_id": "customer"},
                    "subscriptions": [{"external_id": "subscription", "external_customer_id": "customer"}],
                    "fees": [{"external_subscription_id": "subscription", "item": {"type": "subscription"}, "from_date": event["period_start"], "to_date": event["period_end"]}]}}}

    def scope(self, **changes):
        return {"tenant_id": self.tenant, "customer_id": "customer", "subscription_id": "subscription", "invoice_id": "invoice",
                "account_id": 1, "conversation_id": 2, **changes}

    def claim(self):
        return self.call("create_claim", {"claim_id": "claim", **{k: v for k, v in self.scope().items() if k != "tenant_id"},
            "original_message": {"message_id": 3, "created_at": "2026-11-09T00:00:00Z", "content_sha256": "a" * 64}})["result"]

    def open_coverage(self, expected):
        return self.call("open_coverage", {"coverage_id": "coverage", "customer_id": "customer", "subscription_id": "subscription", "invoice_id": "invoice",
            "metric_code": "units-test", "unit": "units", "period_start": "2026-10-31T00:00:00Z", "period_end": "2026-11-01T00:00:00Z", "expected_events": expected})["result"]

    def reconcile(self, units="0", **changes):
        return self.call("reconcile_coverage", {"coverage_id": "coverage", "raw_response": {
            "lago_invoice_id": "invoice", "from_datetime": "2026-10-31T00:00:00Z", "to_datetime": "2026-11-29T23:59:59Z",
            "charges_usage": [{"units": units, "billable_metric": {"code": "units-test"}}], **changes},
            "receipt": {"service": "lago", "method": "GET", "path": "/api/v1/customers/customer/current_usage", "status": 200,
                        "observed_at": "2026-11-09T00:00:00Z", "query": {"external_subscription_id": "subscription"}}})["result"]


class PersistenceTests(DatabaseCase):
    def test_idempotency_conflict_and_immutable_asset_version(self):
        first = self.call("put_policy", self.policy(), key="same")
        self.assertEqual(first, self.call("put_policy", self.policy(), key="same"))
        with self.assertRaisesRegex(SourceError, "OPERATION_KEY_CONFLICT"):
            self.call("put_policy", self.policy(version="v2"), key="same")
        changed = self.policy()
        changed["terms"]["window_seconds"] = 1
        with self.assertRaisesRegex(SourceError, "IMMUTABLE_ASSET_VERSION"):
            self.call("put_policy", changed)
        self.assertEqual(self.store.context(self.scope())["source_revision"], first["source_revision"])

    def test_concurrent_identical_command_is_one_committed_version(self):
        command = {"operation_key": "concurrent", "type": "put_policy", "payload": {"tenant_id": self.tenant, **self.policy()}}
        barrier = Barrier(2)
        def invoke(_index):
            barrier.wait(timeout=5)
            return self.store.execute(command)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(invoke, (0, 1)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.store.context(self.scope())["policies"]), 1)

    def test_renewal_is_state_driven_and_duplicate_advance_does_not_duplicate(self):
        activated = self.prepare()
        self.assertEqual(activated["events"][0]["event_type"], "SUBSCRIPTION_ACTIVATED")
        renewal = self.call("advance_clock", {"subscription_id": "subscription", "to": "2026-10-31T00:00:00Z"})
        self.assertEqual(renewal["result"]["events"][0]["term_number"], 2)
        self.assertEqual(renewal["result"]["events"][0]["event_type"], "SUBSCRIPTION_RENEWAL_EFFECTIVE")
        duplicate = self.call("advance_clock", {"subscription_id": "subscription", "to": "2026-10-31T00:00:00Z"})
        self.assertEqual(duplicate["result"]["events"], [])
        self.assertEqual(duplicate["source_revision"], renewal["source_revision"])
        restarted = CommercialStore(TEST_DSN)
        self.assertEqual(len(restarted.context(self.scope())["events"]), 2)
        with self.assertRaisesRegex(SourceError, "CANNOT_REWIND"):
            self.call("advance_clock", {"subscription_id": "subscription", "to": "2026-10-30T00:00:00Z"})

    def test_cancel_before_boundary_prevents_renewal(self):
        self.prepare()
        self.call("cancel_renewal", {"subscription_id": "subscription"})
        result = self.call("advance_clock", {"subscription_id": "subscription", "to": "2026-12-01T00:00:00Z"})["result"]
        self.assertEqual(result["events"], [])
        self.assertEqual(result["subscription"]["status"], "ENDED")
        self.assertFalse(any(e["event_type"] == "SUBSCRIPTION_RENEWAL_EFFECTIVE" for e in self.store.context(self.scope())["events"]))

    def test_termination_cannot_be_relabelled_as_renewal(self):
        self.prepare()
        terminated = self.call("terminate_subscription", {"subscription_id": "subscription"})["result"]["events"][0]
        self.assertEqual(terminated["event_type"], "SUBSCRIPTION_TERMINATED")
        self.call("advance_clock", {"subscription_id": "subscription", "to": "2026-12-01T00:00:00Z"})
        self.assertEqual(len(self.store.context(self.scope())["events"]), 2)

    def test_invoice_mapping_requires_owner_and_exact_period(self):
        self.prepare()
        event = self.call("advance_clock", {"subscription_id": "subscription", "to": "2026-10-31T00:00:00Z"})["result"]["events"][0]
        payload = self.invoice_payload(event)
        payload["native_period_end"] = "2026-12-01T00:00:00Z"
        with self.assertRaisesRegex(SourceError, "INVOICE_EVENT_PERIOD_MISMATCH"):
            self.call("link_invoice", payload)
        payload = self.invoice_payload(event)
        payload["source_receipt"]["invoice"]["customer"]["external_id"] = "foreign"
        with self.assertRaisesRegex(SourceError, "NATIVE_INVOICE_SCOPE_MISMATCH"):
            self.call("link_invoice", payload)
        payload = self.invoice_payload(event)
        payload["native_invoice_kind"] = "TERMINATION"
        with self.assertRaisesRegex(SourceError, "TERMINATION_IS_NOT_RENEWAL"):
            self.call("link_invoice", payload)

    def test_claim_original_time_and_message_are_immutable(self):
        self.linked()
        original = self.claim()
        self.assertNotEqual(original["claimed_at"], original["original_message"]["created_at"])
        self.call("advance_clock", {"subscription_id": "subscription", "to": "2026-11-01T00:00:00Z"})
        updated = self.call("supplement_claim", {"claim_id": "claim", "message": {"message_id": 4, "created_at": "2026-11-10T00:00:00Z", "content_sha256": "b" * 64}})["result"]
        self.assertEqual(updated["claimed_at"], original["claimed_at"])
        self.assertEqual(updated["original_message"], original["original_message"])
        self.assertEqual(updated["revision"], 2)
        with self.assertRaisesRegex(SourceError, "IMMUTABLE_MESSAGE_REFERENCE"):
            self.call("supplement_claim", {"claim_id": "claim", "message": {"message_id": 3, "created_at": original["original_message"]["created_at"], "content_sha256": "b" * 64}})

    def test_zero_usage_requires_native_reconciliation_even_with_empty_set(self):
        self.linked()
        self.open_coverage([])
        with self.assertRaisesRegex(SourceError, "NATIVE_AGGREGATE_UNRECONCILED"):
            self.call("seal_coverage", {"coverage_id": "coverage"})
        self.reconcile("0")
        result = self.call("seal_coverage", {"coverage_id": "coverage"})["result"]
        self.assertEqual(result["status"], "SEALED")
        self.assertEqual(result["completeness_scope"], "LOCAL_CONTROLLED_GENERATOR_ONLY")
        self.assertEqual(result["pending_count"], 0)

    def test_pending_event_and_late_arrival_invalidate_old_zero(self):
        self.linked()
        self.open_coverage([{"event_id": "usage-1", "units": "3", "occurred_at": "2026-10-31T01:00:00Z", "native_occurred_at": "2026-11-09T00:00:00Z"}])
        self.reconcile("0")
        with self.assertRaisesRegex(SourceError, "USAGE_EVENTS_INCOMPLETE"):
            self.call("seal_coverage", {"coverage_id": "coverage"})
        with self.assertRaisesRegex(SourceError, "USAGE_NOT_SENT"):
            self.call("record_usage_received", {"coverage_id": "coverage", "event_id": "usage-1", "receipt": {"status": 200}})
        self.call("record_usage_sent", {"coverage_id": "coverage", "event_id": "usage-1", "receipt": {"transaction_id": "usage-1"}})
        self.call("record_usage_received", {"coverage_id": "coverage", "event_id": "usage-1", "receipt": {"status": 200}})
        with self.assertRaisesRegex(SourceError, "NATIVE_AGGREGATE_UNRECONCILED"):
            self.call("seal_coverage", {"coverage_id": "coverage"})
        self.reconcile("3")
        result = self.call("seal_coverage", {"coverage_id": "coverage"})["result"]
        self.assertEqual(result["units"], "3")

    def test_business_subinterval_does_not_relabel_whole_native_aggregate(self):
        self.linked()
        self.open_coverage([{"event_id": "usage-after-claim", "units": "7", "occurred_at": "2026-11-02T00:00:00Z", "native_occurred_at": "2026-11-09T00:00:00Z"}])
        for kind in ("record_usage_sent", "record_usage_received"):
            self.call(kind, {"coverage_id": "coverage", "event_id": "usage-after-claim", "receipt": {"transaction_id": "usage-after-claim", "status": 200}})
        self.reconcile("7")
        coverage = self.call("seal_coverage", {"coverage_id": "coverage"})["result"]
        self.assertEqual(coverage["units"], "0")
        self.assertEqual(coverage["native_expected_units"], "7")

    def test_raw_lago_metric_period_and_invoice_must_match(self):
        self.linked()
        self.open_coverage([])
        for change in ({"lago_invoice_id": "foreign"}, {"to_datetime": "2026-11-30T23:59:59Z"}):
            with self.subTest(change=change), self.assertRaisesRegex(SourceError, "LAGO_USAGE_SCOPE_MISMATCH"):
                self.reconcile("0", **change)
        with self.assertRaisesRegex(SourceError, "LAGO_USAGE_METRIC_AMBIGUOUS"):
            self.reconcile("0", charges_usage=[{"units": "0", "billable_metric": {"code": "foreign"}}])

    def test_current_subscription_usage_keeps_missing_native_invoice_id(self):
        self.linked()
        self.open_coverage([])
        payload = {"coverage_id": "coverage", "raw_response": {
            "from_datetime": "2026-10-31T00:00:00Z", "to_datetime": "2026-11-29T23:59:59Z",
            "charges_usage": [{"units": "0", "billable_metric": {"code": "units-test"}}]},
            "receipt": {"service": "lago", "scope_kind": "SUBSCRIPTION_CURRENT_PERIOD", "method": "GET",
                        "path": "/api/v1/customers/customer/current_usage", "status": 200,
                        "observed_at": "2026-11-09T00:00:00Z", "query": {"external_subscription_id": "subscription"}}}
        observed = self.call("reconcile_coverage", payload)["result"]["reconciliation"]
        self.assertTrue(observed["matched"])
        self.assertNotIn("lago_invoice_id", observed["raw_response"])
        self.assertEqual(observed["raw_response_sha256"], digest(payload["raw_response"]))
        payload["receipt"]["path"] = "/api/v1/customers/foreign/current_usage"
        with self.assertRaisesRegex(SourceError, "LAGO_CURRENT_USAGE_SCOPE_MISMATCH"):
            self.call("reconcile_coverage", payload)

    def test_failed_command_rolls_back_entire_tenant_business_revision(self):
        self.prepare()
        initial = self.store.context(self.scope())["source_revision"]
        # 单次超过120个term会先生成若干事件再报错，事务必须整体回滚。
        with self.assertRaisesRegex(SourceError, "BUSINESS_CLOCK_ADVANCE_LIMIT"):
            self.call("advance_clock", {"subscription_id": "subscription", "to": "2040-01-01T00:00:00Z"})
        result = self.store.context(self.scope())
        self.assertEqual(result["source_revision"], initial)
        self.assertEqual(result["subscription"]["term_number"], 1)
        self.assertEqual(len(result["events"]), 1)

    def test_payment_is_only_simulated_and_context_enforces_identity(self):
        self.linked()
        self.claim()
        payment = {"payment_id": "payment", "customer_id": "customer", "subscription_id": "subscription", "invoice_id": "invoice",
            "source_kind": "SIMULATED_PAYMENT", "authority_scope": "LOCAL_LAB", "status": "PAID", "amount_minor": 1000,
            "currency": "USD", "paid_at": "2026-10-31T00:00:00Z", "receipt_ref": "simulated-receipt"}
        self.call("record_payment", payment)
        self.assertEqual(self.store.context(self.scope())["payment"]["source_kind"], "SIMULATED_PAYMENT")
        with self.assertRaisesRegex(SourceError, "ONLY_LOCAL_SIMULATED_PAYMENT"):
            self.call("record_payment", {**payment, "source_kind": "ACTUAL_PAYMENT"})
        with self.assertRaisesRegex(SourceError, "CONTEXT_SCOPE_MISMATCH"):
            self.store.context(self.scope(customer_id="foreign"))
        self.assertIsNone(self.store.context(self.scope(conversation_id=99))["claim"])


class HttpTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        settings = Settings(TEST_DSN, "admin-" + "a" * 32, "reader-" + "b" * 32, (self.tenant,), "http-test",
                            (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        self.settings = settings
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.url = "http://127.0.0.1:" + str(self.listener.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(create_app(settings, self.store), log_level="critical", access_log=False))
        self.thread = Thread(target=lambda: self.server.run(sockets=[self.listener]), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started, "real HTTP listener did not start")

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.listener.close()
        self.assertFalse(self.thread.is_alive())

    def http(self, path, *, token=None, value=None, raw=None):
        body = raw if raw is not None else (json.dumps(value).encode() if value is not None else None)
        headers = {"Authorization": "Bearer " + token} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=body, headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_real_http_role_scope_and_duplicate_key_rejection(self):
        command = {"operation_key": "http", "type": "put_policy", "payload": {"tenant_id": self.tenant, **self.policy()}}
        self.assertEqual(self.http("/v1/commands", token=self.settings.read_token, value=command)[0], 401)
        self.assertEqual(self.http("/v1/commands", token=self.settings.admin_token, value=command)[0], 200)
        path = "/v1/context?" + urlencode(self.scope())
        status, context = self.http(path, token=self.settings.read_token)
        self.assertEqual(status, 200)
        self.assertEqual(context["source_kind"], "SYNTHETIC_BUSINESS_SOURCE")
        self.assertNotIn("eligibility", context)
        self.assertEqual(self.http(path, token=self.settings.admin_token)[0], 401)
        self.assertEqual(self.http("/v1/context?" + urlencode(self.scope(tenant_id="foreign")), token=self.settings.read_token)[0], 403)
        self.assertEqual(self.http(path + "&tenant_id=foreign", token=self.settings.read_token)[0], 422)
        self.assertEqual(self.http("/v1/commands", token=self.settings.admin_token, raw=b'{"type":"put_policy","type":"put_contract"}')[0], 422)

    def test_real_http_retry_returns_original_and_conflict_409(self):
        command = {"operation_key": "http", "type": "put_policy", "payload": {"tenant_id": self.tenant, **self.policy()}}
        first = self.http("/v1/commands", token=self.settings.admin_token, value=command)
        self.assertEqual(first, self.http("/v1/commands", token=self.settings.admin_token, value=command))
        command["payload"]["version"] = "v2"
        self.assertEqual(self.http("/v1/commands", token=self.settings.admin_token, value=command)[0], 409)
        self.assertEqual(self.http("/health/ready")[0], 200)


if __name__ == "__main__":
    unittest.main()
