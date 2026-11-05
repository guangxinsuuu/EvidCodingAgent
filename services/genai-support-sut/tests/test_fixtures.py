"""业务事实包的边界测试；只验证数据装载，不将参考答案冒充业务 Oracle。"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from genai_support.fixtures import FIXTURE_SHA256, FixtureError, load_fixture_bundle


REPOSITORY = Path(__file__).resolve().parents[3]
FIXTURES = REPOSITORY / "datasets/genai-support/synthetic-v1/sut"
REFERENCE = REPOSITORY / "datasets/genai-support/verification/synthetic-v1.json"


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "sut"
        shutil.copytree(FIXTURES, self.directory)

    def rewrite_facts(self, transform):
        """重算副本摘要以走到语义校验层；默认生产入口仍使用 pinned digest。"""
        facts = json.loads((self.directory / "facts.json").read_text())
        transform(facts)
        raw = (json.dumps(facts, ensure_ascii=False, indent=2) + "\n").encode()
        (self.directory / "facts.json").write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.directory / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["facts_sha256"] = digest
        path.write_text(json.dumps(manifest))
        return digest

    def assert_rejected(self, code, digest=FIXTURE_SHA256):
        with self.assertRaises(FixtureError) as caught:
            load_fixture_bundle(self.directory, expected_sha256=digest)
        self.assertEqual(caught.exception.code, code)

    def test_repeat_load_is_stable_and_deeply_immutable(self):
        first = load_fixture_bundle(self.directory)
        second = load_fixture_bundle(self.directory)
        self.assertEqual(first, second)
        self.assertEqual(first.content_sha256, FIXTURE_SHA256)
        self.assertEqual(first.clock, "2026-11-10T12:00:00Z")
        self.assertEqual(len(first.facts["tickets"]), 9)
        with self.assertRaises(TypeError):
            first.facts["tickets"][0]["revision"] = 999
        with self.assertRaises(TypeError):
            first.facts["policies"][0]["window_semantics"]["end_inclusive"] = False
        self.assertIsNot(first.facts, second.facts)

    def test_same_ticket_id_is_scoped_by_authenticated_tenant(self):
        bundle = load_fixture_bundle(self.directory)
        a = bundle.get_ticket("tenant-a", "CS-1043", 1)
        b = bundle.get_ticket("tenant-b", "CS-1043", 1)
        self.assertEqual(a["billing_record_ref"], "BILL-a1043")
        self.assertEqual(b["billing_record_ref"], "BILL-b1043")
        self.assertIsNone(bundle.get_record("billing_records", "tenant-a", "BILL-b1043"))
        self.assertIsNone(bundle.get_ticket("tenant-b", "CS-1042", 3))
        self.assertIsNone(bundle.get_ticket("tenant-a", "CS-1043", True))
        self.assertIsNone(bundle.get_ticket("tenant-a", "CS-1043", 2))

    def test_missing_evidence_is_absent_not_fabricated_empty_fact(self):
        bundle = load_fixture_bundle(self.directory)
        ticket = bundle.get_ticket("tenant-a", "CS-1042", 3)
        subscription = bundle.get_record("subscriptions", "tenant-a", ticket["subscription_ref"])
        self.assertIsNone(subscription["contract_ref"])
        self.assertFalse(any(row["billing_record_ref"] == ticket["billing_record_ref"]
                             for row in bundle.facts["usage_records"]))

    def test_window_samples_cover_exact_start_end_and_one_second_after(self):
        bundle = load_fixture_bundle(self.directory)
        def elapsed(ticket_id):
            ticket = bundle.get_ticket("tenant-a", ticket_id, 1)
            billing = bundle.get_record("billing_records", "tenant-a", ticket["billing_record_ref"])
            return (datetime.fromisoformat(ticket["received_at"].replace("Z", "+00:00"))
                    - datetime.fromisoformat(billing["renewed_at"].replace("Z", "+00:00"))).total_seconds()
        self.assertEqual(elapsed("CS-1049"), 0)
        self.assertEqual(elapsed("CS-1045"), 7 * 86400)
        self.assertEqual(elapsed("CS-1046"), 7 * 86400 + 1)
        self.assertEqual(elapsed("CS-1047"), 7 * 86400 + 1)
        self.assertEqual(bundle.facts["policies"][0]["effective_to_exclusive"],
                         bundle.facts["policies"][1]["effective_from"])

    def test_public_reference_is_separate_and_binds_same_facts(self):
        reference = json.loads(REFERENCE.read_text())
        self.assertEqual(reference["facts_sha256"], FIXTURE_SHA256)
        self.assertEqual(reference["visibility"], "PUBLIC_CALIBRATION_REFERENCE_NOT_SUT_INPUT")
        self.assertEqual(reference["approval_state"], "DRAFT")
        self.assertEqual(len(reference["cases"]), 9)
        facts = json.loads((self.directory / "facts.json").read_text())
        self.assertNotIn("expected_eligibility", json.dumps(facts))
        self.assertNotIn("expected_policy_ref", json.dumps(facts))
        # 相邻验证数据即便不存在/变化，SUT 事实加载仍只访问自己的两个固定文件。
        (self.directory.parent / "verification").mkdir()
        (self.directory.parent / "verification/answers.json").write_text("not JSON")
        self.assertEqual(load_fixture_bundle(self.directory).content_sha256, FIXTURE_SHA256)

    def test_changed_facts_and_changed_manifest_cannot_bypass_default_pin(self):
        self.rewrite_facts(lambda facts: facts["tickets"][0].update(body="改写业务输入"))
        self.assert_rejected("FIXTURE_DIGEST_MISMATCH")

    def test_changed_raw_bytes_are_rejected_even_when_json_equivalent(self):
        path = self.directory / "facts.json"
        path.write_bytes(path.read_bytes() + b"\n")
        self.assert_rejected("FIXTURE_DIGEST_MISMATCH")

    def test_cross_tenant_reference_is_rejected_after_rehash(self):
        digest = self.rewrite_facts(lambda facts: facts["tickets"][1].update(billing_record_ref="BILL-b1043"))
        self.assert_rejected("FIXTURE_REFERENCE_INVALID", digest)

    def test_wrong_subscription_association_is_rejected_after_rehash(self):
        digest = self.rewrite_facts(lambda facts: facts["tickets"][1].update(billing_record_ref="BILL-a1044"))
        self.assert_rejected("FIXTURE_REFERENCE_INVALID", digest)

    def test_duplicate_record_is_rejected(self):
        digest = self.rewrite_facts(lambda facts: facts["tickets"].append(deepcopy(facts["tickets"][0])))
        self.assert_rejected("FIXTURE_DUPLICATE_RECORD", digest)

    def test_boolean_revision_is_not_an_integer(self):
        digest = self.rewrite_facts(lambda facts: facts["tickets"][0].update(revision=True))
        self.assert_rejected("FIXTURE_SCHEMA_INVALID", digest)

    def test_unknown_fields_cannot_inject_expected_answers(self):
        digest = self.rewrite_facts(lambda facts: facts["tickets"][0].update(expected_eligibility="ELIGIBLE"))
        self.assert_rejected("FIXTURE_SCHEMA_INVALID", digest)

    def test_ticket_before_renewal_is_rejected(self):
        digest = self.rewrite_facts(lambda facts: facts["tickets"][0].update(received_at="2026-10-24T00:00:00Z"))
        self.assert_rejected("FIXTURE_TIME_INVALID", digest)

    def test_provided_usage_must_cover_ticket_received_time(self):
        digest = self.rewrite_facts(lambda facts: facts["usage_records"][0].update(observed_through="2026-11-01T00:00:00Z"))
        self.assert_rejected("FIXTURE_TIME_INVALID", digest)

    def test_overlapping_policy_versions_are_rejected(self):
        digest = self.rewrite_facts(lambda facts: facts["policies"][1].update(effective_from="2026-10-31T00:00:00Z"))
        self.assert_rejected("FIXTURE_POLICY_OVERLAP", digest)

    def test_duplicate_json_keys_are_rejected_before_use(self):
        path = self.directory / "manifest.json"
        path.write_text(path.read_text().replace('"schema_version": 1,', '"schema_version": 1, "schema_version": 1,'))
        self.assert_rejected("FIXTURE_DUPLICATE_JSON_KEY")

    def test_deeply_nested_json_has_a_stable_safe_error(self):
        (self.directory / "manifest.json").write_text('{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}')
        self.assert_rejected("FIXTURE_JSON_INVALID")

    def test_decoder_recursion_failure_is_sanitized(self):
        with patch("genai_support.fixtures.json.loads", side_effect=RecursionError("decoder internal details")):
            self.assert_rejected("FIXTURE_JSON_INVALID")

    def test_verification_file_inside_sut_directory_is_rejected(self):
        (self.directory / "answers.json").write_text("{}")
        self.assert_rejected("FIXTURE_LAYOUT_INVALID")

    def test_manifest_cannot_declare_owner_approval(self):
        path = self.directory / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["provenance"]["business_policy_approval"] = "owner-approved"
        path.write_text(json.dumps(manifest))
        self.assert_rejected("FIXTURE_SCHEMA_INVALID")

    def test_fixture_files_must_not_be_symlinks(self):
        path = self.directory / "facts.json"
        path.unlink()
        path.symlink_to(FIXTURES / "facts.json")
        self.assert_rejected("FIXTURE_PATH_INVALID")

    def test_bounded_file_read_rejects_oversize_input(self):
        (self.directory / "facts.json").write_bytes(b" " * 1_048_577)
        self.assert_rejected("FIXTURE_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
