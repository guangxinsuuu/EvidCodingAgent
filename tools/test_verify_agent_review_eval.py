"""离线评估只见证被主图接受、且业务值仍来自模型原草案的计划。"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest

_SPEC = importlib.util.spec_from_file_location("review_eval_under_test", Path(__file__).with_name("verify-agent-review-eval.py"))
_EVAL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_EVAL)


def ledger_fixture():
    raw = {"objective": "test the registered relation", "inputs": [{"name": "registered.value", "value": "1"}],
           "control_inputs": [{"name": "registered.value", "value": "2"}], "control_relation": "DIFFERENT",
           "observation_field": "decision.value", "operator": "EQUALS", "expected_value": "1",
           "requirement_ref": "REQ-1", "diff_ref": "diff:source", "changed_fields": ["value"],
           "claim": "SHOULD_PRESERVE", "expected_baseline": "1", "expected_candidate": "1"}
    spec = {"inputs": deepcopy(raw["inputs"]), "control": {"inputs": deepcopy(raw["control_inputs"]), "expected_relation": "DIFFERENT"},
            "observations": [{"field": "decision.value"}],
            "expectations": [{"operator": "EQUALS", "value": "1", "requirement_ref": "REQ-1"}],
            "change_relation": {field: raw[field] for field in
                                ("diff_ref", "changed_fields", "claim", "requirement_ref", "expected_baseline", "expected_candidate")}}
    compiled = {"payload": {"objective": raw["objective"], "check_spec": spec}}
    proposal = {"schema_version": 3, "action_kind": "PLAN_DRAFT", "payload": {"draft": raw}}
    draft = {"ref": "plan:draft-op", "digest": "digest", "status": "BOUND_PLAN", "proposal": proposal, "compiled": compiled}
    original = {"id": "draft-op", "proposal": proposal, "status": "COMPLETED", "result": {"status": "BOUND_PLAN",
                "draft_digest": "digest", "compiled_check": deepcopy(compiled["payload"])}}
    accepted = {"id": "accept-op", "proposal": {"schema_version": 3, "action_kind": "ACCEPT_PLAN",
                "payload": {"draft_ref": "plan:draft-op", "draft_digest": "digest"}}, "status": "COMPLETED",
                "result": {"status": "PLAN_ACCEPTED_FOR_REVIEW", "draft_ref": "plan:draft-op", "draft_digest": "digest",
                           "compiled_check": deepcopy(compiled["payload"])}}
    return {"run_id": "run-id", "run": {"id": "run-id", "bundle": {"frozen": True}, "state": {
                "status": "PLANNED", "stop_reason": "SCOPED_PLAN_ACCEPTED",
                "review_cycle": {"draft": draft, "accepted_plan_ref": draft["ref"]}}},
            "operations": [original, accepted], "calls": []}


class ReviewEvaluationIdentityTests(unittest.TestCase):
    def test_bound_but_not_accepted_draft_never_becomes_effective_plan(self):
        ledger = ledger_fixture()
        ledger["operations"] = ledger["operations"][:1]
        compiled, counts = _EVAL._accepted_check(ledger, {"frozen": True})
        self.assertIsNone(compiled)
        self.assertEqual(counts["accepted_plans"], 0)

    def test_matching_accepted_plan_retains_exact_compiled_payload(self):
        ledger = ledger_fixture()
        compiled, counts = _EVAL._accepted_check(ledger, {"frozen": True})
        self.assertEqual(compiled, ledger["run"]["state"]["review_cycle"]["draft"]["compiled"])
        self.assertEqual(counts["accepted_operation_id"], "accept-op")

    def test_other_task_or_draft_digest_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "task identity"):
            _EVAL._accepted_check(ledger_fixture(), {"frozen": False})
        ledger = ledger_fixture()
        ledger["operations"][1]["result"]["draft_digest"] = "different"
        with self.assertRaisesRegex(ValueError, "draft binding"):
            _EVAL._accepted_check(ledger, {"frozen": True})

    def test_consistent_compiled_rewrite_cannot_replace_original_model_input(self):
        ledger = ledger_fixture()
        ledger["run"]["state"]["review_cycle"]["draft"]["compiled"]["payload"]["check_spec"]["inputs"][0]["value"] = "999"
        for operation in ledger["operations"]:
            operation["result"]["compiled_check"]["check_spec"]["inputs"][0]["value"] = "999"
        with self.assertRaisesRegex(ValueError, "business values differ"):
            _EVAL._accepted_check(ledger, {"frozen": True})

    def test_ambiguous_acceptances_are_not_best_of_selected(self):
        ledger = ledger_fixture()
        ledger["operations"].append(deepcopy(ledger["operations"][1]))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            _EVAL._accepted_check(ledger, {"frozen": True})


class ReviewEvaluationSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _EVAL.LocalGitReader(_EVAL.ROOT).read(_EVAL._H.SOURCE_COMMIT, _EVAL._H.SOURCE_PATH).decode()
        cls.contract = json.loads((_EVAL.ROOT / _EVAL._H.CONTRACT_PATH).read_text())

    def test_default_initial_suite_retains_prior_cases(self):
        default = _EVAL.suite_definitions(self.source)
        self.assertEqual(default, _EVAL.definitions(self.source))
        self.assertEqual([row["case_id"] for row in default], ["review_a", "review_b"])
        self.assertEqual(sum(len(row["rows"]) for row in
            _EVAL.qualify_suite(self.source, default, self.contract, "initial")), 6)

    def test_unknown_suite_is_not_silently_replaced(self):
        with self.assertRaisesRegex(ValueError, "unknown evaluation suite"):
            _EVAL.suite_definitions(self.source, "unknown")

    def test_followup_is_disjoint_and_reference_conditions_qualify(self):
        old = _EVAL.suite_definitions(self.source, "initial")
        new = _EVAL.suite_definitions(self.source, "followup")
        self.assertEqual([row["case_id"] for row in new], ["review_c", "review_d"])
        self.assertFalse({row["candidate"] for row in old} & {row["candidate"] for row in new})
        qualifications = _EVAL.qualify_suite(self.source, new, self.contract, "followup")
        self.assertEqual(sum(len(row["rows"]) for row in qualifications), 4)
        self.assertTrue(all(row["expected_matches"] for case in qualifications for row in case["rows"]))

    def test_witness_requires_own_relation_and_distinct_control(self):
        for suite in ("initial", "followup"):
            definitions = _EVAL.suite_definitions(self.source, suite)
            qualifications = _EVAL.qualify_suite(self.source, definitions, self.contract, suite)
            for definition, qualification in zip(definitions, qualifications):
                with self.subTest(case_id=definition["case_id"]):
                    contexts = {label: _EVAL._H._override(self.contract["fixture"]["context"],
                        definition["input_field"], definition[label], self.contract) for label in ("primary", "control")}
                    rows = {row["label"]: row for row in qualification["rows"]}
                    outputs = {version: {label: rows[label][version] for label in contexts}
                               for version in ("baseline", "candidate")}
                    self.assertTrue(_EVAL.relation_witness(definition, contexts, outputs))
                    contexts["control"] = deepcopy(contexts["primary"])
                    # 即便结果文字被填成参考答案，没有模型自己的有效对照也不能通过。
                    self.assertFalse(_EVAL.relation_witness(definition, contexts, outputs))


if __name__ == "__main__":
    unittest.main()
