"""平台目录装配行为测试；真实 YAML 作为输入，绝不导入或执行产品代码。"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from tools.registry.task_assembler import TaskAssemblyError, assemble_test_context
from tools.registry.reference_resolver import MaterialError


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "6b1d4815b4178c070f4442edfc2b5606069a3f11"
HISTORY = "reports/05-baseline/20261110-release-verification/terminated-investigation.json"
RULES = "business-source-pack/v1/policies/rules.md"


class FixtureResolver:
    """仅在测试 setup 读取现有本地合成材料，测试装配期不访问业务环境。"""

    def __init__(self, content=True):
        self.scope = SimpleNamespace(principal_id="test-local-reader", purpose="LOCAL_PLAN",
                                     allow_metadata=True, allow_content=content,
                                     model_egress_allowed=False)
        provenance = yaml.safe_load((ROOT / "registry/packs/saas-support/provenance.yaml").read_text())
        self.records = {x["path"]: x for x in provenance["files"]}
        self.bodies = {ref: (ROOT / ref).read_text() for ref in (RULES, HISTORY)}
        self.calls = []

    def read_registered_material(self, ref, start_line=None, end_line=None, purpose="LOCAL_PLAN"):
        self.calls.append((ref, purpose))
        record = self.records[ref]
        result = dict(material_ref=ref, git_commit=record["git_commit"], sha256=record["sha256"],
                      digest_algorithm="SHA256_RAW_BYTES", status="METADATA_ONLY",
                      content_verification="NOT_READ")
        if not self.scope.allow_content:
            return result
        lines = self.bodies[ref].splitlines(keepends=True)
        start = start_line or 1
        end = min(end_line or start + 79, len(lines))
        result.update(status="READABLE", content_verification="SHA256_VERIFIED",
                      content="".join(lines[start - 1:end]), start_line=start, end_line=end,
                      total_lines=len(lines), truncated=end < len(lines))
        return result


class FixtureReader:
    """隔离装配逻辑的目录夹具；Reader 的 Git/摘要能力另有集成测试。"""

    def __init__(self, content=True):
        self.catalog_commit = COMMIT
        self.validation = {"status": "VALID_DRAFT", "scenarios": 12}
        self.resolver = FixtureResolver(content)
        self.scope = self.resolver.scope
        self.index = {}
        mappings = {"ownership.yaml": {"roles": "role"},
                    "sources.yaml": {"sources": "source", "requirements": "requirement"},
                    "test-catalog.yaml": {"capabilities": "capability"},
                    "cross-team-scenarios.yaml": {"scenarios": "scenario"},
                    "data-flows.yaml": {"flows": "data_flow", "retention_policies": "retention_policy"}}
        for filename, kinds in mappings.items():
            doc = yaml.safe_load((ROOT / "registry/packs/saas-support" / filename).read_text())
            for field, kind in kinds.items():
                self.index[kind] = {row["id"]: row for row in doc[field]}
        self.index["stage"] = {stage["stage_ref"]: stage for row in self.index["scenario"].values()
                               for stage in row["stages"]}
        path = "reports/06-source-catalog/20261111-local-draft/gaps.json"
        raw = (ROOT / path).read_bytes()
        self.gap_data = {"source": {"path": path, "git_commit": COMMIT,
                                    "sha256": hashlib.sha256(raw).hexdigest()},
                         "gaps": json.loads(raw)["gaps"]}

    def get_asset(self, kind, identifier):
        return deepcopy(self.index[kind][identifier])

    def assets(self, kind):
        return deepcopy(list(self.index[kind].values()))

    def gap_records(self):
        return deepcopy(self.gap_data)

    def material_identity(self, ref):
        record = self.resolver.records[ref]
        return {"material_ref": ref, "git_commit": record["git_commit"], "sha256": record["sha256"],
                "status": "REFERENCE_ONLY", "content_projected": False}


def task(**changes):
    value = dict(task_id="terminated-investigation-plan", task_mode="KNOWN_ISSUE_INVESTIGATION",
                 goal="调查终止订阅账单争议为何在创建 Job 之前失败，并规划验证。",
                 catalog_commit=COMMIT, product_candidate_ref=None, historical_issue_ref=HISTORY,
                 operation_scope="PLAN_ONLY")
    value.update(changes)
    return value


class TaskAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.reader = FixtureReader()

    def test_real_catalog_produces_six_sections_and_only_selected_scenarios(self):
        result = assemble_test_context(self.reader, task())
        self.assertEqual(set(result), {"task_identity", "applicable_requirements", "business_context",
                                     "capabilities_and_scenarios", "evidence_inventory", "gaps_and_next_requests"})
        scenarios = result["capabilities_and_scenarios"]["scenarios"]
        self.assertEqual([x["id"] for x in scenarios], ["terminated_subscription", "v2_boundary"])
        policy = result["capabilities_and_scenarios"]["selection_policy"]
        self.assertEqual(policy["mode"], "EXPLICIT_TASK_RECIPE")
        self.assertFalse(policy["proves_other_scenarios_irrelevant"])
        self.assertFalse(policy["control_is_single_variable_experiment"])

    def test_unbound_candidate_and_historical_pass_never_become_current_coverage(self):
        result = assemble_test_context(self.reader, task())
        self.assertIsNone(result["task_identity"]["product_candidate_ref"])
        self.assertEqual(result["task_identity"]["candidate_binding_status"], "UNBOUND")
        evidence = result["evidence_inventory"]
        self.assertEqual(evidence["current_candidate_coverage"], [])
        self.assertEqual(evidence["current_evaluation_status"], "NOT_RUN")
        self.assertFalse(evidence["historical_pass_is_current_pass"])
        self.assertNotIn("root_cause_status", json.dumps(result))
        self.assertNotIn("严格费用期间不匹配后，LagoClient", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("classification", evidence["historical_issue"].get("observations", {}))

    def test_rejects_candidate_commit_head_and_model_authority_flags(self):
        for candidate in (COMMIT, "HEAD", {"commit": COMMIT}):
            with self.subTest(candidate=candidate), self.assertRaises(TaskAssemblyError) as caught:
                assemble_test_context(self.reader, task(product_candidate_ref=candidate))
            self.assertEqual(caught.exception.code, "CANDIDATE_BINDING_UNSUPPORTED")
        for extra in ({"approved": True}, {"model_egress_allowed": True}, {"scenario_ids": ["v1_history"]}):
            with self.subTest(extra=extra), self.assertRaises(TaskAssemblyError) as caught:
                assemble_test_context(self.reader, task(**extra))
            self.assertEqual(caught.exception.code, "UNSUPPORTED_TASK_FIELDS")

    def test_rejects_wrong_task_mode_scope_catalog_or_unregistered_history(self):
        for field, value in (("task_mode", "UNKNOWN_DEFECT_DISCOVERY"), ("operation_scope", "EXECUTE"),
                             ("catalog_commit", "0" * 40), ("historical_issue_ref", "/etc/passwd")):
            with self.subTest(field=field), self.assertRaises(TaskAssemblyError):
                assemble_test_context(self.reader, task(**{field: value}))

    def test_capability_closure_retains_contracts_and_is_not_callable(self):
        result = assemble_test_context(self.reader, task())["capabilities_and_scenarios"]
        capabilities = {x["id"]: x for x in result["capabilities"]}
        self.assertTrue({"native.prepare", "source.prepare", "runtime.source", "runtime.sut",
                         "sut.submit", "observe.native", "check.business"} <= set(capabilities))
        self.assertNotIn("run.release", capabilities)
        self.assertNotIn("check.boundaries", capabilities)
        for identifier, row in capabilities.items():
            source = self.reader.get_asset("capability", identifier)
            for key in ("preconditions", "side_effects", "profile_gaps", "maintainer_role"):
                self.assertEqual(row[key], source[key])
            self.assertFalse(row["callable_in_task"])
            self.assertFalse(row["dispatch_enabled"])
        self.assertEqual(result["scenarios"][0]["stages"][0]["depends_on"], [])

    def test_rules_are_versioned_pending_and_scenario_mapping_is_not_upgraded(self):
        requirements = assemble_test_context(self.reader, task())["applicable_requirements"]
        self.assertTrue({"R1", "R2", "R4"} <= {x["id"] for x in requirements})
        for requirement in requirements:
            self.assertIsNone(requirement["approval_ref"])
            self.assertEqual(requirement["review_status"], "LOCAL_CALIBRATION_PENDING_USER_REVIEW")
            self.assertEqual(requirement["material"]["content_verification"], "SHA256_VERIFIED")
            self.assertIn("## " + requirement["id"], requirement["material"]["content"])
            self.assertEqual(requirement["material"]["git_commit"],
                             self.reader.resolver.records[RULES]["git_commit"])

    def test_gap_routing_reads_original_ids_flows_and_separates_readiness(self):
        result = assemble_test_context(self.reader, task())["gaps_and_next_requests"]
        self.assertEqual({g["id"] for g in result["catalog_gaps"]},
                         {g["id"] for g in self.reader.gap_data["gaps"]})
        self.assertEqual({f["id"] for f in result["proposed_data_flows"]},
                         {f["id"] for f in self.reader.assets("data_flow") if f["collection_status"] == "PROPOSED"})
        readiness = result["readiness"]
        self.assertEqual(set(readiness), {"catalog", "materials", "rules", "capabilities", "authorization", "evidence"})
        self.assertEqual(readiness["catalog"]["status"], "VALID_DRAFT")
        self.assertTrue(readiness["authorization"]["local_plan_allowed"])
        self.assertFalse(readiness["authorization"]["execute_allowed"])
        self.assertFalse(readiness["authorization"]["model_egress_allowed"])
        for gap in result["catalog_gaps"]:
            self.assertFalse(gap["blocks_local_catalog_read"])
            self.assertTrue(gap["requested_from_roles"])
            self.assertTrue(gap["affected_actions"])
        raw = next(g for g in result["catalog_gaps"] if g["id"] == "RAW_MODEL_AND_HTTP_ARCHIVE")
        self.assertIn("历史", raw["task_implication"])

    def test_metadata_only_scope_never_copies_body_as_fallback(self):
        reader = FixtureReader(content=False)
        result = assemble_test_context(reader, task())
        for requirement in result["applicable_requirements"]:
            self.assertEqual(requirement["material"]["status"], "METADATA_ONLY")
            self.assertNotIn("content", requirement["material"])
        self.assertEqual(result["evidence_inventory"]["historical_issue"]["status"], "METADATA_ONLY")
        self.assertNotIn("observations", result["evidence_inventory"]["historical_issue"])
        self.assertEqual(result["gaps_and_next_requests"]["readiness"]["materials"]["status"], "METADATA_ONLY")

    def test_no_execution_network_or_product_module_import_and_no_reader_mutation(self):
        before = deepcopy(self.reader.index)
        with patch("subprocess.run", side_effect=AssertionError("unexpected process")), \
             patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            result = assemble_test_context(self.reader, task())
        self.assertEqual(self.reader.index, before)
        self.assertNotIn("genai_support", __import__("sys").modules)
        self.assertTrue(all(purpose == "LOCAL_PLAN" for _, purpose in self.reader.resolver.calls))
        self.assertFalse(result["gaps_and_next_requests"]["readiness"]["authorization"]["execute_allowed"])

    def test_source_fact_provenance_and_observation_time_domains_remain_separate(self):
        result = assemble_test_context(self.reader, task())
        issue = result["evidence_inventory"]["historical_issue"]
        self.assertEqual(issue["historical_candidate_ref"], "bdccf89ea431ab6ff678e754a97113b54547e5d4")
        self.assertEqual(issue["observations"]["created_sut_jobs_for_case"], 0)
        self.assertTrue(issue["observations"]["original_sut_observations"])
        self.assertTrue(issue["observations"]["followup_native_observations"])
        self.assertNotIn("current_native_observations", issue["observations"])
        self.assertEqual(issue["status"], "READABLE")
        self.assertTrue(result["business_context"]["scenario_facts"][0]["provenance"])

    def test_unreadable_materials_report_denied_missing_and_invalid_distinctly(self):
        original = self.reader.resolver.read_registered_material
        for code, status in (("MATERIAL_ACCESS_DENIED", "DENIED"), ("MATERIAL_PURPOSE_DENIED", "DENIED"),
                             ("MATERIAL_SECRET_CONTENT_FORBIDDEN", "DENIED"), ("PRIVATE_SOURCE_FORBIDDEN", "DENIED"),
                             ("SOURCE_FILE_MISSING", "MISSING"), ("MATERIAL_DIGEST_MISMATCH", "INVALID")):
            def fail_issue(ref, *args, **kwargs):
                if ref == HISTORY:
                    raise MaterialError(code)
                return original(ref, *args, **kwargs)
            with self.subTest(code=code), patch.object(self.reader.resolver, "read_registered_material", fail_issue):
                result = assemble_test_context(self.reader, task())
                self.assertEqual(result["evidence_inventory"]["historical_issue"]["status"], status)
                self.assertNotIn("observations", result["evidence_inventory"]["historical_issue"])
                self.assertEqual(result["gaps_and_next_requests"]["readiness"]["materials"]["status"], "PARTIAL_OR_UNAVAILABLE")

    def test_real_fixed_catalog_integration_and_restricted_scope(self):
        from tools.registry.catalog_reader import load_catalog, local_plan_scope
        scope = local_plan_scope(ROOT, COMMIT)
        reader = load_catalog(ROOT, COMMIT, scope)
        result = assemble_test_context(reader, task())
        self.assertEqual(result["gaps_and_next_requests"]["readiness"]["catalog"]["status"], "VALID_DRAFT")
        self.assertEqual(result["evidence_inventory"]["historical_issue"]["observations"]["created_sut_jobs_for_case"], 0)
        restricted = load_catalog(ROOT, COMMIT, replace(scope, allowed_materials=frozenset({RULES})))
        partial = assemble_test_context(restricted, task())
        self.assertEqual(partial["evidence_inventory"]["historical_issue"]["status"], "DENIED")
        self.assertEqual(partial["gaps_and_next_requests"]["readiness"]["catalog"]["status"], "VALID_DRAFT_METADATA_ONLY")
        metadata_reader = load_catalog(ROOT, COMMIT, replace(scope, allow_content=False))
        metadata = assemble_test_context(metadata_reader, task())
        self.assertEqual(metadata["evidence_inventory"]["historical_issue"]["status"], "METADATA_ONLY")
        self.assertNotIn("observations", metadata["evidence_inventory"]["historical_issue"])

    def test_unprojected_refs_retain_fixed_identity_without_content(self):
        result = assemble_test_context(self.reader, task())
        references = result["evidence_inventory"]["reference_only_materials"]
        self.assertTrue(references)
        for reference in references:
            self.assertEqual(reference["status"], "REFERENCE_ONLY")
            self.assertEqual(reference["git_commit"], self.reader.resolver.records[reference["material_ref"]]["git_commit"])
            self.assertEqual(len(reference["sha256"]), 64)
            self.assertFalse(reference["content_projected"])
            self.assertNotIn("content", reference)

    def test_truncated_bytes_and_bad_json_never_become_interpretable_evidence(self):
        original = self.reader.resolver.read_registered_material
        def truncated_issue(ref, *args, **kwargs):
            value = original(ref, *args, **kwargs)
            if ref == HISTORY:
                value["byte_truncated"] = True
            return value
        with patch.object(self.reader.resolver, "read_registered_material", truncated_issue):
            result = assemble_test_context(self.reader, task())
            self.assertEqual(result["evidence_inventory"]["historical_issue"]["status"], "LIMIT")
            self.assertEqual(result["gaps_and_next_requests"]["readiness"]["evidence"]["status"], "HISTORICAL_MATERIAL_UNAVAILABLE")
        self.reader.resolver.bodies[HISTORY] = "{invalid-json}"
        result = assemble_test_context(self.reader, task())
        self.assertEqual(result["evidence_inventory"]["historical_issue"]["status"], "INVALID")
        self.assertEqual(result["gaps_and_next_requests"]["readiness"]["evidence"]["status"], "HISTORICAL_MATERIAL_UNAVAILABLE")

    def test_missing_rule_section_does_not_claim_rule_readiness(self):
        self.reader.resolver.bodies[RULES] = self.reader.resolver.bodies[RULES].replace("## R5", "## Removed")
        result = assemble_test_context(self.reader, task())
        self.assertEqual(next(x for x in result["applicable_requirements"] if x["id"] == "R5")["material"]["status"], "SECTION_MISSING")
        self.assertEqual(result["gaps_and_next_requests"]["readiness"]["rules"]["status"], "RULE_MATERIAL_PARTIAL_OR_UNAVAILABLE")

    def test_gap_routing_lists_do_not_mutate_next_assembly(self):
        first = assemble_test_context(self.reader, task())["gaps_and_next_requests"]["catalog_gaps"][0]
        expected_roles, expected_actions = first["requested_from_roles"][:], first["affected_actions"][:]
        try:
            first["requested_from_roles"][0] = "qa_lead"
            first["affected_actions"].append("TEST_CALLER_MUTATION")
            second = assemble_test_context(self.reader, task())["gaps_and_next_requests"]["catalog_gaps"][0]
            self.assertEqual(second["requested_from_roles"], expected_roles)
            self.assertEqual(second["affected_actions"], expected_actions)
        finally:
            first["requested_from_roles"][:] = expected_roles
            first["affected_actions"][:] = expected_actions

    def test_wrong_json_shape_is_invalid_without_crashing(self):
        self.reader.resolver.bodies[HISTORY] = "[]"
        result = assemble_test_context(self.reader, task())
        self.assertEqual(result["evidence_inventory"]["historical_issue"]["status"], "INVALID")


if __name__ == "__main__":
    unittest.main()
