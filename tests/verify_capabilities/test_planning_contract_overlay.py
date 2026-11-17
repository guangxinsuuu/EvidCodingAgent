"""能力目录叠加是受信本地规划导航，不改写冻结YAML或赋予真实阶段资格。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from tools.registry.release_assembler import _planning_contract_overlay

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/verify-capabilities"))
from development_release import generate


class PlanningOverlayTests(unittest.TestCase):
    def test_overlay_keeps_old_capabilities_unbound_and_does_not_import_history(self):
        original = {"capabilities": [{"id": "unit.commercial", "dispatch_enabled": False}],
                    "scenarios": [{"id": "late_usage", "stages": [{"stage_ref": "late_usage.initial", "depends_on": []}]}]}
        catalog = deepcopy(original)
        contracts = _planning_contract_overlay(catalog)
        self.assertEqual(catalog["capabilities"][0], original["capabilities"][0])
        self.assertEqual(catalog["scenarios"][0], original["scenarios"][0])
        self.assertEqual(contracts[0]["parent_capability_ref"], "unit.sut_decision")
        self.assertEqual(catalog["capabilities"][-1]["catalog_origin"], "LOCAL_TRUSTED_BINDING_CONTRACT_OVERLAY")
        self.assertFalse(catalog["capabilities"][-1]["dispatch_enabled"])
        self.assertFalse(catalog["scenarios"][-1]["live_stage_prerequisites_satisfied"])
        self.assertEqual(catalog["scenarios"][-1]["execution_mode"], "PURE_FIXTURE")
        self.assertNotIn("history", catalog["scenarios"][-1])
        self.assertNotIn("expected_eligibility", contracts[0]["fixture"]["context"])

    def test_public_development_pair_contains_full_baseline_identity_not_minimal_excerpt(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = generate(Path(directory) / "development")
            bundle = json.loads(Path(manifest["bundle_path"]).read_text(encoding="utf-8"))
            contract = bundle["planning_contracts"][0]
            baseline = next(item for item in bundle["materials"] if item["kind"] == "BASELINE_SOURCE"
                            and item["path"] == contract["entrypoint"]["path"])
            self.assertEqual(baseline["sha256"], contract["source_sha256"])
            self.assertIn("def commercial_decision(context: dict)", baseline["content"])
            self.assertEqual(bundle["evaluation_status"], "NOT_RUN")
            self.assertFalse(manifest["candidate_executed"])
            self.assertNotIn("intervention", bundle)
            self.assertEqual(bundle["current_candidate_coverage"], [])
