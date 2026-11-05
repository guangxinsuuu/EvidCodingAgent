"""真实本机子进程/端口验收；只检验服务骨架，不冒充05.4业务smoke。

临时token只在内存和子进程环境中，日志/证据不保存它。finally精确停止本测试PID，
不扫描或清理其他进程。进程重启、DB恢复和容器身份仍由后续05.2/05.3实现。
"""

import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


class LifecycleTests(unittest.TestCase):
    def test_real_process_protocol_and_exact_shutdown(self) -> None:
        token = secrets.token_urlsafe(32)
        # 不继承模型key、HTTP代理等无关能力；仅允许当前测试服务运行所需变量。
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}}
        environment.update(EGAP_SUPPORT_API_TOKEN=token, EGAP_SUPPORT_TENANT_ID="tenant-a", EGAP_SUPPORT_PORT="0")
        process = subprocess.Popen([sys.executable, "-m", "genai_support.main"], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        port = None
        observations = []
        try:
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=10), "service did not report bound socket")
                line = process.stdout.readline()
            if not line:
                self.fail("service exited before binding; inspect configuration or local-port permissions")
            bound = json.loads(line)
            self.assertEqual(bound["event"], "support_socket_bound")
            self.assertEqual(bound["pid"], process.pid)
            self.assertEqual(bound["host"], "127.0.0.1")
            port = bound["port"]
            self.assertGreater(port, 0)
            opener = build_opener(ProxyHandler({}))

            def request(path: str, payload: dict | None = None, auth: bool = False) -> tuple[int, dict]:
                headers = {"Authorization": "Bearer " + token} if auth else {}
                if payload is not None:
                    headers["Content-Type"] = "application/json"
                req = Request(f"http://127.0.0.1:{port}{path}",
                              data=None if payload is None else json.dumps(payload).encode(), headers=headers)
                try:
                    response = opener.open(req, timeout=1)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read(8192))

            deadline = time.monotonic() + 8
            while True:
                try:
                    self.assertEqual(request("/health"), (200, {"schema_version": 1, "status": "ok"}))
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.025)
            payload = {"schema_version": 1, "ticket_id": "CS-1042", "ticket_revision": 3}
            for path, data, auth, expected in (
                ("/health/ready", None, False, 503),
                ("/support-resolution-jobs", payload, False, 401),
                ("/support-resolution-jobs", payload, True, 503),
                ("/support-resolution-jobs", {**payload, "ticket_revision": 0}, True, 422),
                ("/support-resolution-jobs", {**payload, "tenant_id": "tenant-b"}, True, 422),
                ("/support-resolution-jobs/" + "a" * 32, None, True, 503),
            ):
                status, body = request(path, data, auth)
                self.assertEqual(status, expected)
                self.assertNotIn(token, json.dumps(body))
                observations.append({"path": path, "status": status, "error_code": body.get("error_code")})
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=3)
                self.fail("service needed forced kill instead of bounded shutdown")
            self.assertNotIn(token, stdout + stderr)
        self.assertIn(process.returncode, (0, -signal.SIGTERM))
        assert port is not None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0, "test process leaked listening socket")
        if evidence_path := os.environ.get("EGAP_SUPPORT_TEST_EVIDENCE"):
            # 调用者选择本地忽略目录；只写端口/状态，不写token、工单正文或模型数据。
            Path(evidence_path).write_text(json.dumps({
                "schema_version": 1, "kind": "support-skeleton-lifecycle", "scope": "local-foundation-only",
                "port": port, "pid": process.pid, "health_status": 200, "observations": observations,
                "process_returncode": process.returncode, "port_closed": True,
                "persistent_job_acceptance": "NOT_IMPLEMENTED", "model_calls": 0,
                "business_verification": "NOT_PERFORMED",
            }, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
