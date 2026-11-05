"""验证启动器传入的能力边界，不启动服务或访问真实凭据。"""

import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("support_service", Path(__file__).with_name("support-service.py"))
assert spec is not None and spec.loader is not None
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class LauncherTests(unittest.TestCase):
    def test_provider_and_publication_credentials_never_enter_child_environment(self) -> None:
        source = {"PATH": "/bin", "OPENAI_API_KEY": "private-model-key", "GH_TOKEN": "private-git-key",
                  "AWS_SECRET_ACCESS_KEY": "private-cloud-key", "PYTHONPATH": "/injected",
                  "PYTHONSTARTUP": "/injected.py", "HTTP_PROXY": "http://credentials@proxy"}
        for action in ("start", "test", "fixtures"):
            actual = service.runtime_environment(action, source)
            self.assertEqual(set(actual), {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"})
            self.assertEqual(actual["PYTHONPATH"], str(service.SERVICE / "src"))
            self.assertNotIn("private-", str(actual))

    def test_service_credentials_only_go_to_explicit_start(self) -> None:
        source = {"EGAP_SUPPORT_API_TOKEN": "local-test", "EGAP_SUPPORT_TENANT_ID": "tenant-a",
                  "EGAP_SUPPORT_PORT": "0", "EGAP_SUPPORT_TEST_EVIDENCE": "local-evidence.json"}
        self.assertEqual(service.runtime_environment("start", source)["EGAP_SUPPORT_API_TOKEN"], "local-test")
        self.assertNotIn("EGAP_SUPPORT_TEST_EVIDENCE", service.runtime_environment("start", source))
        self.assertNotIn("EGAP_SUPPORT_API_TOKEN", service.runtime_environment("test", source))
        self.assertEqual(service.runtime_environment("test", source)["EGAP_SUPPORT_TEST_EVIDENCE"], "local-evidence.json")
        self.assertNotIn("EGAP_SUPPORT_TEST_EVIDENCE", service.runtime_environment("fixtures", source))


if __name__ == "__main__":
    unittest.main()
