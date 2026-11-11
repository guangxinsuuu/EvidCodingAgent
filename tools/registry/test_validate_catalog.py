"""目录拒绝语义测试；只读Git/仓库，不import产品、不运行实验或读取private材料。"""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import validate_catalog as catalog


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {name: catalog.parse_yaml((catalog.ROOT / catalog.PACK / name).read_bytes())
                    for name in catalog.FILES}

    def altered(self):
        return deepcopy(self.docs)

    def reject(self, docs, code):
        with self.assertRaisesRegex(catalog.CatalogError, code):
            catalog.validate_documents(docs)

    def stage(self, docs, label, stage):
        scenario = next(s for s in docs["cross-team-scenarios.yaml"]["scenarios"] if s["id"] == label)
        return next(s for s in scenario["stages"] if s["id"] == stage)

    def test_real_catalog_matches_pinned_sources_and_history(self):
        result = catalog.validate()
        self.assertEqual(result["status"], "VALID_DRAFT")
        self.assertEqual(result["counts"]["stages"], 14)
        self.assertEqual(result["counts"]["historical_observed_stages"], 13)
        self.assertFalse(result["dispatch_authorized"])

    def test_duplicate_yaml_key_cannot_hide_permission_change(self):
        with self.assertRaisesRegex(catalog.CatalogError, "YAML_DUPLICATE_KEY"):
            catalog.parse_yaml(b"dispatch_enabled: false\ndispatch_enabled: true\n")

    def test_aliases_and_custom_object_tags_are_not_accepted(self):
        for value in (b"a: &x []\nb: *x\n", b"!!python/object/apply:os.system ['false']"):
            with self.subTest(value=value), self.assertRaises(catalog.CatalogError):
                catalog.parse_yaml(value)

    def test_unregistered_owner_is_rejected(self):
        docs = self.altered()
        docs["sources.yaml"]["sources"][0]["maintainer_role"] = "everyone"
        self.reject(docs, "ROLE_REFERENCE_UNKNOWN")

    def test_missing_source_and_capability_are_rejected(self):
        for field, value, code in (("source_refs", ["unknown"], "SOURCE_REFERENCE_UNKNOWN"),
                                   ("rebuild_capability_refs", ["unknown"], "CAPABILITY_REFERENCE_UNKNOWN")):
            docs = self.altered()
            docs["test-catalog.yaml"]["capabilities"][0][field] = value
            self.reject(docs, code)

    def test_source_context_and_singular_reference_are_required(self):
        docs = self.altered()
        del docs["sources.yaml"]["sources"][0]["collection"]
        self.reject(docs, "SOURCE_CONTEXT_REQUIRED")
        docs = self.altered()
        docs["sources.yaml"]["requirements"][0]["source_ref"] = "unknown"
        self.reject(docs, "SOURCE_REFERENCE_UNKNOWN")

    def test_oracle_cannot_be_rebound_to_another_case(self):
        docs = self.altered()
        self.stage(docs, "v1_history", "initial")["verification_only_expectation"]["arguments"]["label"] = "v2_boundary"
        self.reject(docs, "ORACLE_CASE_BINDING_INVALID")

    def test_historical_capability_cannot_claim_unrecorded_candidate(self):
        docs = self.altered()
        capability = next(c for c in docs["test-catalog.yaml"]["capabilities"] if c["id"] == "unit.commercial")
        capability["qualification"]["candidate_ref"] = "0" * 40
        with self.assertRaisesRegex(catalog.CatalogError, "QUALIFICATION_CANDIDATE_UNPROVEN"):
            catalog.verify_history(catalog.ROOT, docs)

    def test_duplicate_capability_is_rejected(self):
        docs = self.altered()
        docs["test-catalog.yaml"]["capabilities"].append(deepcopy(docs["test-catalog.yaml"]["capabilities"][0]))
        self.reject(docs, "DUPLICATE_ASSET_ID")

    def test_catalog_does_not_grant_dispatch(self):
        docs = self.altered()
        docs["test-catalog.yaml"]["capabilities"][0]["dispatch_enabled"] = True
        self.reject(docs, "CATALOG_CANNOT_AUTHORIZE_EXECUTION")

    def test_claimed_person_or_approval_needs_separate_future_workflow(self):
        docs = self.altered()
        docs["ownership.yaml"]["roles"][0]["principal_ref"] = "invented-reviewer"
        self.reject(docs, "UNCONFIRMED_PERSON_MUST_REMAIN_UNASSIGNED")
        docs = self.altered()
        docs["sources.yaml"]["requirements"][0]["approval_ref"] = "invented-approval"
        self.reject(docs, "DRAFT_CANNOT_ASSERT_APPROVAL")

    def test_credential_or_shell_payload_cannot_be_embedded(self):
        for field in ("password", "shell", "database_url"):
            docs = self.altered()
            docs["sources.yaml"]["sources"][0]["collection"][field] = "sentinel"
            self.reject(docs, "SECRET_OR_EXECUTABLE_PAYLOAD_FORBIDDEN")

    def test_history_cannot_be_promoted_to_current_pass(self):
        docs = self.altered()
        self.stage(docs, "v1_history", "initial")["current_evaluation_status"] = "PASS"
        self.reject(docs, "HISTORY_CANNOT_BECOME_CURRENT_RESULT")

    def test_wrong_candidate_or_job_in_history_is_rejected(self):
        for field, value, code in (("candidate_commit", "0" * 40, "HISTORY_CANDIDATE_MISMATCH"),
                                   ("job_id", "0" * 32, "HISTORY_STAGE_MISMATCH")):
            docs = self.altered()
            self.stage(docs, "v1_history", "initial")["history"][field] = value
            with self.assertRaisesRegex(catalog.CatalogError, code):
                catalog.verify_history(catalog.ROOT, docs)

    def test_missing_observation_does_not_become_schedulable_stage(self):
        docs = self.altered()
        self.stage(docs, "v1_history", "initial")["required_observations"] = []
        self.reject(docs, "STAGE_OBSERVATION_REQUIRED")

    def test_stage_cycle_and_missing_initial_are_rejected(self):
        docs = self.altered()
        self.stage(docs, "v1_history", "initial")["depends_on"] = ["v1_history.supplement"]
        self.reject(docs, "STAGE_DEPENDENCY_CYCLE")
        docs = self.altered()
        self.stage(docs, "v1_history", "supplement")["depends_on"] = []
        self.reject(docs, "INITIAL_STAGE_PREREQUISITE_REQUIRED")

    def test_refresh_before_late_event_is_rejected(self):
        docs = self.altered()
        steps = self.stage(docs, "late_usage", "after_seal")["steps"]
        left = next(i for i, s in enumerate(steps) if s["capability_ref"] == "usage.send_seal")
        right = next(i for i, s in enumerate(steps) if s["capability_ref"] == "sut.reevaluate")
        steps[left]["capability_ref"], steps[right]["capability_ref"] = steps[right]["capability_ref"], steps[left]["capability_ref"]
        self.reject(docs, "MUTATION_MUST_PRECEDE_REEVALUATION")

    def test_expected_answers_stay_out_of_source_and_sut(self):
        docs = self.altered()
        self.stage(docs, "v1_history", "initial")["verification_only_expectation"]["send_to_sut"] = True
        self.reject(docs, "ORACLE_DOMAIN_BOUNDARY_REQUIRED")

    def test_retention_cannot_be_invented_from_missing_policy(self):
        docs = self.altered()
        docs["data-flows.yaml"]["retention_policies"][0]["retention_value"] = 90
        self.reject(docs, "RETENTION_UNCONFIRMED")

    def test_source_drift_is_detected(self):
        manifest = catalog.parse_yaml((catalog.ROOT / catalog.PACK / "provenance.yaml").read_bytes())
        manifest["files"][0]["sha256"] = "0" * 64
        with patch.object(catalog, "parse_yaml", return_value=manifest):
            with self.assertRaisesRegex(catalog.CatalogError, "SOURCE_CONTENT_DRIFT"):
                catalog.verify_provenance(catalog.ROOT, catalog.PACK, self.docs)

    def test_path_escape_private_file_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actual").write_text("safe")
            (root / "link").symlink_to(root / "actual")
            for path in ("../outside", "/etc/passwd", "artifacts/evidence.json", "config/runtime.private.json", "link"):
                with self.subTest(path=path), self.assertRaises(catalog.CatalogError):
                    catalog.safe_file(root, path)


if __name__ == "__main__":
    unittest.main()
