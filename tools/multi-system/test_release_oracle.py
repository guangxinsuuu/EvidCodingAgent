"""验证侧校准器自测：有意义的错误观察不能变成PASS，模型词汇探针不替代语义Oracle。

所有输入均为本单元测试内构造，不调用产品代码、商业来源、模型或数据库。
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import unittest

from release_oracle import OracleInputError, assess_case, expectation_for, expectation_manifest, wording_probes


def example():
    case = {"label": "v2_boundary", "tenant_id": "tenant-a", "customer_id": "customer-a",
        "subscription_id": "subscription-1", "invoice_id": "invoice-1", "account_id": 1,
        "conversation_id": 2, "metric_code": "api_units", "period_start": "2026-11-10T00:00:00Z",
        "claimed_at": "2026-11-17T00:00:00Z"}
    original = {"id": 5, "content": "请核对退款条件", "created_at": 1794355200}
    scope = {key: case[key] for key in ("tenant_id", "customer_id", "subscription_id", "invoice_id", "account_id", "conversation_id")}
    usage = {"from_datetime": case["period_start"], "to_datetime": "2026-12-09T23:59:59Z", "lago_invoice_id": None,
        "charges_usage": [{"billable_metric": {"code": "api_units"}, "units": "0"},
                          {"billable_metric": {"code": "unrelated_metric"}, "units": "999"}]}
    claim = {"claim_id": "claim-1", "claimed_at": case["claimed_at"], "original_message": {
        "message_id": 5, "created_at": datetime.fromtimestamp(original["created_at"], timezone.utc).isoformat(),
        "content_sha256": hashlib.sha256(original["content"].encode()).hexdigest()}}
    coverage = {"coverage_id": "coverage-1", "status": "SEALED", "sealed_at": case["claimed_at"],
        "completeness_scope": "LOCAL_CONTROLLED_GENERATOR_ONLY", "metric_code": "api_units", "unit": "units",
        "pending_count": 0, "expected_events": [], "sent_event_ids": [], "received_event_ids": [],
        "period_start": case["period_start"], "period_end": case["claimed_at"], "units": "0"}
    source = {"scope": scope, "authority_scope": "LOCAL_LAB", "source_kind": "SYNTHETIC_BUSINESS_SOURCE",
        "policies": [], "contracts": [],
        "claim": claim, "coverage": coverage, "payment": {"payment_id": "payment-1", "source_kind": "SIMULATED_PAYMENT",
            "authority_scope": "LOCAL_LAB", "status": "PAID", "amount_minor": 12000, "currency": "USD"},
        "events": [{"event_type": "SUBSCRIPTION_RENEWAL_EFFECTIVE", "term_number": 2, "invoice_refs": ["invoice-1"]}]}
    context = {"commercial_source": source, "missing_evidence": [], "billing": {"invoice_id": "invoice-1"},
        "subscription": {"external_subscription_id": "subscription-1"}, "usage": deepcopy(usage),
        "policy": {"version": "lab-v2", "terms": {"window_seconds": 604800}}}
    result = {"job": {"job_id": "job1", "status": "DONE", "draft": {"eligibility": "ELIGIBLE", "proposed_action": "HUMAN_REVIEW",
        "applicable_policy_ref": "refund-standard:lab-v2", "customer_reply_draft": "仅依据本地实验规则，需要人工审核。"}},
        "delivery": {"delivery_state": "COMPLETE", "external_message_id": 6}}
    observations = {"usage": deepcopy(usage), "invoice": {"lago_id": "invoice-1", "customer": {"external_id": "customer-a"},
        "subscriptions": [{"external_id": "subscription-1"}], "total_amount_cents": 12000, "currency": "USD",
        "fees": [{"external_subscription_id": "subscription-1", "item": {"type": "subscription"},
                  "from_date": "2026-11-10T00:00:00Z", "to_date": "2026-12-09T23:59:59Z"}]},
        "original_message": original, "conversation": {"status": "open"}, "messages": [original | {"message_type": 0, "private": False},
            {"id": 6, "private": True, "message_type": 1, "conversation_id": 2, "content": "[EGAP draft job1] 本地实验，仅依据模拟资料。"}]}
    return case, result, context, observations


def failed_names(verdict):
    return {item["name"] for item in verdict["checks"] if item["status"] == "FAIL"}


class ReleaseOracleTests(unittest.TestCase):
    def test_exact_new_policy_endpoint_passes_without_using_other_metric(self):
        before = example()
        after = deepcopy(before)
        verdict = assess_case(*after)
        self.assertEqual(verdict["status"], "PASS")
        self.assertEqual(after, before, "检查器不能修改产品证据或原生观察")
        self.assertEqual(verdict["semantic_quality_status"], "NOT_FULLY_EVALUATED")

    def test_wrong_product_answer_is_rejected_even_when_job_done(self):
        args = example()
        args[1]["job"]["draft"].update(eligibility="INELIGIBLE", proposed_action="NO_ACTION")
        verdict = assess_case(*args)
        self.assertEqual(verdict["status"], "FAIL")
        self.assertTrue({"eligibility", "proposed_action"}.issubset(failed_names(verdict)))

    def test_native_invoice_scope_is_not_trusted_from_product_snapshot(self):
        args = example()
        args[3]["invoice"]["customer"]["external_id"] = "other-customer"
        self.assertIn("native_invoice_ownership", failed_names(assess_case(*args)))

    def test_same_source_id_with_changed_fact_is_not_accepted(self):
        args = example()
        args[3]["source_context"] = deepcopy(args[2]["commercial_source"])
        args[2]["commercial_source"]["payment"]["amount_minor"] = 1
        self.assertIn("independent_source_payment_content", failed_names(assess_case(*args)))

    def test_missing_native_metric_is_not_zero(self):
        args = example()
        args[3]["usage"]["charges_usage"] = []
        self.assertIn("native_selected_metric_units", failed_names(assess_case(*args)))

    def test_duplicate_or_public_note_fails_even_with_complete_delivery(self):
        for modification in ("duplicate", "public", "wrong_conversation"):
            args = example()
            if modification == "duplicate":
                args[3]["messages"].append(deepcopy(args[3]["messages"][-1]))
            elif modification == "public":
                args[3]["messages"][-1]["private"] = False
            else:
                args[3]["messages"][-1]["conversation_id"] = 999
            with self.subTest(modification=modification):
                self.assertIn("exact_private_note_delivery", failed_names(assess_case(*args)))

    def test_zero_with_pending_predeclared_event_requires_more_evidence(self):
        args = example()
        args[0].update(label="late_usage", claimed_at="2026-11-12T00:00:00Z")
        args[1]["job"]["draft"].update(eligibility="NEEDS_MORE_EVIDENCE", proposed_action="REQUEST_INFORMATION")
        args[2]["missing_evidence"] = ["USAGE_COMPLETENESS"]
        source = args[2]["commercial_source"]
        source["claim"]["claimed_at"] = args[0]["claimed_at"]
        source["coverage"].update(status="OPEN", sealed_at=None, pending_count=1,
            period_end=args[0]["claimed_at"], expected_events=[{"event_id": "late-1", "units": "1", "occurred_at": "2026-11-11T00:00:00Z"}])
        self.assertEqual(assess_case(*args)["status"], "PASS")
        args[1]["job"]["draft"].update(eligibility="ELIGIBLE")
        self.assertIn("eligibility", failed_names(assess_case(*args)))

    def test_after_claim_event_not_counted_as_claim_interval_use(self):
        args = example()
        args[0].update(label="post_claim_usage", claimed_at="2026-11-12T00:00:00Z")
        args[2]["commercial_source"]["claim"]["claimed_at"] = args[0]["claimed_at"]
        args[2]["commercial_source"]["coverage"].update(period_end=args[0]["claimed_at"],
            expected_events=[{"event_id": "after-1", "units": "1", "occurred_at": "2026-11-13T00:00:00Z"}],
            sent_event_ids=["after-1"], received_event_ids=["after-1"])
        args[2]["usage"]["charges_usage"][0]["units"] = "1"
        args[3]["usage"]["charges_usage"][0]["units"] = "1"
        self.assertEqual(assess_case(*args)["status"], "PASS")
        args[2]["commercial_source"]["coverage"]["units"] = "1"
        self.assertIn("independent_business_subinterval_sum", failed_names(assess_case(*args)))

    def test_no_model_keyword_match_is_not_a_semantic_pass(self):
        self.assertEqual(wording_probes("仅依据本地实验规则，需要人工审核。", "ELIGIBLE"), [])
        verdict = assess_case(*example())
        self.assertEqual(verdict["semantic_quality_status"], "NOT_FULLY_EVALUATED")
        self.assertEqual(verdict["qualification"], "LOCAL_INDEPENDENT_RULE_CHECK")

    def test_wording_hint_with_missing_facts_is_only_review_finding(self):
        findings = wording_probes("您似乎符合退款标准。", "NEEDS_MORE_EVIDENCE")
        self.assertIn("ELIGIBILITY_HINT_WITH_MISSING_EVIDENCE", {row["code"] for row in findings})
        self.assertTrue(all(row["status"] == "REVIEW_REQUIRED" for row in findings))

    def test_missing_observation_and_unknown_stage_do_not_pass(self):
        args = example()
        args[3].pop("invoice")
        with self.assertRaisesRegex(OracleInputError, "COMPLETE_CASE"):
            assess_case(*args)
        with self.assertRaisesRegex(OracleInputError, "UNKNOWN_SCENARIO_OR_STAGE"):
            expectation_for("v2_boundary", "future")
        with self.assertRaisesRegex(OracleInputError, "STAGE_NOT_APPLICABLE"):
            expectation_for("v2_boundary", "after_seal")

    def test_bad_candidate_timestamp_is_fail_not_missing_native_observation(self):
        args = example()
        args[2]["commercial_source"]["claim"]["claimed_at"] = "not-a-time"
        verdict = assess_case(*args)
        self.assertEqual(verdict["status"], "FAIL")
        self.assertIn("candidate_protocol_and_business_value_shape", failed_names(verdict))

    def test_missing_candidate_draft_is_fail_with_complete_observations(self):
        args = example()
        args[1]["job"].pop("draft")
        self.assertEqual(assess_case(*args)["status"], "FAIL")

    def test_all_twelve_objects_and_late_stage_have_frozen_expected_outcomes(self):
        manifest = expectation_manifest()
        self.assertEqual(len(manifest["scenarios"]), 12)
        self.assertEqual(manifest["scenarios"]["v1_history"]["window_seconds"], 1209600)
        self.assertEqual(manifest["scenarios"]["v2_late"]["elapsed_seconds"], 604801)
        self.assertEqual(manifest["stages"]["late_usage:after_seal"]["eligibility"], "INELIGIBLE")


if __name__ == "__main__":
    unittest.main()
