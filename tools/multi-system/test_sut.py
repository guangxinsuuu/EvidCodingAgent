"""启动准备器的凭据边界；不以该单测声称外部产品已经接通。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sut


class PreparationTests(unittest.TestCase):
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
