"""预检只读与脱敏边界：不依赖宿主 Docker/密钥/端口状态。"""

import errno
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("preflight", Path(__file__).with_name("genai-support-preflight.py"))
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def test_docker_query_cannot_inherit_remote_context_or_credentials(self):
        with patch.object(preflight.shutil, "which", return_value="/bin/docker"), \
                patch.dict(preflight.os.environ, {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "tcp://secret"}), \
                patch.object(preflight.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "28.4.0"
            preflight.run_command(("docker", "info", "--format", "{{.ServerVersion}}"))
            command = run.call_args.args[0]
            self.assertEqual(command[1], "--config")
            self.assertEqual(command[3], "--host")
            self.assertTrue(command[4].startswith("unix://"))
            self.assertNotIn("DOCKER_CONTEXT", run.call_args.kwargs["env"])
            self.assertNotIn("DOCKER_HOST", run.call_args.kwargs["env"])

    def test_versions_never_echo_untrusted_command_output(self):
        result = preflight.tool_version(("node", "--version"), lambda _: {
            "status": "OBSERVED", "exit_code": 0,
            "stdout": "v25.4.0\nhttps://secret:password@host/ bearer-do-not-print",
        })
        self.assertEqual(result["version"], "25.4.0")
        self.assertNotIn("secret", json.dumps(result))

    def test_key_presence_does_not_authorize_model_call(self):
        result = preflight.model_access({name: "sensitive-value" for name in preflight.MODEL_ENV_NAMES})
        self.assertEqual(result["status"], "NOT_RUN")
        self.assertEqual(result["provider_request_count"], 0)
        self.assertNotIn("sensitive-value", json.dumps(result))
        self.assertTrue(all(result["environment_presence_only"].values()))

    def test_port_permission_failure_is_unknown_not_free(self):
        def denied(*args, **kwargs):
            raise PermissionError(errno.EPERM, "denied")
        self.assertEqual(preflight.probe_port(5432, denied)["status"], "UNKNOWN")

    def test_daemon_failure_does_not_query_images_or_claim_runtime(self):
        calls = []
        def unavailable(argv):
            calls.append(argv)
            return {"status": "UNAVAILABLE", "exit_code": 1, "stdout": ""}
        result = preflight.docker_state(unavailable)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["local_images"], {})
        self.assertFalse(result["runtime_images_frozen"])

    def test_local_image_id_is_not_mislabeled_registry_digest(self):
        def available(argv):
            return {"status": "OBSERVED", "exit_code": 0,
                    "stdout": "29.1.0" if argv[1] == "info" else "sha256:" + "a" * 64 + " 18\n"}
        result = preflight.docker_state(available)
        self.assertEqual(result["local_images"]["postgres"]["images"][0]["tag"], "18")
        self.assertIn("local_image_id", result["local_images"]["postgres"]["images"][0])
        self.assertFalse(result["runtime_images_frozen"])


if __name__ == "__main__":
    unittest.main()
