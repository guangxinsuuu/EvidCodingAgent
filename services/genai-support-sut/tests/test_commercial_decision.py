"""产品决定的确定性单测；独立手算预期，不把商业服务或本函数当发布 Oracle。

通过完整范围的最小原生 usage 结构检查边界，而不是把答案塞进 context。
这些测试不调用真实来源，也不证明自然续费、支付商或企业批准已经接入。
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock

from genai_support.integrations.commercial import CommercialClient, commercial_decision, normalized_lago_usage
from genai_support.integrations.http import IntegrationError
from genai_support.integrations.persistence import assessment_facts_digest
from genai_support.business_tools import assemble_draft
from genai_support.protocol import validate_support_job


def source_asset(identifier, **fields):
    text = "实验标准退款补充条款 EGAP-LAB-REFUND-1；仅本地校准。" + identifier
    hashed = sha256(text.encode()).hexdigest()
    return {"review_status": "LOCAL_CALIBRATION", "source_document": {"reference": identifier,
        "sha256": hashed, "origin": "SYNTHETIC", "text": text}, "structured_source_sha256": hashed, **fields}


def example_context(*, renewed="2026-10-31T00:00:00Z", elapsed_days=10, units="0"):
    """旧政策续费+新政策期间申请；预期由 14/7 天算术独立给出。"""
    start = datetime.fromisoformat(renewed.replace("Z", "+00:00"))
    claimed = (start + timedelta(days=elapsed_days)).isoformat()
    end = (start + timedelta(days=30)).isoformat()
    scope = {"tenant_id": "t", "customer_id": "c", "subscription_id": "s", "invoice_id": "i", "account_id": 1, "conversation_id": 2}
    message = {"id": 7, "created_at": 1_790_000_000, "content": "请审核退款资格"}
    period = {"from_datetime": renewed, "to_datetime": end}
    usage = {**period, "lago_invoice_id": "i", "charges_usage": [
        {"billable_metric": {"code": "requests"}, "units": units},
        {"billable_metric": {"code": "unrelated"}, "units": "100000"}]}
    policies = [source_asset("policy-" + version, policy_id="refund", version=version,
        effective_from=left, effective_to=right, terms={"selection": "RENEWAL_EFFECTIVE_AT",
            "window_seconds": days * 86400, "window_endpoint": "INCLUSIVE", "usage_scope": "RENEWAL_TO_CLAIM",
            "metric_code": "requests", "unit": "units", "maximum_usage_units": "0", "require_payment": True,
            "allowed_payment_source": "SIMULATED_PAYMENT", "output_scope": "LOCAL_LAB_ADVISORY_ONLY"})
        for version, left, right, days in (("v1", "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z", 14),
                                         ("v2", "2026-11-01T00:00:00Z", None, 7))]
    contract = source_asset("contract-source", contract_id="contract", version="v1", customer_id="c", subscription_id="s",
        effective_from="2026-10-01T00:00:00Z", effective_to=None, status="ACTIVE",
        policy_ref={"policy_id": "refund", "version": None}, terms={"refund_addendum": "EGAP-LAB-REFUND-1",
            "special_terms": False, "policy_selection": "RENEWAL_EFFECTIVE_AT"},
        acceptance={"kind": "SIMULATED_ACCEPTANCE", "accepted_at": "2026-10-01T00:00:00Z"})
    events = [] if units == "0" else [{"event_id": "usage-1", "units": units, "occurred_at": renewed, "native_occurred_at": renewed}]
    source = {"source_kind": "SYNTHETIC_BUSINESS_SOURCE", "authority_scope": "LOCAL_LAB", "source_revision": 1, "scope": scope,
        "policies": policies, "contracts": [contract],
        "events": [{"event_id": "renewal-2", "event_type": "SUBSCRIPTION_RENEWAL_EFFECTIVE", "customer_id": "c",
            "subscription_id": "s", "term_number": 2, "contract_ref": {"contract_id": "contract", "version": "v1"}, "effective_at": renewed,
            "period_start": renewed, "period_end": end, "invoice_refs": ["i"]}],
        "claim": {"claim_id": "claim", "customer_id": "c", "subscription_id": "s", "invoice_id": "i", "claimed_at": claimed,
            "account_id": 1, "conversation_id": 2,
            "original_message": {"message_id": 7, "created_at": datetime.fromtimestamp(message["created_at"], timezone.utc).isoformat(), "content_sha256": sha256(message["content"].encode()).hexdigest()}},
        "invoice_link": {"business_period_start": renewed, "business_period_end": end, "native_period_start": renewed,
            "native_period_end": end, "native_invoice_kind": "INITIAL", "mapping_kind": "EXACT_PERIOD_LOCAL_BUSINESS_MAPPING"},
        "coverage": {"coverage_id": "coverage", "revision": 1, "status": "SEALED", "completeness_scope": "LOCAL_CONTROLLED_GENERATOR_ONLY",
            "customer_id": "c", "subscription_id": "s", "invoice_id": "i", "metric_code": "requests", "unit": "units",
            "period_start": renewed, "period_end": claimed, "interval": "CLOSED", "expected_events": events,
            "sent_event_ids": [row["event_id"] for row in events], "received_event_ids": [row["event_id"] for row in events],
            "pending_count": 0, "units": units, "native_expected_units": units, "sealed_at": claimed,
            "reconciliation": {"matched": True, "lago_units": units,
                "raw_response_sha256": sha256(json.dumps(usage, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
                "raw_response": deepcopy(usage), "receipt": {"service": "lago", "method": "GET", "path": "/past_usage", "status": 200, "observed_at": claimed},
                "native_period_start": renewed, "native_period_end": end}},
        "payment": {"payment_id": "pay", "customer_id": "c", "subscription_id": "s", "invoice_id": "i",
            "source_kind": "SIMULATED_PAYMENT", "authority_scope": "LOCAL_LAB", "status": "PAID", "amount_minor": 100,
            "currency": "USD", "paid_at": renewed, "receipt_ref": "simulated-payment-1"}}
    return {"ticket": {"tenant_id": "t", "received_at": claimed}, "billing": {"billing_record_id": "lago-invoice:i",
        "invoice_id": "i", "external_customer_id": "c", "currency": "USD", "total_amount_cents": 100},
        "subscription": {"subscription_id": "lago-sub:s", "external_subscription_id": "s"},
        "usage": usage, "invoice_period": period, "observed_usage_period": period,
        "lago_query_scope": {"customer_id": "c", "subscription_id": "s", "invoice_id": "i"},
        "customer_message_refs": [message], "commercial_source": source, "missing_evidence": []}


class CommercialDecisionTests(unittest.TestCase):
    def test_document_paths_become_valid_draft_evidence_identifiers(self):
        context = example_context()
        context["ticket"].update(ticket_id="cw:1:2", revision=1)
        context["commercial_source"]["policies"][0]["source_document"]["reference"] = "business-source-pack/v1/policy.md#v1"
        context["commercial_source"]["contracts"][0]["source_document"]["reference"] = "business-source-pack/v1/contract.md#customer"
        decision = commercial_decision(context)
        draft = assemble_draft(context, decision, "仅本地实验规则；模拟付款已核对，待人工审核，未执行退款。")
        request = {"schema_version": 1, "job_id": "a" * 32, "tenant_id": "t", "ticket_id": "cw:1:2", "ticket_revision": 1,
            "status": "DONE", "draft": draft, "error_code": None}
        validate_support_job(request)
        self.assertTrue(any(ref.startswith("lab-doc:") for ref in draft["evidence_refs"]))
        self.assertFalse(any("/" in ref or "#" in ref for ref in draft["evidence_refs"]))

    def test_fact_digest_ignores_observer_times_global_revision_and_unrelated_policy(self):
        context = example_context()
        before = assessment_facts_digest(context)
        altered = deepcopy(context)
        altered["source_observed_at"] = "new-observation"
        altered["usage"]["updated_at"] = "new-cache-time"
        altered["usage"]["charges_usage"][0]["observed_at"] = "new-observation"
        source = altered["commercial_source"]
        source["source_revision"] += 100
        source["coverage"]["reconciliation"]["receipt"]["observed_at"] = "later-read"
        source["coverage"]["reconciliation"]["raw_response"]["updated_at"] = "later-cache"
        source["coverage"]["reconciliation"]["raw_response_sha256"] = "changed-observer-hash"
        unrelated = deepcopy(source["policies"][0])
        unrelated.update(policy_id="other-product", version="irrelevant")
        source["policies"].append(unrelated)
        later = deepcopy(source["policies"][1])
        later.update(version="future", effective_from="2027-01-01T00:00:00Z")
        source["policies"].append(later)
        source["subscription"] = {"business_clock": "2027-02-01T00:00:00Z", "term_number": 12}
        event = deepcopy(source["events"][0])
        event.update(event_id="future-event", effective_at="2027-02-01T00:00:00Z", invoice_refs=["future-invoice"])
        source["events"].append(event)
        self.assertEqual(assessment_facts_digest(altered), before)

    def test_fact_digest_keeps_applicable_policy_overlap_and_business_times(self):
        context = example_context()
        before = assessment_facts_digest(context)
        changes = [lambda c: c["commercial_source"]["policies"][0]["terms"].update(window_seconds=7 * 86400),
            lambda c: c["commercial_source"]["payment"].update(paid_at="2026-11-01T00:00:00Z"),
            lambda c: c["commercial_source"]["claim"].update(claimed_at="2026-11-02T00:00:00Z"),
            lambda c: c["commercial_source"]["events"][0].update(effective_at="2026-10-30T00:00:00Z"),
            lambda c: c["commercial_source"]["contracts"][0]["acceptance"].update(accepted_at="2026-11-01T00:00:00Z"),
            lambda c: c["usage"]["charges_usage"][0].update(units="9")]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                altered = deepcopy(context)
                change(altered)
                self.assertNotEqual(assessment_facts_digest(altered), before)
        overlap = deepcopy(context)
        overlap["commercial_source"]["policies"].append({**deepcopy(overlap["commercial_source"]["policies"][0]), "version": "overlap"})
        self.assertNotEqual(assessment_facts_digest(overlap), before)

    def test_historical_renewal_uses_14_days_not_latest_7_days(self):
        result = commercial_decision(example_context())
        self.assertEqual((result["eligibility"], result["policy_ref"], result["window_seconds"]), ("ELIGIBLE", "refund:v1", 14 * 86400))
        self.assertEqual(result["proposed_action"], "HUMAN_REVIEW")
        self.assertEqual(result["assessment_scope"], "LAB_SCOPED")
        self.assertTrue(result["payment_simulated"])

    def test_policy_change_boundary_uses_new_version_and_rejects_late_claim(self):
        result = commercial_decision(example_context(renewed="2026-11-01T00:00:00Z", elapsed_days=8))
        self.assertEqual((result["eligibility"], result["policy_ref"]), ("INELIGIBLE", "refund:v2"))
        self.assertIn("REFUND_WINDOW_EXCEEDED", result["reason_codes"])

    def test_inclusive_second_endpoint_and_one_second_late(self):
        context = example_context(renewed="2026-11-01T00:00:00Z", elapsed_days=7)
        self.assertEqual(commercial_decision(context)["eligibility"], "ELIGIBLE")
        later = (datetime.fromisoformat(context["commercial_source"]["claim"]["claimed_at"]) + timedelta(seconds=1)).isoformat()
        context["commercial_source"]["claim"]["claimed_at"] = later
        context["commercial_source"]["coverage"]["period_end"] = later
        self.assertEqual(commercial_decision(context)["eligibility"], "INELIGIBLE")

    def test_used_units_are_specific_metric_and_zero_is_not_missing(self):
        for units, eligibility in (("0", "ELIGIBLE"), ("11", "INELIGIBLE")):
            with self.subTest(units=units):
                result = commercial_decision(example_context(units=units))
                self.assertEqual(result["eligibility"], eligibility)
                self.assertEqual(result["usage_units"], units)

    def test_top_level_units_and_ambiguous_metric_never_replace_missing_native_measurement(self):
        context = example_context()
        context["usage"] = {"units": 0}
        self.assertIsNone(normalized_lago_usage(context, "requests", "units"))
        self.assertEqual(commercial_decision(context)["eligibility"], "NEEDS_MORE_EVIDENCE")
        context = example_context()
        context["usage"]["charges_usage"].append(deepcopy(context["usage"]["charges_usage"][0]))
        self.assertIsNone(normalized_lago_usage(context, "requests", "units"))

    def test_each_missing_or_wrong_business_source_prevents_eligibility(self):
        mutations = {
            "pending_policy": lambda s: s["policies"][0].update(review_status="PENDING_USER_REVIEW"),
            "source_hash": lambda s: s["contracts"][0].update(structured_source_sha256="0" * 64),
            "original_text": lambda s: s["contracts"][0]["source_document"].update(text="changed"),
            "draft_contract": lambda s: s["contracts"][0].update(status="DRAFT"),
            "unaccepted": lambda s: s["contracts"][0].update(acceptance=None),
            "expired": lambda s: s["contracts"][0].update(effective_to="2026-10-30T00:00:00Z"),
            "expired_before_claim": lambda s: s["contracts"][0].update(effective_to="2026-11-01T00:00:00Z"),
            "activation": lambda s: s["events"][0].update(event_type="SUBSCRIPTION_ACTIVATED"),
            "termination": lambda s: s["events"][0].update(event_type="SUBSCRIPTION_TERMINATED"),
            "term_one": lambda s: s["events"][0].update(term_number=1),
            "termination_invoice": lambda s: s["invoice_link"].update(native_invoice_kind="TERMINATION"),
            "pending_events": lambda s: s["coverage"].update(pending_count=1),
            "open_set": lambda s: s["coverage"].update(status="OPEN"),
            "wrong_metric": lambda s: s["coverage"].update(metric_code="unrelated"),
            "wrong_unit": lambda s: s["coverage"].update(unit="bytes"),
            "wrong_interval": lambda s: s["coverage"].update(period_start="2026-10-30T00:00:00Z"),
            "unpaid": lambda s: s.update(payment=None),
            "wrong_payment_invoice": lambda s: s["payment"].update(invoice_id="other"),
            "underpayment": lambda s: s["payment"].update(amount_minor=99),
            "overpayment": lambda s: s["payment"].update(amount_minor=101),
            "wrong_currency": lambda s: s["payment"].update(currency="EUR"),
            "forged_claim_message": lambda s: s["claim"]["original_message"].update(content_sha256="0" * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                context = example_context()
                mutate(context["commercial_source"])
                self.assertEqual(commercial_decision(context)["eligibility"], "NEEDS_MORE_EVIDENCE")

    def test_special_terms_route_to_human_not_automatic_denial(self):
        context = example_context()
        context["commercial_source"]["contracts"][0]["terms"]["special_terms"] = True
        result = commercial_decision(context)
        self.assertEqual((result["eligibility"], result["proposed_action"]), ("NEEDS_MORE_EVIDENCE", "HUMAN_REVIEW"))

    def test_invoice_amount_must_be_nonnegative_integer_not_boolean(self):
        for invalid in (None, True, False, -1, 100.0, "100"):
            with self.subTest(invalid=invalid):
                context = example_context()
                context["billing"]["total_amount_cents"] = invalid
                self.assertEqual(commercial_decision(context)["eligibility"], "NEEDS_MORE_EVIDENCE")

    def test_append_message_cannot_move_original_claim_time(self):
        context = example_context()
        before = commercial_decision(context)
        context["customer_message_refs"].append({"id": 8, "created_at": 1_800_000_000, "content": "补充截图"})
        context["ticket"]["received_at"] = "2099-01-01T00:00:00Z"
        after = commercial_decision(context)
        self.assertEqual((before["elapsed_seconds"], before["eligibility"]), (after["elapsed_seconds"], after["eligibility"]))

    def test_changed_lago_aggregate_after_seal_does_not_reuse_old_completeness(self):
        context = example_context()
        context["usage"]["charges_usage"][0]["units"] = "3"
        result = commercial_decision(context)
        self.assertEqual(result["eligibility"], "NEEDS_MORE_EVIDENCE")
        self.assertIsNone(result["usage_units"])

    def test_full_period_units_are_not_substituted_for_claim_subinterval(self):
        context = example_context(units="11")
        coverage = context["commercial_source"]["coverage"]
        coverage["expected_events"][0]["occurred_at"] = "2026-11-15T00:00:00Z"
        coverage["units"] = "0"
        result = commercial_decision(context)
        self.assertEqual((result["eligibility"], result["usage_units"]), ("ELIGIBLE", "0"))
        self.assertEqual(context["usage"]["charges_usage"][0]["units"], "11")

    def test_client_checks_all_scope_fields_and_never_falls_back_on_error(self):
        context = example_context()
        client = CommercialClient("http://127.0.0.1:59001", "test")
        client.http = Mock()
        client.http.request.return_value = context["commercial_source"]
        self.assertEqual(client.context(context["commercial_source"]["scope"])["source_revision"], 1)
        client.http.request.return_value = {**context["commercial_source"], "scope": {**context["commercial_source"]["scope"], "conversation_id": 999}}
        with self.assertRaisesRegex(IntegrationError, "COMMERCIAL_SOURCE_SCOPE_INVALID"):
            client.context(context["commercial_source"]["scope"])
        client.http.request.side_effect = IntegrationError("EXTERNAL_UNAVAILABLE")
        with self.assertRaisesRegex(IntegrationError, "EXTERNAL_UNAVAILABLE"):
            client.context(context["commercial_source"]["scope"])


if __name__ == "__main__":
    unittest.main()
