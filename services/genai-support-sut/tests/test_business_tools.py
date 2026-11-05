"""实际业务工具行为校准；独立期望参考不进入BusinessTools的运行输入。"""

import json
from copy import deepcopy
import unittest

from genai_support.business_tools import BusinessToolError, BusinessTools, evaluate_refund_policy, scripted_draft
from genai_support.config import DEFAULT_FIXTURE_PATH
from genai_support.fixtures import load_fixture_bundle


class BusinessToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = load_fixture_bundle(DEFAULT_FIXTURE_PATH)
        self.events = []
        self.tools = BusinessTools(self.bundle, self.events.append)

    def test_renewal_date_selects_old_policy_and_missing_evidence_stays_missing(self) -> None:
        context = self.tools.collect("tenant-a", "CS-1042", 3)
        self.assertEqual(context["policy"]["policy_id"], "POLICY-OCT-14D")
        self.assertIsNone(context["usage"])
        self.assertIsNone(context["contract"])
        self.assertEqual(self.events[-1]["arguments"]["renewed_at"], "2026-10-25T00:00:00Z")
        self.assertEqual(len(self.events), 6)
        self.assertFalse(self.events[3]["found"])
        self.assertEqual(scripted_draft(context)["eligibility"], "NEEDS_MORE_EVIDENCE")

    def test_same_ticket_id_cannot_mix_tenants_or_billing(self) -> None:
        a = self.tools.collect("tenant-a", "CS-1043", 1)
        b = self.tools.collect("tenant-b", "CS-1043", 1)
        self.assertEqual(a["billing"]["billing_record_id"], "BILL-a1043")
        self.assertEqual(b["billing"]["billing_record_id"], "BILL-b1043")
        self.assertNotEqual(scripted_draft(a)["eligibility"], scripted_draft(b)["eligibility"])
        with self.assertRaises(BusinessToolError):
            self.tools.collect("tenant-b", "CS-1042", 3)

    def test_scripted_calibration_matches_nine_explicit_reference_cases(self) -> None:
        reference = DEFAULT_FIXTURE_PATH.parents[1] / "verification/synthetic-v1.json"
        for case in json.loads(reference.read_text())["cases"]:
            with self.subTest(case=case["case_id"]):
                draft = scripted_draft(self.tools.collect(case["tenant_id"], case["ticket_id"], case["ticket_revision"]))
                self.assertEqual(draft["applicable_policy_ref"], case["expected_policy_ref"])
                self.assertEqual(draft["eligibility"], case["expected_eligibility"])
                self.assertIn(draft["proposed_action"], case["allowed_proposed_actions"])

    def test_missing_tool_receipt_stops_before_completing_context(self) -> None:
        def unavailable(_event: dict) -> None:
            raise RuntimeError("journal-unavailable")
        with self.assertRaises(RuntimeError):
            BusinessTools(self.bundle, unavailable).collect("tenant-a", "CS-1043", 1)

    def test_policy_window_and_usage_limit_are_read_from_policy_data(self) -> None:
        context = self.tools.collect("tenant-a", "CS-1043", 1)
        self.assertEqual(evaluate_refund_policy(context)["eligibility"], "ELIGIBLE")
        shorter = deepcopy(context)
        shorter["ticket"]["ticket_id"] = "ARBITRARY-UNSEEN-TICKET"
        shorter["policy"]["refund_window_days"] = 2
        self.assertEqual(evaluate_refund_policy(shorter)["eligibility"], "INELIGIBLE")
        self.assertEqual(evaluate_refund_policy(shorter)["window_seconds"], 2 * 86_400)
        context["usage"]["units"] = 3
        context["policy"]["eligibility_conditions"]["maximum_usage_units"] = 2
        self.assertIn("USAGE_LIMIT_EXCEEDED", evaluate_refund_policy(context)["reason_codes"])
        context["policy"]["eligibility_conditions"]["maximum_usage_units"] = 3
        self.assertEqual(evaluate_refund_policy(context)["eligibility"], "ELIGIBLE")

    def test_exact_elapsed_seconds_and_inclusive_policy_boundary(self) -> None:
        context = self.tools.collect("tenant-a", "CS-1045", 1)
        decision = self.tools.evaluate_refund_policy(context)
        self.assertEqual(decision["elapsed_seconds"], 604800)
        self.assertTrue(decision["within_window"])
        self.assertEqual(self.events[-1]["tool_name"], "evaluate_refund_policy")
        after = self.tools.collect("tenant-a", "CS-1046", 1)
        decision = evaluate_refund_policy(after)
        self.assertEqual(decision["elapsed_seconds"], 604801)
        self.assertFalse(decision["within_window"])
        self.assertIn("OUTSIDE_REFUND_WINDOW", decision["reason_codes"])
        context["policy"]["window_semantics"]["end_inclusive"] = False
        self.assertFalse(evaluate_refund_policy(context)["within_window"])

    def test_missing_facts_remain_unknown_and_references_are_actual_records(self) -> None:
        context = self.tools.collect("tenant-a", "CS-1042", 3)
        decision = evaluate_refund_policy(context)
        self.assertIsNone(decision["usage_units"])
        self.assertIsNone(decision["usage_within_limit"])
        self.assertIsNone(decision["contract_terms_match"])
        self.assertEqual(set(decision["missing_evidence"]), {"USAGE_RECORD", "CONTRACT_RECORD"})
        self.assertEqual(set(decision["evidence_refs"]), {"BILL-a1042", "POLICY-OCT-14D"})


if __name__ == "__main__":
    unittest.main()
