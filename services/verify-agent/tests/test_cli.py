"""报告路径不能绕过冻结文档约定；token 权限失败时不得开始模型工作。"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from verify_agent import __main__ as cli


RUN_ID = "54f5c728-7fc1-4439-aeaf-f7360ef35401"
TOKEN = "synthetic-cli-token-no-real-permission"
REPORT = {"run_id": RUN_ID, "outcome": "PLANNED", "business_verdict": "NOT_EVALUATED"}


class CliBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "repo"
        self.root.mkdir()
        self.token = self.root / "private-token"
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)
        self.root_patch = patch.object(cli, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def invoke(self, output: Path, *, token: Path | None = None) -> tuple[int, object]:
        with patch.object(cli, "run_agent", return_value=REPORT) as agent:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = cli.main(["--runtime", "http://127.0.0.1:51234", "--run-id", RUN_ID,
                                   "--token-file", str(token or self.token), "--output", str(output)])
        return result, agent

    def test_new_artifacts_and_tmp_json_reports_are_private(self):
        for container in ("artifacts", "tmp"):
            with self.subTest(container):
                output = self.root / container / "run" / "report.json"
                result, agent = self.invoke(output)
                self.assertEqual(result, 0)
                agent.assert_called_once()
                self.assertEqual(json.loads(output.read_text()), REPORT)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)

    def test_docs_readme_and_outside_report_paths_are_rejected(self):
        for output in (self.root / "docs/new.json", self.root / "README.md", self.root.parent / "outside.json",
                       self.root / "artifacts/../docs/escape.json", self.root / "artifacts/README.md"):
            with self.subTest(output):
                result, agent = self.invoke(output)
                self.assertEqual(result, 2)
                agent.assert_not_called()
                self.assertFalse(output.exists())

    def test_parent_symlink_cannot_escape_artifacts(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts/redirect").symlink_to(outside, target_is_directory=True)
        result, agent = self.invoke(self.root / "artifacts/redirect/report.json")
        self.assertEqual(result, 2)
        agent.assert_not_called()
        self.assertFalse((outside / "report.json").exists())

    def test_output_file_symlink_is_not_followed_or_replaced(self):
        (self.root / "tmp").mkdir()
        target = self.root.parent / "existing.json"
        target.write_text("preserve me")
        output = self.root / "tmp/report.json"
        output.symlink_to(target)
        result, agent = self.invoke(output)
        self.assertEqual(result, 2)
        agent.assert_not_called()
        self.assertTrue(output.is_symlink())
        self.assertEqual(target.read_text(), "preserve me")

    def test_existing_evidence_is_not_overwritten(self):
        output = self.root / "artifacts/report.json"
        output.parent.mkdir()
        output.write_text("previous evidence")
        result, agent = self.invoke(output)
        self.assertEqual(result, 2)
        agent.assert_not_called()
        self.assertEqual(output.read_text(), "previous evidence")

    def test_token_symlink_is_rejected_before_report_or_model_call(self):
        token = self.root / "linked-token"
        token.symlink_to(self.token)
        output = self.root / "artifacts/report.json"
        result, agent = self.invoke(output, token=token)
        self.assertEqual(result, 2)
        agent.assert_not_called()
        self.assertFalse(output.exists())

    def test_token_requires_exact_0600_permissions(self):
        for mode in (0o644, 0o640, 0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                self.token.chmod(mode)
                output = self.root / "tmp/report.json"
                result, agent = self.invoke(output)
                self.assertEqual(result, 2)
                agent.assert_not_called()
                self.assertFalse(output.exists())

    def test_token_fifo_does_not_block_or_start_work(self):
        token = self.root / "token-fifo"
        os.mkfifo(token, mode=0o600)
        output = self.root / "tmp/report.json"
        result, agent = self.invoke(output, token=token)
        self.assertEqual(result, 2)
        agent.assert_not_called()
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
