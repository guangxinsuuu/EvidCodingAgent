"""独立留出准备与离线评价自身的边界检查；无模型、Graph 或网络调用。"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("binding_holdout", ROOT / "tools/verify-agent-binding-holdout.py")
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)


class BindingHoldoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="egap-binding-holdout-")
        cls.prepared = Path(cls.temporary.name) / "prepared"
        cls.result = H.prepare(cls.prepared)
        cls.manifest = json.loads((cls.prepared / "evaluation-only/manifest.json").read_text())
        cls.contract = json.loads((cls.prepared / "evaluation-only/authority-contract.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def reference_compiled(self, case):
        """只在评价器单测构造参考编译对象；模型运行不会获得这个函数或其输入。"""
        rubric = json.loads((self.prepared / case["rubric_path"]).read_text())
        contexts = {label: H._override(self.contract["fixture"]["context"], rubric["input_field"], rubric[label], self.contract)
                    for label in ("primary", "control")}
        return {"contract_sha256": H.digest(self.contract), "fixture_sha256": self.contract["fixture"]["sha256"],
                "capability_ref": self.contract["capability_ref"], "stage_ref": self.contract["stage_ref"],
                **{label + "_context": ctx for label, ctx in contexts.items()},
                **{label + "_context_sha256": H.digest(ctx) for label, ctx in contexts.items()},
                "assertions": [{"observation_ref": "out", "field": "decision.eligibility", "operator": "EQUALS",
                                "value": rubric["expected_primary"][0], "requirement_ref": rubric["requirement"]}],
                "control_relation": {"observation_ref": "out", "expected_relation": "DIFFERENT"},
                "change_relation": {"claim": rubric["claim"], "requirement_ref": rubric["requirement"],
                                    "expected_baseline": {"out": rubric["expected_primary"][0]},
                                    "expected_candidate": {"out": rubric["expected_primary"][1]}}}

    def test_complete_source_and_real_git_diff(self):
        source = H.LocalGitReader(ROOT).read(H.SOURCE_COMMIT, H.SOURCE_PATH)
        for case in self.manifest["cases"]:
            bundle = json.loads((self.prepared / case["bundle_path"]).read_text())
            self.assertEqual(len(bundle["changes"]), 1)
            materials = {m["material_ref"]: m for m in bundle["materials"]}
            change = bundle["changes"][0]
            self.assertEqual(materials[change["baseline_material_ref"]]["content"].encode(), source)
            self.assertIn("@@", materials[change["diff_material_ref"]]["content"])
            repo = H.LocalGitReader(self.prepared / case["repository"])
            for path in H.SUPPORT_PATHS:
                self.assertEqual(repo.read(case["baseline_sha"], path), repo.read(case["candidate_sha"], path))

    def test_two_broken_relations_and_one_preserved(self):
        rubrics = [json.loads((self.prepared / c["rubric_path"]).read_text()) for c in self.manifest["cases"]]
        self.assertEqual([r["relation_kind"] for r in rubrics].count("BROKEN"), 2)
        self.assertEqual([r["relation_kind"] for r in rubrics].count("PRESERVED"), 1)
        self.assertGreaterEqual(len({r["requirement"] for r in rubrics}), 2)
        qualifications = json.loads((self.prepared / "evaluation-only/qualification.json").read_text())
        self.assertEqual(len(qualifications["cases"]), 3)
        self.assertTrue(all(row["expected_matches"] for case in qualifications["cases"] for row in case["rows"]))

    def test_frozen_inputs_keep_answers_separate(self):
        for case in self.manifest["cases"]:
            bundle = json.loads((self.prepared / case["bundle_path"]).read_text())
            self.assertEqual(bundle["goal"], H.GOAL)
            self.assertEqual(bundle["input_provenance"], "CALIBRATION_SYNTHETIC")
            self.assertEqual(bundle["planning_contracts"], [self.contract])
            raw = json.dumps(bundle)
            for forbidden in ("reference_check", "critical_missing", "expected_primary", "witness_rule", "relation_kind", "evaluation-only"):
                self.assertNotIn(forbidden, raw)
        frozen = json.loads((self.prepared / "freeze.json").read_text())
        for item in frozen["files"]:
            self.assertEqual(hashlib.sha256((self.prepared / item["path"]).read_bytes()).hexdigest(), item["sha256"])

    def test_reference_plans_witness_relations_without_changing_fixture(self):
        before = deepcopy(self.contract["fixture"]["context"])
        for case in self.manifest["cases"]:
            plan = self.reference_compiled(case)
            untouched = deepcopy(plan)
            evaluated = H.evaluate_compiled(self.prepared, case["case_id"], plan)
            self.assertTrue(evaluated["sixth_hard_gate"])
            self.assertTrue(evaluated["predictions_match"])
            self.assertTrue(evaluated["contexts_used_without_golden_completion"])
            self.assertEqual(plan, untouched)
        self.assertEqual(self.contract["fixture"]["context"], before)

    def test_unbound_plan_is_not_run_and_no_reference_completion(self):
        result = H.evaluate_compiled(self.prepared, self.manifest["cases"][0]["case_id"], None)
        self.assertEqual(result["status"], "NOT_RUN")
        self.assertFalse(result["relation_witness"])

    def test_wrong_model_prediction_and_irrelevant_relation_fail(self):
        case = self.manifest["cases"][0]
        plan = self.reference_compiled(case)
        plan["change_relation"]["expected_candidate"] = {"out": "INELIGIBLE"}
        result = H.evaluate_compiled(self.prepared, case["case_id"], plan)
        self.assertFalse(result["predictions_match"])
        self.assertFalse(result["sixth_hard_gate"])
        plan = self.reference_compiled(case)
        plan["change_relation"]["requirement_ref"] = "R5"
        self.assertFalse(H.evaluate_compiled(self.prepared, case["case_id"], plan)["sixth_hard_gate"])

    def test_evaluator_does_not_substitute_missing_critical_input(self):
        case = self.manifest["cases"][0]
        plan = self.reference_compiled(case)
        for label in ("primary", "control"):
            plan[label + "_context"] = deepcopy(self.contract["fixture"]["context"])
            plan[label + "_context_sha256"] = H.digest(plan[label + "_context"])
        result = H.evaluate_compiled(self.prepared, case["case_id"], plan)
        self.assertFalse(result["relation_witness"])
        self.assertFalse(result["sixth_hard_gate"])

    def test_identity_and_evidence_overwrite_are_rejected(self):
        case = self.manifest["cases"][0]
        plan = self.reference_compiled(case)
        plan["primary_context_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "context digest"):
            H.evaluate_compiled(self.prepared, case["case_id"], plan)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            H.prepare(self.prepared)

    def test_json_numbers_share_one_type_but_boolean_is_distinct(self):
        self.assertTrue(H._assertion(864000.0, "EQUALS", 864000))
        self.assertFalse(H._assertion(864000.0, "NOT_EQUALS", 864000))
        self.assertFalse(H._assertion(True, "EQUALS", 1))
        self.assertFalse(H._assertion(100, "EQUALS", "100"))


if __name__ == "__main__":
    unittest.main()
