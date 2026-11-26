"""独立产品身份与路径映射验证；不运行产品代码，不访问 GitHub。"""
import subprocess
import tempfile
from pathlib import Path
import unittest

from tools.registry.product_repository import ProductGitReader
from tools.registry.reference_resolver import MaterialError
from tools.registry.release_assembler import assemble_review_request
from tools.registry.test_review_request import request_fixture
from tools.registry.validate_catalog import ROOT

ORIGIN = "https://github.com/guangxinsuuu/EvidSupportSUT.git"
PHYSICAL = "src/genai_support/integrations/commercial.py"
LOGICAL = "services/genai-support-sut/" + PHYSICAL


class ProductRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Product Boundary Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("remote", "add", "origin", ORIGIN)
        self.write("value = 1\n")
        self.baseline = self.commit()
        self.write("value = 2\n")
        self.candidate = self.commit()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def write(self, text):
        p = self.repo / PHYSICAL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "product change")
        return self.git("rev-parse", "HEAD")

    def request(self):
        req = request_fixture(self.baseline, self.candidate)
        for side in ("baseline", "candidate"):
            req[side]["source_ref"] = ORIGIN
        return req

    def test_freezes_product_objects_and_real_paths_not_worktree(self):
        self.write("not committed; must not enter material\n")
        bundle = assemble_review_request(ROOT, self.request(), candidate_root=self.repo,
            input_provenance="REGISTERED_EXTERNAL_PRODUCT", product_id="evid-support")
        self.assertEqual(bundle["schema_version"], 2)
        self.assertEqual(bundle["product_binding"]["candidate"]["tree"], self.git("rev-parse", self.candidate + "^{tree}"))
        self.assertNotEqual(bundle["product_binding"]["egap_commit"], self.candidate)
        current = next(m for m in bundle["materials"] if m["material_ref"] == "candidate:" + LOGICAL)
        self.assertEqual(current["source_path"], PHYSICAL)
        self.assertEqual(current["repository_id"], "evid-support")
        self.assertEqual(current["content"], "value = 2\n")
        self.assertEqual(bundle["current_candidate_coverage"], [])
        self.assertFalse(bundle["product_binding"]["remote_attestation"])

    def test_product_root_origin_and_registration_are_separately_checked(self):
        with self.assertRaisesRegex(MaterialError, "PRODUCT_REPOSITORY_MUST_BE_SEPARATE"):
            ProductGitReader(ROOT, product_id="evid-support", authority_root=ROOT)
        with self.assertRaisesRegex(MaterialError, "PRODUCT_REPOSITORY_ROOT_REQUIRED"):
            ProductGitReader(self.repo / "src", product_id="evid-support", authority_root=ROOT)
        with self.assertRaisesRegex(MaterialError, "PRODUCT_NOT_REGISTERED"):
            ProductGitReader(self.repo, product_id="invented", authority_root=ROOT)
        self.git("remote", "set-url", "origin", "https://example.invalid/other.git")
        with self.assertRaisesRegex(MaterialError, "PRODUCT_REPOSITORY_ORIGIN_MISMATCH"):
            ProductGitReader(self.repo, product_id="evid-support", authority_root=ROOT)

    def test_old_external_checkout_rule_is_not_silently_relaxed(self):
        with self.assertRaisesRegex(MaterialError, "RELEASE_SYNTHETIC_LABEL_REQUIRED"):
            assemble_review_request(ROOT, self.request(), candidate_root=self.repo)
        with self.assertRaisesRegex(MaterialError, "RELEASE_PROVENANCE_INVALID"):
            assemble_review_request(ROOT, self.request(), candidate_root=self.repo,
                input_provenance="REGISTERED_EXTERNAL_PRODUCT")
        wrong = self.request(); wrong["candidate"]["source_ref"] = "local:wrong"
        with self.assertRaisesRegex(MaterialError, "REVIEW_PRODUCT_SOURCE_MISMATCH"):
            assemble_review_request(ROOT, wrong, candidate_root=self.repo,
                input_provenance="REGISTERED_EXTERNAL_PRODUCT", product_id="evid-support")

    def test_product_symlink_is_not_read_as_source(self):
        p = self.repo / PHYSICAL
        p.unlink(); p.symlink_to("/etc/passwd")
        self.candidate = self.commit()
        with self.assertRaisesRegex(MaterialError, "SOURCE_SYMLINK_FORBIDDEN"):
            assemble_review_request(ROOT, self.request(), candidate_root=self.repo,
                input_provenance="REGISTERED_EXTERNAL_PRODUCT", product_id="evid-support")
