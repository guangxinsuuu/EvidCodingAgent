"""观察与断言的失败边界；只用内存假客户端，不连接业务服务或模型。"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/multi-system"))
from late_usage_observer import LateUsageObserver, assess_observation, assess_transition, observe_submission, summarize_execution
from test_release_oracle import example


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def fixture(label="late_usage", sealed=False):
    """仅在单测内构造证据；两案例除事件在申请前后外共享规范初态。"""
    case, result, context, observations = example()
    case.update(label=label, claimed_at="2026-11-12T00:00:00Z", customer_message_id=5,
                experiment_id="experiment-1", candidate_sha="a" * 40)
    event = {"event_id": "usage-1", "units": "1", "occurred_at": "2026-11-11T00:00:00Z" if label == "late_usage"
             else "2026-11-13T00:00:00Z", "native_occurred_at": "2026-11-11T00:00:00Z"}
    source = context["commercial_source"]
    source["claim"]["claimed_at"] = case["claimed_at"]
    coverage = source["coverage"]
    coverage.update(period_end=case["claimed_at"], expected_events=[event], status="OPEN", pending_count=1,
                    sealed_at=None, units="1" if label == "late_usage" else "0", revision=1,
                    native_expected_units="1", send_receipts={}, receive_receipts={}, reconciliation=None)
    context["missing_evidence"] = ["USAGE_COMPLETENESS"]
    result["job"]["draft"].update(eligibility="NEEDS_MORE_EVIDENCE", proposed_action="REQUEST_INFORMATION")
    if sealed:
        result["job"]["job_id"] = "job2"
        result["job"]["draft"].update(eligibility="INELIGIBLE" if label == "late_usage" else "ELIGIBLE",
                                      proposed_action="NO_ACTION" if label == "late_usage" else "HUMAN_REVIEW")
        observations["messages"][-1]["content"] = "[EGAP draft job2] 本地实验，仅依据模拟资料。"
        context["missing_evidence"] = []
        for raw in (context["usage"], observations["usage"]):
            raw["charges_usage"][0]["units"] = "1"
        send = {"service": "lago", "method": "POST", "path": "/api/v1/events", "status": 201,
                "transaction_id": event["event_id"], "native_event": {
                    "transaction_id": event["event_id"], "code": case["metric_code"],
                    "external_subscription_id": case["subscription_id"], "properties": {"units": "1"},
                    "timestamp": int(datetime.fromisoformat(event["native_occurred_at"].replace("Z", "+00:00")).timestamp())}}
        receive = {"service": "lago", "method": "GET", "status": 200, "transaction_id": event["event_id"],
                   "bounded_event_set": [event["event_id"]], "query": {"external_subscription_id": case["subscription_id"]}}
        raw = deepcopy(observations["usage"])
        coverage.update(status="SEALED", pending_count=0, sealed_at="2026-11-19T12:00:00Z", revision=5,
                        sent_event_ids=[event["event_id"]], received_event_ids=[event["event_id"]],
                        send_receipts={event["event_id"]: send}, receive_receipts={event["event_id"]: receive},
                        reconciliation={"matched": True, "raw_response_sha256": digest(raw), "raw_response": raw,
                                        "receipt": deepcopy(receive), "ledger_revision": 3, "lago_units": "1"})
    observations["source_context"] = deepcopy(source)
    package = {"result": result, "context": context, "observations": observations, "protocol_status": "VALID",
               "scope": source["scope"], "experiment_id": case["experiment_id"], "candidate_sha": case["candidate_sha"],
               "accepted_event": {"parent_job_id": "job1" if sealed else None,
                                  "assessment_facts_sha256": "sealed-facts" if sealed else "open-facts"},
               "gaps": ["ACTUAL_MODEL_REQUEST_NOT_CAPTURED", "RAW_MODEL_OUTPUT_NOT_CAPTURED", "DETERMINISTIC_DECISION_NOT_CAPTURED"]}
    return case, package


def failed(verdict):
    return {row["name"] for row in verdict["checks"] if row["status"] == "FAIL"}


def observer_fixture(package):
    sut, cw, lago, source = Mock(), Mock(), Mock(), Mock()
    sut.request.return_value = deepcopy(package["result"])
    native = package["observations"]
    cw.request.side_effect = lambda method, path: {"payload": deepcopy(native["messages"])} if path.endswith("/messages") else deepcopy(native["conversation"])
    lago.request.side_effect = lambda method, path: {"invoice": deepcopy(native["invoice"])} if "/invoices/" in path else {"customer_usage": deepcopy(native["usage"])}
    source.request.return_value = deepcopy(native["source_context"])
    audit = Mock(side_effect=lambda query, params: deepcopy(package["context"] if "external_inputs" in query else package["accepted_event"]))
    observer = LateUsageObserver(sut, cw, lago, source, audit, cw_prefix="/api/v1/accounts/1")
    return observer, (sut, cw, lago, source), audit


class ReadOnlyObserverTests(unittest.TestCase):
    def test_submission_503_without_job_is_retained_without_existence_inference(self):
        receipt = {"status": 503, "response": {"error": "SOURCE_UNAVAILABLE"}, "request_sent": True}
        observed = observe_submission(receipt)
        self.assertEqual(observed["state"], "HTTP_ERROR_WITHOUT_JOB_ID")
        self.assertIsNone(observed["job_id"])
        self.assertEqual(observed["receipt"], receipt)
        self.assertEqual(observed["business_verdict"], "NOT_EVALUATED")

    def test_success_without_job_is_protocol_error_and_transport_unknown_remains_unknown(self):
        self.assertEqual(observe_submission({"status": 202, "response": {}})["state"], "ACCEPTANCE_PROTOCOL_INVALID")
        self.assertEqual(observe_submission({"outcome": "UNKNOWN"})["state"], "UNKNOWN")

    def test_constructor_has_no_io_and_observation_only_uses_get_and_scoped_sql(self):
        case, package = fixture()
        observer, clients, audit = observer_fixture(package)
        for client in clients:
            client.request.assert_not_called()
        audit.assert_not_called()
        actual = observer.observe(case, "job1")
        self.assertEqual(actual["context"], package["context"])
        self.assertEqual(actual["protocol_status"], "VALID")
        for client in clients:
            self.assertTrue(all(call.args[0] == "GET" for call in client.request.call_args_list))
        self.assertTrue(all(call.args[1] == (case["tenant_id"], "job1") for call in audit.call_args_list))
        self.assertIsNone(actual["decision"])
        self.assertIsNone(actual["model_request"])
        self.assertIsNone(actual["model_output"])
        self.assertIn("ACTUAL_MODEL_REQUEST_NOT_CAPTURED", actual["gaps"])

    def test_nonterminal_timeout_keeps_same_job_and_does_not_submit(self):
        case, package = fixture()
        package["result"].update(job={"job_id": "job1", "status": "PROCESSING", "draft": None}, delivery=None)
        observer, clients, _ = observer_fixture(package)
        actual = observer.wait(case, "job1", timeout=0)
        self.assertEqual(actual["wait_status"], "WAITING")
        self.assertEqual(assess_observation(case, actual)["status"], "WAITING")
        self.assertEqual(clients[0].request.call_count, 1)

    def test_poll_persists_full_observation_first_and_stops_on_persistence_failure(self):
        case, package = fixture()
        package["result"].update(job={"job_id": "job1", "status": "PROCESSING", "draft": None}, delivery=None)
        observer, clients, _ = observer_fixture(package)
        persisted = []

        def save_then_fail(observation):
            persisted.append(deepcopy(observation))
            raise OSError("evidence storage unavailable")

        with self.assertRaisesRegex(OSError, "evidence storage unavailable"):
            observer.wait(case, "job1", timeout=30, poll_interval=0.001, on_observation=save_then_fail)
        self.assertEqual(clients[0].request.call_count, 1)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["context"], package["context"])
        self.assertEqual(set(persisted[0]["observations"]), set(package["observations"]))
        self.assertEqual(persisted[0]["observations"]["source_context"], package["observations"]["source_context"])
        self.assertEqual(persisted[0]["observations"]["original_message"]["id"], case["customer_message_id"])
        self.assertNotIn("poll_history", persisted[0])
        self.assertTrue(all(call.args[0] == "GET" for client in clients for call in client.request.call_args_list))

    def test_failed_job_and_malformed_job_are_observed_not_lost(self):
        for result, expected in (({"job": {"job_id": "job1", "status": "FAILED"}, "delivery": None}, "VALID"),
                                 ({"job": {"job_id": "other", "status": "DONE"}}, "INVALID"),
                                 ({"job": {"job_id": "job1", "status": "QUEUED"}, "delivery": {"delivery_state": "COMPLETE"}}, "INVALID")):
            case, package = fixture()
            package["result"] = result
            observer, _, _ = observer_fixture(package)
            actual = observer.observe(case, "job1")
            self.assertEqual(actual["result"], result)
            self.assertEqual(actual["protocol_status"], expected)
            self.assertEqual(assess_observation(case, actual)["status"], "FAIL")

    def test_one_unavailable_source_does_not_discard_others_or_leak_exception(self):
        case, package = fixture()
        observer, clients, _ = observer_fixture(package)
        clients[2].request.side_effect = RuntimeError("secret-dsn-must-not-leak")
        actual = observer.observe(case, "job1")
        self.assertIsNotNone(actual["observations"]["source_context"])
        self.assertIsNotNone(actual["observations"]["messages"])
        self.assertNotIn("secret-dsn", json.dumps(actual))
        self.assertEqual(assess_observation(case, actual)["status"], "INCOMPLETE")

    def test_wrong_job_trace_cannot_fill_model_evidence_gaps(self):
        case, package = fixture()
        observer, _, _ = observer_fixture(package)
        observer.model_trace_reader = Mock(return_value={"job_id": "other", "model_request": {"input": "x"}})
        actual = observer.observe(case, "job1")
        self.assertIsNone(actual["model_request"])
        self.assertIn("MODEL_TRACE_JOB_ID_MISMATCH", actual["gaps"])


class IndependentAssertionTests(unittest.TestCase):
    def test_both_open_cases_need_more_evidence_and_are_not_semantic_pass(self):
        for label in ("late_usage", "post_claim_usage"):
            case, package = fixture(label)
            before = deepcopy(package)
            verdict = assess_observation(case, package)
            self.assertEqual(verdict["status"], "PASS_SCOPED_CHECKS", verdict)
            self.assertEqual(verdict["semantic_quality_status"], "NOT_FULLY_EVALUATED")
            self.assertEqual(package, before)
            package["result"]["job"]["draft"]["eligibility"] = "ELIGIBLE"
            self.assertIn("eligibility", failed(assess_observation(case, package)))

    def test_equal_native_units_produce_different_before_after_claim_outcomes(self):
        results = []
        for label in ("late_usage", "post_claim_usage"):
            case, package = fixture(label, sealed=True)
            verdict = assess_observation(case, package, stage="after_seal")
            self.assertEqual(verdict["status"], "PASS_SCOPED_CHECKS", verdict)
            results.append(package["result"]["job"]["draft"]["eligibility"])
        self.assertEqual(results, ["INELIGIBLE", "ELIGIBLE"])

    def test_sealed_label_without_receipts_or_reconciliation_never_passes(self):
        for field in ("send_receipts", "receive_receipts", "reconciliation"):
            case, package = fixture(sealed=True)
            package["context"]["commercial_source"]["coverage"][field] = None
            package["observations"]["source_context"]["coverage"][field] = None
            self.assertEqual(assess_observation(case, package, stage="after_seal")["status"], "FAIL")

    def test_wrong_metric_in_receipt_and_other_metric_zero_substitution_fail(self):
        case, package = fixture(sealed=True)
        package["observations"]["source_context"]["coverage"]["send_receipts"]["usage-1"]["native_event"]["code"] = "unrelated_metric"
        self.assertIn("native_event_receipt_matches_predeclaration:usage-1", failed(assess_observation(case, package, stage="after_seal")))
        case, package = fixture()
        package["observations"]["usage"]["charges_usage"] = [{"billable_metric": {"code": "unrelated_metric"}, "units": "0"}]
        self.assertIn("native_selected_metric_units", failed(assess_observation(case, package)))

    def test_wrong_delivery_conversation_and_duplicate_notes_fail(self):
        case, package = fixture()
        package["observations"]["messages"][-1]["conversation_id"] = 999
        self.assertIn("exact_private_note_delivery", failed(assess_observation(case, package)))

    def test_predeclared_post_claim_event_cannot_be_changed_to_pre_claim(self):
        case, package = fixture("post_claim_usage")
        package["observations"]["source_context"]["coverage"]["expected_events"][0]["occurred_at"] = "2026-11-11T00:00:00Z"
        self.assertIn("predeclared_event_correct_side_of_claim", failed(assess_observation(case, package)))

    def test_wording_probe_is_review_only_and_no_hit_is_never_full_pass(self):
        case, package = fixture(sealed=True)
        package["result"]["job"]["draft"]["customer_reply_draft"] = "仅依据本地实验规则，您超出退款窗口。"
        verdict = assess_observation(case, package, stage="after_seal")
        self.assertEqual(verdict["status"], "PASS_SCOPED_CHECKS")
        self.assertIn("POSSIBLE_FALSE_WINDOW_REASON", {row["code"] for row in verdict["findings"]})
        self.assertTrue(all(row["status"] == "REVIEW_REQUIRED" for row in verdict["findings"]))
        self.assertEqual(verdict["semantic_quality_status"], "NOT_FULLY_EVALUATED")

    def test_captured_actual_decision_reason_is_checked_without_reconstructing_it(self):
        case, package = fixture(sealed=True)
        package["decision"] = {"eligibility": "INELIGIBLE", "proposed_action": "NO_ACTION", "usage_units": "1",
                               "within_window": True, "reason_codes": ["REFUND_WINDOW_EXCEEDED"]}
        self.assertIn("captured_decision_reason_matches_facts", failed(assess_observation(case, package, stage="after_seal")))

    def test_transition_requires_parent_retry_original_claim_and_old_snapshot(self):
        _, before = fixture()
        _, after = fixture(sealed=True)
        old_after = deepcopy(before)
        receipt = {"status": 202, "response": {"job_id": "job2", "parent_job_id": "job1", "assessment_status": "REEVALUATED"}}
        retries = [{"status": 202, "response": {"job_id": "job1", "reused": True}}]
        self.assertEqual(assess_transition(before, after, old_after, retries, receipt)["status"], "PASS_SCOPED_CHECKS")
        retries[0]["response"]["job_id"] = "other"
        after["accepted_event"]["parent_job_id"] = "other"
        after["context"]["commercial_source"]["claim"]["claimed_at"] = "2026-11-13T00:00:00Z"
        old_after["context"]["commercial_source"]["coverage"]["status"] = "SEALED"
        names = failed(assess_transition(before, after, old_after, retries, receipt))
        self.assertTrue({"ordinary_retries_reuse_original_job", "reevaluation_new_job_and_parent", "original_claim_identity_and_time_immutable", "old_snapshot_immutable"}.issubset(names))

    def test_transition_cannot_pass_without_retries_or_independent_accepted_event(self):
        _, before = fixture()
        _, after = fixture(sealed=True)
        after["accepted_event"] = None
        verdict = assess_transition(before, after, deepcopy(before), [], {"job_id": "job2", "parent_job_id": "job1", "assessment_status": "REEVALUATED"})
        self.assertEqual(verdict["status"], "FAIL")
        self.assertIn("ordinary_retries_reuse_original_job", failed(verdict))

    def test_transition_does_not_accept_error_receipt_or_absent_candidate_identity(self):
        _, before = fixture()
        _, after = fixture(sealed=True)
        old_after = deepcopy(before)
        for package in (before, after, old_after):
            package.pop("candidate_sha")
        verdict = assess_transition(before, after, old_after,
            [{"status": 503, "response": {"job_id": "job1", "reused": True}}],
            {"status": 202, "response": {"job_id": "job2", "parent_job_id": "job1", "assessment_status": "REEVALUATED"}})
        self.assertTrue({"ordinary_retries_reuse_original_job", "same_experiment_candidate_and_scope"}.issubset(failed(verdict)))


class ExecutionSummaryTests(unittest.TestCase):
    def complete_cases(self):
        return [{"label": label, "status": "OBSERVED", **{key: {"status": "PASS_SCOPED_CHECKS", "checks": [], "findings": []}
                 for key in ("initial_verdict", "final_verdict", "transition")}}
                for label in ("late_usage", "post_claim_usage")]

    def test_complete_primary_control_and_six_passes_are_required(self):
        cases = self.complete_cases()
        self.assertEqual(summarize_execution(cases)["scoped_result"], "PASS_SCOPED_DETERMINISTIC_CHECKS")
        cases[1]["label"] = "late_usage"
        self.assertEqual(summarize_execution(cases)["scoped_result"], "INCOMPLETE")
        cases = self.complete_cases()
        cases[0].pop("transition")
        self.assertEqual(summarize_execution(cases)["scoped_result"], "INCOMPLETE")

    def test_failure_in_exception_verdict_survives_incomplete_other_case(self):
        cases = [{"label": "late_usage", "status": "J1_INCOMPLETE", "verdict": {
            "status": "FAIL", "checks": [{"name": "execution_failed", "status": "FAIL", "evidence": {}}]}},
            {"label": "post_claim_usage", "status": "ADMISSION_UNRESOLVED"}]
        summary = summarize_execution(cases)
        self.assertEqual(summary["scoped_result"], "FAIL")
        self.assertEqual(summary["failed_checks"][0]["name"], "execution_failed")
        self.assertEqual(len(summary["incomplete_cases"]), 2)

    def test_initial_and_final_wording_findings_are_both_retained_without_mutation(self):
        cases = self.complete_cases()
        for key, code in (("initial_verdict", "INITIAL_WORDING"), ("final_verdict", "FINAL_WORDING")):
            cases[0][key]["findings"] = [{"code": code, "status": "REVIEW_REQUIRED"}]
        original = deepcopy(cases)
        summary = summarize_execution(cases)
        self.assertEqual({row["code"] for row in summary["findings"]}, {"INITIAL_WORDING", "FINAL_WORDING"})
        self.assertEqual({row["stage"] for row in summary["findings"]}, {"initial", "after_seal"})
        self.assertEqual(summary["scoped_result"], "PASS_SCOPED_DETERMINISTIC_CHECKS")
        self.assertEqual(cases, original)

    def test_failed_check_cannot_hide_inside_incomplete_or_inconsistent_verdict(self):
        cases = self.complete_cases()
        cases[0]["transition"].update(status="INCOMPLETE", checks=[{"name": "old_snapshot_immutable", "status": "FAIL"}])
        summary = summarize_execution(cases)
        self.assertEqual(summary["scoped_result"], "FAIL")
        self.assertEqual(len(summary["incomplete_cases"]), 1)
        cases[0]["transition"] = {"status": "FAIL", "checks": []}
        summary = summarize_execution(cases)
        self.assertEqual(summary["scoped_result"], "FAIL")
        self.assertEqual(summary["failed_checks"][0]["name"], "observed_verdict_failure")


if __name__ == "__main__":
    unittest.main()
