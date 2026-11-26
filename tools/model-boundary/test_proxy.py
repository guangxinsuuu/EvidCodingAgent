"""真实 SQLite/HTTP 的边界与恢复测试；Provider 替身不算真实模型质量证明。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from proxy import Outlet, Denied, encode, parse, digest, handler


CONFIG = {"deployment_id":"evid-sut-" + "a"*12, "model_name":"qwen2:7b", "model_digest":"b"*64,
    "max_calls":2, "max_output_tokens":1024, "product_token":"p"*40, "observer_token":"o"*40,
    "expires_at":(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()}
PAYLOAD = {"model":"qwen2:7b", "stream":False, "temperature":0, "max_tokens":100,
    "response_format":{"type":"json_schema", "json_schema":{"schema":{"type":"object"}}},
    "messages":[{"role":"system", "content":"synthetic test"}, {"role":"user", "content":"fake business input"}]}
BODY = encode(PAYLOAD)


class Provider:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.drift = False
        self.wait = None
        self.entered = threading.Event()

    def request(self, method, path, raw=None, **_):
        if method == "GET":
            return 200, encode({"models":[{"name":"qwen2:7b", "digest": "c"*64 if self.drift else "b"*64}]})
        self.calls += 1
        self.entered.set()
        if self.wait:
            self.wait.wait(3)
        if self.fail:
            raise TimeoutError("simulated unknown")
        return 200, encode({"model":"qwen2:7b", "id":"native-response", "usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5},
            "choices":[{"finish_reason":"stop", "message":{"content":'{"customer_reply_draft":"Synthetic reply"}'}}]})


class OutletTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/"calls.sqlite"
        self.provider = Provider()
        self.outlet = Outlet(CONFIG, self.path, provider=self.provider)

    def test_completed_operation_replays_original_bytes_after_restart_without_cost(self):
        first = self.outlet.invoke("a"*32, BODY)
        restored = Outlet(CONFIG, self.path, provider=self.provider)
        restored.recover()
        self.assertEqual(restored.invoke("a"*32, BODY), first)
        self.assertEqual(self.provider.calls, 1)
        observed = restored.observation("a"*32)
        self.assertEqual(observed["operation"]["request_text"].encode(), BODY)
        self.assertEqual(observed["operation"]["response_sha256"], digest(first[1]))
        self.assertEqual(observed["budget"]["reserved_calls"], 1)

    def test_unknown_never_resends_or_releases_reserved_budget(self):
        self.provider.fail = True
        with self.assertRaisesRegex(Denied, "NO_RESEND"):
            self.outlet.invoke("a"*32, BODY)
        restored = Outlet(CONFIG, self.path, provider=self.provider)
        restored.recover()
        with self.assertRaisesRegex(Denied, "NO_RESEND"):
            restored.invoke("a"*32, BODY)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(restored.observation("a"*32)["operation"]["state"], "UNKNOWN")
        self.assertEqual(restored.observation("a"*32)["budget"]["reserved_calls"], 1)

    def test_crash_after_registration_is_unknown_without_new_provider_call(self):
        with self.outlet.connect() as conn:
            conn.execute("INSERT INTO calls(operation_id,request_sha256,request_text,state,admitted_at) VALUES(?,?,?,'DISPATCHED','test')",
                ("a"*32, digest(BODY), BODY.decode()))
        self.outlet.recover()
        with self.assertRaisesRegex(Denied, "NO_RESEND"):
            self.outlet.invoke("a"*32, BODY)
        self.assertEqual(self.provider.calls, 0)

    def test_budget_and_same_operation_payload_conflicts_fail_before_new_model_call(self):
        self.outlet.invoke("a"*32, BODY)
        with self.assertRaisesRegex(Denied, "CONFLICT"):
            self.outlet.invoke("a"*32, encode({**PAYLOAD,"max_tokens":99}))
        self.outlet.invoke("b"*32, BODY)
        with self.assertRaisesRegex(Denied, "BUDGET_EXHAUSTED"):
            self.outlet.invoke("c"*32, BODY)
        self.assertEqual(self.provider.calls, 2)

    def test_parallel_call_admission_is_serialized_and_does_not_duplicate_effect(self):
        self.provider.wait = threading.Event()
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.outlet.invoke, "a"*32, BODY)
            self.assertTrue(self.provider.entered.wait(2))
            with self.assertRaisesRegex(Denied, "NO_RESEND"):
                self.outlet.invoke("a"*32, BODY)
            with self.assertRaisesRegex(Denied, "CONCURRENCY_LIMIT"):
                self.outlet.invoke("b"*32, BODY)
            self.provider.wait.set()
            self.assertEqual(first.result()[0], 200)
        self.assertEqual(self.provider.calls, 1)

    def test_model_drift_is_rejected_without_spending_call(self):
        self.provider.drift = True
        with self.assertRaisesRegex(Denied, "MANIFEST_DRIFT"):
            self.outlet.invoke("a"*32, BODY)
        self.assertEqual(self.provider.calls, 0)

    def test_changed_identity_cannot_reuse_same_database(self):
        self.outlet.invoke("a"*32, BODY)
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHANGED"):
            Outlet({**CONFIG,"model_digest":"c"*64}, self.path, provider=self.provider)

    def test_arbitrary_model_tools_stream_or_budget_is_not_forwarded(self):
        for changes in ({"model":"other"}, {"stream":True}, {"max_tokens":50000}, {"max_tokens":True},
                        {"tools":["shell"]}, {"url":"http://example.invalid"}):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.outlet.invoke("a"*32, encode({**PAYLOAD, **changes}))
        self.assertEqual(self.provider.calls, 0)

    def test_expired_lease_allows_original_observation_but_no_new_execution(self):
        self.outlet.invoke("a"*32, BODY)
        self.outlet.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(self.outlet.observation("a"*32)["operation"]["state"], "COMPLETE")
        with self.assertRaisesRegex(Denied, "LEASE_EXPIRED"):
            self.outlet.invoke("b"*32, BODY)

    def test_role_routes_and_raw_receipts_over_real_http(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.outlet))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, role, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request(method, path, body, {"Authorization":"Bearer " + role*40,
                    "Content-Type":"application/json", "X-EGAP-Model-Operation":"a"*32})
                response = conn.getresponse()
                return response.status, parse(response.read())
            finally:
                conn.close()
        try:
            self.assertEqual(request("POST", "/v1/chat/completions", "o", BODY)[0], 403)
            self.assertEqual(request("POST", "/api/pull", "p", BODY)[0], 403)
            self.assertEqual(request("GET", "/v1/models", "p")[0], 200)
            self.assertEqual(self.provider.calls, 0)
            self.assertEqual(request("POST", "/v1/chat/completions", "p", BODY)[0], 200)
            self.assertEqual(request("GET", "/v1/operations/" + "a"*32, "p")[0], 403)
            self.assertEqual(request("GET", "/v1/operations/" + "a"*32, "x")[0], 401)
            status, evidence = request("GET", "/v1/operations/" + "a"*32, "o")
            self.assertEqual(status, 200)
            self.assertEqual(evidence["operation"]["request_text"].encode(), BODY)
            self.assertEqual(parse(evidence["operation"]["response_text"])["id"], "native-response")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
