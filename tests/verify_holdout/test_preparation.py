"""留出集资格与隔离检查；不调用模型，也不把人工答案加入发布输入。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_holdout", ROOT / "tools/verify-agent-holdout.py")
HOLDOUT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOLDOUT)


class PreparationTests(unittest.TestCase):
    """一次准备后检查真实差异、可判别性和模型/评分材料隔离，不重复评估模型。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="egap-holdout-test-")
        cls.output = Path(cls.temporary.name) / "prepared"
        cls.result = HOLDOUT.generate(cls.output)
        cls.manifest = json.loads((cls.output / "evaluation-only/manifest.json").read_text())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_qualified_witnesses_differ_and_controls_do_not(self) -> None:
        qualifications = json.loads((self.output / "evaluation-only/qualification.json").read_text())["cases"]
        self.assertEqual(len(qualifications), 2)
        for case in qualifications:
            self.assertTrue(case["not_sut_execution"])
            witness = next(row for row in case["samples"] if row["label"] == "discriminating")
            self.assertNotEqual(witness["baseline"], witness["candidate"])
            for control in (row for row in case["samples"] if row["label"] != "discriminating"):
                self.assertEqual(control["baseline"], control["candidate"])

    def test_neutral_goal_and_no_evaluation_payload_in_model_inputs(self) -> None:
        goals = []
        for case in self.manifest["cases"]:
            bundle = json.loads((self.output / case["bundle_path"]).read_text())
            goals.append(bundle["goal"])
            self.assertEqual(bundle["input_provenance"], "CALIBRATION_SYNTHETIC")
            self.assertEqual(bundle["evaluation_status"], "NOT_RUN")
            self.assertEqual(bundle["current_candidate_coverage"], [])
            serialized = json.dumps(bundle)
            for forbidden in ("reference_check", "required_semantics", "expected_baseline", "evaluation-only", "synthetic_intervention"):
                self.assertNotIn(forbidden, serialized)
        self.assertEqual(goals, [HOLDOUT.GOAL, HOLDOUT.GOAL])

    def test_bundle_diff_is_git_diff_and_matches_exact_pair(self) -> None:
        for case in self.manifest["cases"]:
            bundle = json.loads((self.output / case["bundle_path"]).read_text())
            self.assertEqual(bundle["baseline_sha"], case["baseline_sha"])
            self.assertEqual(bundle["candidate_sha"], case["candidate_sha"])
            changes = bundle["changes"]
            self.assertEqual(len(changes), 1)
            changed = changes[0]
            self.assertEqual(changed["status"], "M")
            materials = {row["material_ref"]: row for row in bundle["materials"]}
            baseline = materials[changed["baseline_material_ref"]]["content"]
            candidate = materials[changed["candidate_material_ref"]]["content"]
            difference = materials[changed["diff_material_ref"]]["content"]
            self.assertNotEqual(baseline, candidate)
            self.assertIn("@@", difference)
            self.assertEqual(hashlib.sha256(baseline.encode()).hexdigest(), case["baseline_sha256"])
            self.assertEqual(hashlib.sha256(candidate.encode()).hexdigest(), case["candidate_sha256"])

    def test_freeze_covers_model_input_and_separate_rubric(self) -> None:
        raw = (self.output / "freeze.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.result["freeze_sha256"])
        freeze = json.loads(raw)
        self.assertEqual(freeze["case_count"], 2)
        self.assertEqual(len(freeze["files"]), 6)
        for item in freeze["files"]:
            self.assertEqual(hashlib.sha256((self.output / item["path"]).read_bytes()).hexdigest(), item["sha256"])
        for case in self.manifest["cases"]:
            rubric = json.loads((self.output / case["rubric_path"]).read_text())
            self.assertIn("reference_check", rubric)
            self.assertIn("scoring", rubric)
            self.assertIn("acceptable_scenarios", rubric)

    def test_existing_frozen_directory_cannot_be_overwritten(self) -> None:
        with self.assertRaises(HOLDOUT.MaterialError):
            HOLDOUT.generate(self.output)


if __name__ == "__main__":
    unittest.main()
