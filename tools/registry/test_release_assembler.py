"""发布装配边界：真实临时 Git 对象、固定目录，不接模型或被测业务服务。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.registry.reference_resolver import LocalGitReader, MaterialError
from tools.registry.release_assembler import ROOT, assemble_release, main


PRODUCT_PATH = "services/genai-support-sut/src/genai_support/business_tools.py"


class ReleaseAssemblerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Synthetic calibration")
        self.git("config", "user.email", "calibration@example.invalid")
        self.write(PRODUCT_PATH, "def model_reply_context(decision):\n    return dict(decision)\n")
        self.baseline = self.commit()
        self.write(PRODUCT_PATH, "def model_reply_context(decision):\n    return {'eligibility': decision['eligibility']}\n")
        self.candidate = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                                       cwd=self.root, stderr=subprocess.PIPE, text=True).strip()

    def write(self, path, content):
        location = self.root / path
        location.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            location.write_bytes(content)
        else:
            location.write_text(content, encoding="utf-8")

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "Explicit synthetic test version")
        return self.git("rev-parse", "HEAD")

    def assemble(self, **kwargs):
        arguments = {"root": ROOT, "baseline_sha": self.baseline, "candidate_sha": self.candidate,
                     "goal": "检查回复投影变更是否保留业务事实", "requirement_refs": ["R3", "R5"],
                     "candidate_root": self.root, "input_provenance": "CALIBRATION_SYNTHETIC"}
        arguments.update(kwargs)
        return assemble_release(**arguments)

    def test_real_commit_diff_and_exact_bytes_ignore_worktree(self):
        self.write(PRODUCT_PATH, "uncommitted local edits must never enter the task")
        bundle = self.assemble()
        self.assertEqual(bundle["candidate_sha"], self.candidate)
        self.assertEqual(bundle["baseline_sha"], self.baseline)
        self.assertEqual(bundle["input_provenance"], "CALIBRATION_SYNTHETIC")
        change = bundle["changes"][0]
        self.assertEqual(change["status"], "M")
        materials = {row["material_ref"]: row for row in bundle["materials"]}
        self.assertIn("return dict(decision)", materials[change["baseline_material_ref"]]["content"])
        self.assertIn("decision['eligibility']", materials[change["candidate_material_ref"]]["content"])
        self.assertIn("-    return dict(decision)", materials[change["diff_material_ref"]]["content"])
        self.assertNotIn("uncommitted", json.dumps(bundle))
        for material in bundle["materials"]:
            self.assertEqual(material["sha256"], hashlib.sha256(material["content"].encode()).hexdigest())

    def test_catalog_projection_neither_leaks_history_nor_grants_execution(self):
        bundle = self.assemble()
        encoded = json.dumps(bundle["catalog"])
        for forbidden in ("known_finding_codes", "root_cause", "verification_only_expectation", "historical_reports", "qualification", "findings.json", "terminated-investigation", "current_usage不适用"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(bundle["current_candidate_coverage"], [])
        self.assertEqual(bundle["evaluation_status"], "NOT_RUN")
        self.assertFalse(bundle["catalog"]["dispatch_authorized"])
        self.assertTrue(all(not row["dispatch_enabled"] for row in bundle["catalog"]["capabilities"]))
        self.assertEqual(len(bundle["catalog"]["capabilities"]), 19)
        self.assertEqual(len(bundle["catalog"]["scenarios"]), 12)
        self.assertTrue(bundle["catalog"]["roles"])
        self.assertTrue(bundle["catalog"]["flows"])

    def test_historical_material_bodies_are_not_read_at_all(self):
        original = LocalGitReader.read
        seen = []

        def guarded(reader, commit, path):
            seen.append(path)
            self.assertFalse(path.startswith(("reports/", "artifacts/")))
            return original(reader, commit, path)

        with patch.object(LocalGitReader, "read", guarded):
            bundle = self.assemble()
        self.assertIn("registry/packs/saas-support/provenance.yaml", seen)
        self.assertEqual({m["material_ref"] for m in bundle["materials"] if m["kind"] == "REGISTERED_RULE_EXCERPT"}, {"rule:R3", "rule:R5"})
        self.assertNotIn("R2 窗口", next(m["content"] for m in bundle["materials"] if m["material_ref"] == "rule:R5"))

    def test_short_sha_branch_blob_and_unknown_requirement_rejected(self):
        for value in ("HEAD", "main", self.candidate[:12], self.git("rev-parse", self.candidate + ":" + PRODUCT_PATH)):
            with self.subTest(value=value), self.assertRaises(MaterialError):
                self.assemble(candidate_sha=value)
        with self.assertRaisesRegex(MaterialError, "ASSET_NOT_FOUND"):
            self.assemble(requirement_refs=["R99"])

    def test_different_repository_requires_explicit_synthetic_label(self):
        with self.assertRaisesRegex(MaterialError, "RELEASE_SYNTHETIC_LABEL_REQUIRED"):
            self.assemble(input_provenance="LOCAL_COMMITTED_RELEASE")

    def test_reversed_release_pair_rejected(self):
        with self.assertRaisesRegex(MaterialError, "RELEASE_BASELINE_NOT_ANCESTOR"):
            self.assemble(baseline_sha=self.candidate, candidate_sha=self.baseline)

    def test_dependency_config_prompt_and_migration_changes_are_not_omitted(self):
        paths = {
            "services/genai-support-sut/pyproject.toml": ("DEPENDENCY", "version = '2'\n"),
            "services/genai-support-sut/config/release.json": ("CONFIG_OR_SCHEMA", '{"version": 2}\n'),
            "services/genai-support-sut/prompts/reply.txt": ("PROMPT", "Copy the decision reasons exactly.\n"),
            "services/genai-support-sut/migrations/004.sql": ("MIGRATION", "ALTER TABLE jobs ADD COLUMN revision integer;\n"),
        }
        for path, (_, content) in paths.items():
            self.write(path, content)
        self.candidate = self.commit()
        changes = {row["path"]: row for row in self.assemble()["changes"]}
        for path, (category, _) in paths.items():
            self.assertEqual(changes[path]["category"], category)
            self.assertIn("candidate_material_ref", changes[path])

    def test_outside_profile_changes_remain_explicit_gaps_without_bodies(self):
        self.write("reports/prior-findings.json", '{"root_cause": "do not feed this answer to model"}')
        self.write("business-source-pack/v1/policies/changed.md", "Proposed new company rule without approval")
        self.candidate = self.commit()
        bundle = self.assemble()
        self.assertEqual(sum(row["code"] == "CHANGE_OUTSIDE_PRODUCT_PROFILE" for row in bundle["gaps"]), 2)
        self.assertNotIn("do not feed this answer", json.dumps(bundle))
        self.assertNotIn("prior-findings.json", json.dumps(bundle))
        self.assertNotIn("without approval", json.dumps(bundle))

    def test_private_paths_and_secret_bodies_are_rejected_before_export(self):
        for path, raw in ((".env", "KEY=private"), ("services/genai-support-sut/src/key.py", "-----BEGIN PRIVATE KEY-----")):
            with self.subTest(path=path):
                self.write(path, raw)
                self.candidate = self.commit()
                with self.assertRaises(MaterialError):
                    self.assemble()
                self.git("reset", "--hard", self.baseline)

    def test_candidate_symlink_is_not_followed(self):
        location = self.root / PRODUCT_PATH
        location.unlink()
        location.symlink_to("/etc/passwd")
        self.candidate = self.commit()
        with self.assertRaisesRegex(MaterialError, "SOURCE_SYMLINK_FORBIDDEN"):
            self.assemble()

    def test_binary_and_oversized_sources_are_explicitly_rejected(self):
        for value, code in ((b"binary\0data", "RELEASE_BINARY_MATERIAL_UNSUPPORTED"), ("x" * 100000, "RELEASE_MATERIAL_TOO_LARGE")):
            with self.subTest(code=code):
                self.write(PRODUCT_PATH, value)
                self.candidate = self.commit()
                with self.assertRaisesRegex(MaterialError, code):
                    self.assemble()

    def test_change_count_limit_does_not_silently_truncate(self):
        for index in range(65):
            self.write(f"services/genai-support-sut/src/module{index}.py", "value=1\n")
        self.candidate = self.commit()
        with self.assertRaisesRegex(MaterialError, "RELEASE_CHANGE_SET_TOO_LARGE"):
            self.assemble()

    def test_deleted_file_has_baseline_and_diff_but_no_candidate_body(self):
        (self.root / PRODUCT_PATH).unlink()
        self.candidate = self.commit()
        change = self.assemble()["changes"][0]
        self.assertEqual(change["status"], "D")
        self.assertIn("baseline_material_ref", change)
        self.assertIn("diff_material_ref", change)
        self.assertNotIn("candidate_material_ref", change)

    def test_external_git_diff_is_never_executed(self):
        marker = self.root / "external-diff-ran"
        program = self.root / "external-diff.sh"
        program.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n", encoding="utf-8")
        program.chmod(0o700)
        self.git("config", "diff.external", str(program))
        bundle = self.assemble()
        self.assertEqual(len(bundle["changes"]), 1)
        self.assertFalse(marker.exists())

    def test_file_mode_only_change_is_visible_in_diff_material(self):
        self.write(PRODUCT_PATH, "def model_reply_context(decision):\n    return dict(decision)\n")
        (self.root / PRODUCT_PATH).chmod(0o755)
        self.candidate = self.commit()
        bundle = self.assemble()
        change = bundle["changes"][0]
        self.assertEqual(change["baseline_mode"], "100644")
        self.assertEqual(change["candidate_mode"], "100755")
        diff = next(row["content"] for row in bundle["materials"] if row["material_ref"] == change["diff_material_ref"])
        self.assertIn("old mode 100644\nnew mode 100755", diff)

    def test_identical_versions_are_not_accepted_as_no_change_verdict(self):
        bundle = self.assemble(candidate_sha=self.baseline)
        self.assertEqual(bundle["changes"], [])
        self.assertEqual(bundle["evaluation_status"], "NOT_RUN")
        self.assertIn("NO_VERSION_CHANGES", [gap["code"] for gap in bundle["gaps"]])

    def test_cli_refuses_existing_output_and_frozen_document_location(self):
        arguments = ["--baseline", self.baseline, "--candidate", self.candidate, "--goal", "local calibration",
                     "--candidate-root", str(self.root), "--input-provenance", "CALIBRATION_SYNTHETIC"]
        destination = self.root / "bundle.json"
        self.assertEqual(main(arguments + ["--output", str(destination)]), 0)
        before = destination.read_bytes()
        self.assertEqual(main(arguments + ["--output", str(destination)]), 1)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(main(arguments + ["--output", str(self.root / "README.md")]), 1)
        self.assertEqual(main(arguments + ["--output", str(self.root / "docs" / "run.json")]), 1)


class CalibrationGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("verify_agent_calibration", ROOT / "tools/verify-agent-calibration.py")
        cls.generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.generator)

    def test_reproducible_real_source_excerpts_and_separate_answer_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.generator.generate(Path(directory) / "first")
            second = self.generator.generate(Path(directory) / "second", first["source_commit"])
            goals = set()
            for left, right in zip(first["cases"], second["cases"]):
                self.assertEqual(left["baseline_sha"], right["baseline_sha"])
                self.assertEqual(left["candidate_sha"], right["candidate_sha"])
                one = Path(left["bundle_path"])
                two = Path(right["bundle_path"])
                self.assertEqual(one.read_bytes(), two.read_bytes())
                self.assertEqual(one.stat().st_mode & 0o777, 0o600)
                bundle = json.loads(one.read_text())
                goals.add(bundle["goal"])
                self.assertEqual(bundle["input_provenance"], "CALIBRATION_SYNTHETIC")
                self.assertEqual(bundle["requirement_refs"], ["R1", "R2", "R3", "R4", "R5", "R6"])
                self.assertEqual(len(bundle["changes"]), 1)
                self.assertNotIn("synthetic_intervention", one.read_text())
                self.assertNotIn("expected_analysis_domain", one.read_text())
                self.assertFalse(bundle["catalog"]["dispatch_authorized"])
            self.assertEqual(len(goals), 1)
            self.assertTrue(first["not_a_real_product_pr"])

    def test_generation_refuses_non_temporary_or_existing_destination(self):
        with self.assertRaisesRegex(MaterialError, "CALIBRATION_OUTPUT_OUTSIDE_TEMP_SCOPE"):
            self.generator.generate(ROOT / "docs" / "unapproved")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MaterialError, "CALIBRATION_OUTPUT_ALREADY_EXISTS"):
                self.generator.generate(Path(directory))


if __name__ == "__main__":
    unittest.main()
