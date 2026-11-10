"""启动准备器的凭据边界；不以该单测声称外部产品已经接通。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sut


class PreparationTests(unittest.TestCase):
    def test_commercial_projection_rejects_admin_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.private.json"
            sut.private_save(path, {"base_url": "http://127.0.0.1:42", "admin_token": "never-pass"})
            with self.assertRaisesRegex(RuntimeError, "COMMERCIAL_READER_PROJECTION_REQUIRED"):
                sut.commercial_projection(path)

    def test_explicit_paths_preserve_old_run_and_only_copy_reader_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            old.mkdir()
            (old / "sut.json").write_text("old-evidence")
            runtime = {"chatwoot": {"base_url": "http://127.0.0.1:1", "api_token": "cw", "account_id": 1},
                       "lago": {"base_url": "http://127.0.0.1:2", "api_key": "lago"},
                       "keycloak": {"issuer": "http://127.0.0.1:3/realms/lab", "client_id": "support", "client_secret": "kc", "audience": "support"}}
            sut.private_save(root / "runtime.json", runtime)
            sut.private_save(root / "bindings.json", {"customers": [{"tenant_id": "a"}], "cases": [{"conversation_id": 1}]})
            reader = {"base_url": "http://127.0.0.1:4", "read_token": "r" * 32, "tenant_scopes": ["a"],
                      "instance_id": "commercial_example", "source_digest": "a" * 64, "api_version": "commercial-source-v1",
                      "authority_scope": "LOCAL_LAB", "expires_at": "2026-11-11T00:00:00Z"}
            sut.private_save(root / "reader.json", reader)
            with patch.object(sut, "ARTIFACTS", old):
                result = sut.prepare(runtime_path=root / "runtime.json", bindings_path=root / "bindings.json",
                                     output_directory=root / "new", commercial_config=root / "reader.json")
            self.assertEqual(result.parent, root / "new")
            self.assertEqual((old / "sut.json").read_text(), "old-evidence")
            self.assertFalse((old / "integration.private.json").exists())
            self.assertEqual(json.loads(result.read_text())["commercial_source"], reader)

    def test_reader_configuration_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "PRIVATE_COMMERCIAL_READER_CONFIG_REQUIRED"):
                sut.commercial_projection(path)

    def test_runtime_does_not_receive_deployment_admin_or_operator_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = {"chatwoot": {"base_url": "http://127.0.0.1:1", "api_token": "service-cw", "account_id": 1,
                                    "admin_password": "never-copy-cw-admin"},
                       "lago": {"base_url": "http://127.0.0.1:2", "api_key": "service-lago", "admin_password": "never-copy-lago-admin"},
                       "keycloak": {"issuer": "http://127.0.0.1:3/realms/lab", "client_id": "support", "client_secret": "service-kc",
                                    "audience": "support", "admin_password": "never-copy-kc-admin", "operators": [{"password": "never-copy-user"}]}}
            (root / "runtime.json").write_text(json.dumps(runtime))
            (root / "bindings.json").write_text(json.dumps({"customers": [{"tenant_id": "a"}], "cases": [{"conversation_id": 1}]}))
            with patch.object(sut, "ARTIFACTS", root):
                prepared = sut.prepare()
            self.assertNotIn("never-copy", prepared.read_text())
            self.assertEqual(prepared.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(prepared.read_text())["lago"]["api_key"], "service-lago")

    def test_unseeded_environment_does_not_enable_fixture_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime.json").write_text("{}")
            (root / "bindings.json").write_text('{"customers": [], "cases": []}')
            with patch.object(sut, "ARTIFACTS", root), self.assertRaisesRegex(RuntimeError, "ENV_NOT_READY"):
                sut.prepare()
            self.assertFalse((root / "integration.private.json").exists())


if __name__ == "__main__":
    unittest.main()
