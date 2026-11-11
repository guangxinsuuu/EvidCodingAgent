"""先约束读取边界，再实现解析器；临时 Git 仓库不会触发任何产品代码。"""
from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.registry.reference_resolver import (
    LocalGitReader, MaterialError, ReadScope, ReferenceResolver,
)
from tools.registry import validate_catalog as validator


class ReferenceResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = b"first\nsecond\nthird\n"
        (self.root / "material.txt").write_bytes(self.raw)
        (self.root / "linked.txt").symlink_to("material.txt")
        self.git("init", "-q")
        self.git("add", "material.txt", "linked.txt")
        self.git("-c", "user.name=Catalog Test", "-c", "user.email=catalog@example.invalid",
                 "commit", "-qm", "fixed fixture")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()
        self.record = {"path": "material.txt", "git_commit": self.commit,
                       "digest_algorithm": "SHA256_RAW_BYTES",
                       "sha256": hashlib.sha256(self.raw).hexdigest()}
        self.scope = ReadScope("local-test", "LOCAL_PLAN", frozenset({"material.txt", "linked.txt"}))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True).stdout

    def resolver(self, record=None, scope=None):
        return ReferenceResolver(LocalGitReader(self.root), [record or self.record], scope or self.scope)

    def assert_code(self, code, callback):
        with self.assertRaises(MaterialError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_reads_registered_commit_after_worktree_changes(self):
        (self.root / "material.txt").write_text("unregistered current content\n")
        result = self.resolver().read_registered_material("material.txt")
        self.assertEqual(result["status"], "READABLE")
        self.assertEqual(result["content"], self.raw.decode())
        self.assertEqual(result["git_commit"], self.commit)
        self.assertEqual(result["sha256"], self.record["sha256"])

    def test_historical_read_does_not_follow_current_worktree_symlink(self):
        (self.root / "material.txt").unlink()
        (self.root / "material.txt").symlink_to("/does/not/exist")
        self.assertEqual(self.resolver().read_registered_material("material.txt")["content"], self.raw.decode())

    def test_digest_mismatch_is_explicit(self):
        record = {**self.record, "sha256": "0" * 64}
        self.assert_code("MATERIAL_DIGEST_MISMATCH", lambda: self.resolver(record).read_registered_material("material.txt"))

    def test_slice_retains_full_identity_and_reports_bounds(self):
        result = self.resolver().read_registered_material("material.txt", 2, 2)
        self.assertEqual(result["content"], "second\n")
        self.assertEqual(result["sha256"], self.record["sha256"])
        self.assertEqual((result["start_line"], result["end_line"], result["total_lines"]), (2, 2, 3))
        self.assertTrue(result["truncated"])

    def test_out_of_bounds_and_invalid_ranges_are_rejected(self):
        for bounds in ((0, 1), (2, 1), (1, 4), (True, 2)):
            with self.subTest(bounds=bounds):
                self.assert_code("MATERIAL_RANGE_INVALID", lambda: self.resolver().read_registered_material("material.txt", *bounds))

    def test_unregistered_is_not_reported_as_source_failure(self):
        self.assert_code("MATERIAL_NOT_REGISTERED", lambda: self.resolver().read_registered_material("unknown.txt"))

    def test_missing_registered_blob_is_distinct_from_denial(self):
        record = {**self.record, "path": "missing.txt"}
        scope = replace(self.scope, allowed_materials=frozenset({"missing.txt"}))
        self.assert_code("SOURCE_FILE_MISSING", lambda: self.resolver(record, scope).read_registered_material("missing.txt"))
        self.assert_code("MATERIAL_ACCESS_DENIED", lambda: self.resolver(scope=replace(self.scope, allowed_materials=frozenset())).read_registered_material("material.txt"))

    def test_metadata_only_never_reads_body(self):
        resolver = self.resolver(scope=replace(self.scope, allow_content=False))
        resolver.git.read = lambda *args: self.fail("metadata-only attempted content read")
        result = resolver.read_registered_material("material.txt")
        self.assertEqual(result["status"], "METADATA_ONLY")
        self.assertIsNone(result["content"])
        self.assertEqual(result["content_verification"], "NOT_READ")

    def test_metadata_permission_is_required(self):
        self.assert_code("MATERIAL_ACCESS_DENIED", lambda: self.resolver(scope=replace(self.scope, allow_metadata=False)).read_registered_material("material.txt"))

    def test_purpose_and_model_egress_cannot_be_claimed_by_caller(self):
        self.assert_code("MATERIAL_PURPOSE_DENIED", lambda: self.resolver().read_registered_material("material.txt", purpose="MODEL_INPUT"))

    def test_private_url_and_escape_paths_are_rejected_before_read(self):
        for path in (".", "../material.txt", "/tmp/material.txt", "https://example.com/a", ".git/config", ".env", "output/private.json", "nested/credentials.private.json"):
            with self.subTest(path=path), self.assertRaises(MaterialError):
                LocalGitReader(self.root).read(self.commit, path)

    def test_git_symlink_is_rejected_even_if_registered(self):
        record = {**self.record, "path": "linked.txt", "sha256": hashlib.sha256(b"material.txt").hexdigest()}
        self.assert_code("SOURCE_SYMLINK_FORBIDDEN", lambda: self.resolver(record).read_registered_material("linked.txt"))

    def test_full_commit_required_and_missing_commit_is_distinct(self):
        self.assert_code("SOURCE_REVISION_INVALID", lambda: LocalGitReader(self.root).read("HEAD", "material.txt"))
        self.assert_code("SOURCE_REVISION_UNAVAILABLE", lambda: LocalGitReader(self.root).read("0" * 40, "material.txt"))

    def test_internal_verified_reader_cannot_bypass_restricted_scope(self):
        for scope in (replace(self.scope, allow_content=False),
                      replace(self.scope, allowed_materials=frozenset())):
            with self.subTest(scope=scope):
                self.assert_code("MATERIAL_ACCESS_DENIED", lambda: self.resolver(scope=scope)._read_verified_bytes("material.txt"))

    def test_large_legal_fragments_are_bounded_without_losing_full_hash(self):
        for raw in (b"row\n" * 300, ("中" * 20000 + "\n").encode()):
            record = {**self.record, "sha256": hashlib.sha256(raw).hexdigest()}
            resolver = self.resolver(record)
            resolver.git.read = lambda *args: raw
            result = resolver.read_registered_material("material.txt", 1, len(raw.decode().splitlines()))
            self.assertTrue(result["truncated"])
            self.assertLessEqual(len(result["content"].encode()), 32768)
            self.assertLessEqual(len(result["content"].splitlines()), 200)
            self.assertEqual(result["sha256"], record["sha256"])

    def test_obvious_private_key_content_is_blocked_in_both_read_paths(self):
        raw = b"-----BEGIN PRIVATE KEY-----\nsynthetic-not-a-key\n"
        resolver = self.resolver({**self.record, "sha256": hashlib.sha256(raw).hexdigest()})
        resolver.git.read = lambda *args: raw
        for read in (resolver._read_verified_bytes, resolver.read_registered_material):
            self.assert_code("MATERIAL_SECRET_CONTENT_FORBIDDEN", lambda: read("material.txt"))


class FixedVersionValidatorTests(unittest.TestCase):
    """真实目录仍复用原校验器；固定读取模式完全不依赖 safe_file 工作树路径。"""

    def setUp(self):
        self.git = LocalGitReader(validator.ROOT)
        self.catalog_commit = "6b1d4815b4178c070f4442edfc2b5606069a3f11"
        self.docs = {name: validator.parse_yaml(self.git.read(self.catalog_commit, str(validator.PACK / name)))
                     for name in validator.FILES}

    def test_fixed_provenance_validates_catalog_sources_and_ast_without_worktree(self):
        with patch.object(validator, "safe_file", side_effect=AssertionError("worktree access")):
            count = validator.verify_provenance(
                validator.ROOT, validator.PACK, self.docs, registered_reader=self.git.read,
                catalog_reader=lambda path: self.git.read(self.catalog_commit, path))
        self.assertEqual(count, 66)

    def test_fixed_history_reuses_original_consistency_checks_without_worktree(self):
        basis = self.docs[validator.FILES[0]]["catalog_basis_commit"]
        with patch.object(validator, "safe_file", side_effect=AssertionError("worktree access")):
            count = validator.verify_history(validator.ROOT, self.docs,
                                             read_file=lambda path: self.git.read(basis, path))
        self.assertEqual(count, 13)


if __name__ == "__main__":
    unittest.main()
