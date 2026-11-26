"""构建输入隔离测试：真实 Git 对象、脏工作树、未跟踪凭据与链接边界。"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import deployment
import product
from tools.registry.reference_resolver import MaterialError


class BuildContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.context = self.root / "context"; self.context.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Build Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("remote", "add", "origin", "https://github.com/guangxinsuuu/EvidSupportSUT.git")
        (self.repo / "Dockerfile").write_text("FROM scratch\n")
        (self.repo / "app.py").write_text("value = 1\n")
        (self.repo / ".gitattributes").write_text("app.py export-ignore\n")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", "."); self.git("commit", "-qm", "candidate")
        return self.git("rev-parse", "HEAD")

    def test_only_committed_objects_enter_build_context(self):
        commit = self.commit()
        (self.repo / "app.py").write_text("dirty value\n")
        (self.repo / ".local").mkdir()
        (self.repo / ".local/credentials.private.json").write_text("private sentinel")
        source = product.build_context(self.repo, commit, self.context)
        self.assertEqual((self.context / "app.py").read_text(), "value = 1\n")
        self.assertFalse((self.context / ".local").exists())
        self.assertFalse((self.context / ".git").exists())
        self.assertEqual(source["commit"], commit)

    def test_committed_private_files_and_links_fail_closed(self):
        (self.repo / ".env").write_text("private sentinel")
        commit = self.commit()
        with self.assertRaisesRegex(MaterialError, "PRIVATE_SOURCE_FORBIDDEN"):
            product.build_context(self.repo, commit, self.context)
        self.git("rm", ".env")
        (self.repo / "outside").symlink_to("/etc/passwd")
        commit = self.commit()
        with self.assertRaisesRegex(MaterialError, "BUILD_CONTEXT_REGULAR_FILES_ONLY"):
            product.build_context(self.repo, commit, self.context)

    def test_role_configs_do_not_share_observer_or_administrator_credentials(self):
        image = "sha256:" + "a" * 64
        def docker(args, **kwargs):
            if args[-1] == "{{.Id}}": return "sha256:" + "b" * 64
            return json.dumps([{"Config": {"Labels": {"org.opencontainers.image.revision": "c" * 40}}}])
        with patch.object(deployment, "DEPLOYMENTS", self.root / "deployments"), patch.object(deployment, "command", docker):
            directory = deployment.prepare(image)
        compose = json.loads((directory / "compose.json").read_text())
        for role in ("api", "relay", "worker", "gateway"):
            conf = json.loads((directory / (role + ".private.json")).read_text())
            self.assertNotIn("admin_database_url", conf)
            self.assertNotIn("support_observer", json.dumps(conf))
            self.assertEqual(compose["services"][role]["volumes"], [f"{directory}/{role}.private.json:/run/secrets/role.json:ro"])
        self.assertNotIn("access", compose["services"]["gateway"]["networks"])
