"""受信adapter/既有产品函数的接口合规测试，不是当前候选的独立业务验证。"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/verify-capabilities"))
sys.path.insert(0, str(ROOT / "services/genai-support-sut/src"))

import commercial_fixture as adapter
from development_fixture import development_inputs

# 导入的源码固定为已审本机版本；不允许payload选择模块或候选目录。
adapter.load_contract()
from genai_support.integrations.commercial import commercial_decision


def payload(*inputs):
    return {"capability_ref": adapter.CAPABILITY, "scenario_ref": adapter.SCENARIO,
            "stage_ref": adapter.STAGE, "inputs": [{"name": name, "value": value} for name, value in inputs]}


class FixtureContractTests(unittest.TestCase):
    def test_fixture_identity_and_real_rules_are_fixed_separately_from_candidate(self):
        contract = adapter.load_contract()
        self.assertEqual(contract["fixture"]["sha256"], sha256(adapter.canonical_bytes(contract["fixture"]["context"])).hexdigest())
        self.assertEqual(contract["source_sha256"], sha256((ROOT / adapter.SOURCE_PATH).read_bytes()).hexdigest())
        self.assertEqual(contract["fixture_provenance"]["source_commit"], "0f46c9b0602334db2b8955c018ed076c6c059c37")
        policies = contract["fixture"]["context"]["commercial_source"]["policies"]
        self.assertEqual(policies[0]["effective_to"], "2026-11-10T00:00:00Z")
        self.assertEqual(policies[1]["effective_from"], "2026-11-10T00:00:00Z")
        self.assertNotIn("expected", contract)
        self.assertNotIn("expected_eligibility", contract["fixture"]["context"])

    def test_prepare_never_executes_product_or_marks_live_stage_ready(self):
        with patch("socket.socket", side_effect=AssertionError("NO_NETWORK")), \
                patch("genai_support.integrations.commercial.commercial_decision", side_effect=AssertionError("NO_PRODUCT_EXECUTION")):
            prepared = adapter.prepare_probe(payload(("commercial_source.payment.amount_minor", 99)))
        self.assertFalse(prepared["dispatch_qualified"])
        self.assertEqual(prepared["execution_mode"], "PURE_FIXTURE")
        self.assertEqual(prepared["prerequisite_status"], "NOT_CHECKED")
        self.assertTrue(all(item["status"] == "REQUIRED_AT_EXECUTION" for item in prepared["stage_prerequisites"]))
        self.assertEqual(prepared["context"]["commercial_source"]["payment"]["amount_minor"], 99)

    def test_primary_control_copies_and_caller_values_cannot_mutate_frozen_fixture(self):
        p = payload(("commercial_source.payment.amount_minor", 99))
        primary = adapter.prepare_probe(p)
        p["inputs"][0]["value"] = 7
        primary["context"]["commercial_source"]["payment"]["amount_minor"] = 8
        primary["applied_inputs"][0]["value"] = 9
        control = adapter.prepare_probe(payload())
        self.assertEqual(control["context"]["commercial_source"]["payment"]["amount_minor"], 100)
        self.assertEqual(control["context_sha256"], control["fixture_sha256"])

    def test_unknown_callable_file_url_path_and_live_stage_fail_closed(self):
        for bad in [{**payload(), "entrypoint": "os.system"}, {**payload(), "file": "/tmp/model"},
                    {**payload(), "url": "http://127.0.0.1"}, {**payload(), "stage_ref": "late_usage.after_seal"},
                    payload(("commercial_source.__class__", "x")), payload(("commercial_source.payment.expected_eligibility", "ELIGIBLE"))]:
            with self.subTest(bad=bad), self.assertRaises(adapter.FixtureBindingError):
                adapter.prepare_probe(bad)

    def test_input_types_ranges_duplicate_names_and_dates_are_strict(self):
        for name, value in [("commercial_source.payment.amount_minor", True), ("commercial_source.payment.amount_minor", "100"),
                            ("commercial_source.payment.amount_minor", -1), ("commercial_source.payment.amount_minor", 1000000001),
                            ("commercial_source.contracts.0.terms.special_terms", "false"), ("commercial_source.payment.currency", "JPY"),
                            ("commercial_source.payment.paid_at", "2026-11-09"), ("commercial_source.payment.paid_at", "2026-02-30T00:00:00Z")]:
            with self.subTest(name=name, value=value), self.assertRaises(adapter.FixtureBindingError):
                adapter.prepare_probe(payload((name, value)))
        with self.assertRaisesRegex(adapter.FixtureBindingError, "DUPLICATE_INPUT"):
            adapter.prepare_probe(payload(("commercial_source.payment.amount_minor", 100), ("commercial_source.payment.amount_minor", 99)))

    def test_fixture_digest_and_entrypoint_drift_are_rejected(self):
        for change in (lambda c: c["fixture"]["context"]["billing"].update(currency="EUR"),
                       lambda c: c["entrypoint"].update(symbol="evaluate"),
                       lambda c: c.update(source_sha256="a" * 64)):
            contract = adapter.load_contract()
            change(contract)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "contract.json"
                path.write_text(json.dumps(contract), encoding="utf-8")
                with patch.object(adapter, "CONTRACT_PATH", path), self.assertRaises(adapter.FixtureBindingError):
                    adapter.load_contract()

    def test_every_registered_scalar_path_is_compatible_with_real_function_output_types(self):
        """调用受信既有纯函数只检接口合规；不验证新候选、自然语言或跨组状态流程。"""
        values = [101, "EUR", "different-customer", "different-subscription", "different-tenant", 1,
                  "2026-11-19T00:00:01Z", "DRAFT", "2026-11-20T00:00:00Z", "2026-11-10T00:00:00Z", True,
                  "OPEN", 1, False, "UNPAID", 99, "EUR", "different-customer", "2026-11-20T00:00:00Z", "TERMINATION"]
        definitions = adapter.load_contract()["inputs"]
        self.assertEqual(len(values), len(definitions))
        for field, value in zip(definitions, values):
            with self.subTest(field=field["name"]), patch("socket.socket", side_effect=AssertionError("NO_NETWORK")):
                context = adapter.prepare_probe(payload((field["name"], value)))["context"]
                before = deepcopy(context)
                receipt = adapter.validate_result(commercial_decision(context))
                self.assertEqual(context, before)
                self.assertEqual(receipt["schema_conformance"], "VALID")
                self.assertEqual(receipt["business_verdict"], "NOT_ESTABLISHED")
                self.assertFalse(receipt["cross_system_coverage"])
                self.assertFalse(receipt["release_authorized"])

    def test_base_fixture_has_complete_relations_for_a_meaningful_probe(self):
        # 先排除装配器自己制造的缺失关系，否则所有反事实都只会落到补证分支。
        # 这是受信基线fixture的自检，不是当前新候选或跨系统Oracle的验收结果。
        with patch("socket.socket", side_effect=AssertionError("NO_NETWORK")):
            result = commercial_decision(adapter.prepare_probe(payload())["context"])
        self.assertEqual(result["missing_evidence"], [])
        self.assertEqual(result["authority_scope"], "LOCAL_LAB")
        self.assertEqual(adapter.validate_result(result)["business_verdict"], "NOT_ESTABLISHED")

    def test_product_output_validator_rejects_mistyped_or_added_authority_fields(self):
        original = commercial_decision(adapter.prepare_probe(payload())["context"])
        for key, value in [("eligibility", "PASS"), ("within_window", "true"), ("elapsed_seconds", True),
                           ("elapsed_seconds", float("inf")), ("reason_codes", "LOCAL_LAB"), ("release_authorized", True)]:
            with self.subTest(key=key), self.assertRaises(adapter.FixtureBindingError):
                adapter.validate_result({**original, key: value})

    def test_public_development_generator_provides_inputs_without_answers_or_execution(self):
        with patch("socket.socket", side_effect=AssertionError("NO_NETWORK")):
            example = development_inputs()
        self.assertEqual(example["evaluation_status"], "NOT_RUN")
        self.assertFalse(example["expected_results_provided"])
        self.assertNotEqual(example["primary"]["context_sha256"], example["control"]["context_sha256"])


if __name__ == "__main__":
    unittest.main()
