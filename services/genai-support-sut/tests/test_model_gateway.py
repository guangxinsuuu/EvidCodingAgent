"""Gateway状态/接口测试与可选真实PG事务测试；假账本不称持久性证明。"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg

from genai_support.business_tools import digest, scripted_draft
from genai_support.config import DEFAULT_FIXTURE_PATH
from genai_support.fixtures import load_fixture_bundle
from genai_support.gateway_store import GatewayStore, GatewayStoreError
from genai_support.model_gateway import (MODEL_REPLY_SCHEMA, PROMPT_REVISION, GatewaySettings, ModelCallFailed,
                                        ModelCallUnknown, ResolutionGateway, call_local_model, create_gateway_app,
                                        validate_model_reply)


TOKEN = "synthetic-gateway-token-not-provider-secret-0001"
COMMAND = {"job_id": "a" * 32, "tenant_id": "tenant-a", "ticket_id": "CS-1042", "ticket_revision": 3}


class MemoryJournal:
    """测试替身，仅证明编排不会重复调用；真实PG行为另测。"""
    def __init__(self) -> None:
        self.rows = {}
        self.events = []
    def health(self) -> bool:
        return True
    def begin(self, request, request_sha256, config_sha256, mode, max_calls, max_output_tokens, budget_id):
        if row := self.rows.get(request["job_id"]):
            if row["request_sha256"] != request_sha256 or row["config_sha256"] != config_sha256:
                raise GatewayStoreError("GATEWAY_IDENTITY_CONFLICT")
            return False, deepcopy(row)
        row = {"job_id": request["job_id"], "request_sha256": request_sha256, "config_sha256": config_sha256,
               "execution_mode": mode, "state": "STARTED", "draft": None}
        self.rows[request["job_id"]] = row
        return True, deepcopy(row)
    def get(self, job_id):
        return deepcopy(self.rows.get(job_id))
    def finish(self, job_id, state, **fields):
        self.rows[job_id].update(state=state, **fields)
        return deepcopy(self.rows[job_id])
    def observe_tool(self, job_id, event):
        self.events.append({"job_id": job_id, **event})


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = load_fixture_bundle(DEFAULT_FIXTURE_PATH)
        self.journal = MemoryJournal()
        self.settings = GatewaySettings(token=TOKEN)

    def test_scripted_completion_is_cached_and_explicitly_not_a_model_call(self) -> None:
        gateway = ResolutionGateway(self.settings, self.fixtures, self.journal)
        status, body = gateway.resolve(COMMAND)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "COMPLETE")
        self.assertEqual(body["execution_mode"], "SCRIPTED_CALIBRATION")
        self.assertEqual(self.journal.get(COMMAND["job_id"])["receipt"]["model_calls"], 0)
        self.assertEqual(gateway.resolve(COMMAND), (status, body))
        self.assertEqual(len(self.journal.events), 7)
        self.assertEqual(self.journal.get(COMMAND["job_id"])["receipt"]["workflow_revision"], "support-policy-decision-v2")

    def test_unknown_model_receipt_never_resends_even_after_new_gateway_instance(self) -> None:
        calls = []
        def unknown(*_args):
            calls.append(1)
            raise ModelCallUnknown("UNKNOWN")
        settings = GatewaySettings(token=TOKEN, execution_mode="LIVE_MODEL",
                                   model_endpoint="http://127.0.0.1:11434/v1/chat/completions", model_name="local-model",
                                   job_reader_dsn="placeholder-never-used")
        with patch("genai_support.model_gateway._verify_job"):
            gateway = ResolutionGateway(settings, self.fixtures, self.journal, unknown)
            self.assertEqual(gateway.resolve(COMMAND)[0], 202)
            recovered = ResolutionGateway(settings, self.fixtures, self.journal, unknown)
            self.assertEqual(recovered.resolve(COMMAND)[1]["status"], "UNKNOWN")
        self.assertEqual(calls, [1])

    def test_incomplete_started_call_is_unknown_not_permission_to_replay(self) -> None:
        self.journal.begin(COMMAND, digest(COMMAND), self.settings.identity(self.fixtures.content_sha256),
                           "SCRIPTED_CALIBRATION", 12, 1024, "test")
        result = ResolutionGateway(self.settings, self.fixtures, self.journal).resolve(COMMAND)
        self.assertEqual(result[0], 202)
        self.assertEqual(self.journal.events, [])

    def test_invalid_complete_output_retains_model_receipt_and_does_not_retry(self) -> None:
        calls = []
        def invalid(*_args):
            calls.append(1)
            return {"wrong": "shape"}, {"model_observed": "actual-model", "usage": {"total_tokens": 42}}
        settings = GatewaySettings(token=TOKEN, execution_mode="LIVE_MODEL",
                                   model_endpoint="http://127.0.0.1:11434/v1/chat/completions", model_name="local-model",
                                   job_reader_dsn="placeholder-never-used")
        with patch("genai_support.model_gateway._verify_job"):
            gateway = ResolutionGateway(settings, self.fixtures, self.journal, invalid)
            self.assertEqual(gateway.resolve(COMMAND)[1]["status"], "FAILED")
            self.assertEqual(gateway.resolve(COMMAND)[1]["status"], "FAILED")
        self.assertEqual(calls, [1])
        self.assertEqual(self.journal.get(COMMAND["job_id"])["receipt"]["model_observed"], "actual-model")

    def test_same_job_with_changed_identity_or_config_is_conflict(self) -> None:
        gateway = ResolutionGateway(self.settings, self.fixtures, self.journal)
        gateway.resolve(COMMAND)
        with self.assertRaisesRegex(GatewayStoreError, "IDENTITY_CONFLICT"):
            gateway.resolve({**COMMAND, "tenant_id": "tenant-b"})
        with self.assertRaisesRegex(GatewayStoreError, "IDENTITY_CONFLICT"):
            ResolutionGateway(replace(self.settings, max_output_tokens=2048), self.fixtures, self.journal).resolve(COMMAND)

    def test_api_requires_internal_auth_and_rejects_extra_or_duplicate_fields(self) -> None:
        with TestClient(create_gateway_app(self.settings, self.fixtures, self.journal)) as client:
            self.assertEqual(client.post("/resolve", json=COMMAND).status_code, 401)
            headers = {"Authorization": "Bearer " + TOKEN}
            self.assertEqual(client.get("/resolutions/" + "b" * 32, headers=headers).status_code, 404)
            self.assertEqual(client.post("/resolve", json={**COMMAND, "model": "attacker"}, headers=headers).status_code, 422)
            raw = json.dumps(COMMAND)[:-1] + ',"ticket_revision":1}'
            self.assertEqual(client.post("/resolve", content=raw, headers={**headers, "content-type": "application/json"}).status_code, 422)
            done = client.post("/resolve", json=COMMAND, headers=headers)
            self.assertEqual(done.status_code, 200)
            self.assertEqual(client.get("/resolutions/" + COMMAND["job_id"], headers=headers).json(), done.json())

    def test_endpoint_and_budget_cannot_escape_fixed_local_model_scope(self) -> None:
        for endpoint in ("https://remote.example/v1/chat/completions", "http://127.0.0.1:11434/other",
                         "http://secret@127.0.0.1:11434/v1/chat/completions", "http://127.0.0.1:11434/v1/chat/completions?x=1"):
            with self.assertRaises(ValueError):
                GatewaySettings(token=TOKEN, execution_mode="LIVE_MODEL", model_endpoint=endpoint, model_name="m", job_reader_dsn="dsn")
        with self.assertRaises(ValueError):
            GatewaySettings(token=TOKEN, max_calls=True)

    def test_live_model_writes_only_reply_and_never_calls_scripted_draft(self) -> None:
        contexts = []
        actual_reply = "根据您的使用记录，用量超过当前政策限制，暂不符合退款条件；未执行退款。"
        def writer(_settings, context):
            contexts.append(context)
            return {"customer_reply_draft": actual_reply}, {"model_observed": "actual-model"}
        settings = GatewaySettings(token=TOKEN, execution_mode="LIVE_MODEL", model_name="local-model",
            model_endpoint="http://127.0.0.1:11434/v1/chat/completions", job_reader_dsn="unused")
        command = {**COMMAND, "ticket_id": "CS-1044", "ticket_revision": 1}
        with patch("genai_support.model_gateway._verify_job"), \
                patch("genai_support.model_gateway.scripted_draft", side_effect=AssertionError("LIVE cannot use scripted reply")):
            status, result = ResolutionGateway(settings, self.fixtures, self.journal, writer).resolve(command)
        self.assertEqual(status, 200)
        self.assertEqual(result["draft"]["eligibility"], "INELIGIBLE")
        self.assertEqual(result["draft"]["proposed_action"], "NO_ACTION")
        self.assertEqual(result["draft"]["applicable_policy_ref"], "POLICY-OCT-14D")
        self.assertEqual(result["draft"]["customer_reply_draft"], actual_reply)
        self.assertEqual(len(contexts), 1)
        self.assertNotIn("body", contexts[0]["ticket"])
        self.assertEqual(contexts[0]["decision"]["reason_codes"], ["USAGE_LIMIT_EXCEEDED"])
        receipt = self.journal.get(command["job_id"])["receipt"]
        self.assertEqual(receipt["prompt_revision"], PROMPT_REVISION)
        self.assertEqual(receipt["decision_sha256"], digest(contexts[0]["decision"]))

    def test_model_reply_contract_forbids_structural_decisions_and_completed_refund_claim(self) -> None:
        self.assertEqual(set(MODEL_REPLY_SCHEMA["properties"]), {"customer_reply_draft"})
        for value in ({"customer_reply_draft": "待审文字", "eligibility": "ELIGIBLE"},
                      {"customer_reply_draft": "退款已成功到账。"}, {"customer_reply_draft": " "}):
            with self.assertRaises(ModelCallFailed):
                validate_model_reply(value)
        self.assertEqual(validate_model_reply({"customer_reply_draft": "您的申请仍需人工审核，尚未执行退款。"}),
                         "您的申请仍需人工审核，尚未执行退款。")

    def test_real_model_request_declares_only_reply_schema_and_records_revision(self) -> None:
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def read(self, _limit):
                return json.dumps({"model": "local-model", "id": "actual-id", "usage": {
                    "prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}, "choices": [{
                    "finish_reason": "stop", "message": {"content": '{"customer_reply_draft":"您的申请仍需人工审核，未执行退款。"}'}}]}).encode()
        settings = GatewaySettings(token=TOKEN, execution_mode="LIVE_MODEL", model_name="local-model",
            model_endpoint="http://127.0.0.1:11434/v1/chat/completions", job_reader_dsn="unused")
        with patch("genai_support.model_gateway.build_opener") as builder:
            builder.return_value.open.return_value = Response()
            reply, receipt = call_local_model(settings, {"decision": {"eligibility": "ELIGIBLE"}})
            request = builder.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], MODEL_REPLY_SCHEMA)
        self.assertEqual(set(reply), {"customer_reply_draft"})
        self.assertEqual(receipt["workflow_revision"], "support-policy-decision-v2")
        self.assertEqual(receipt["request_id"], "actual-id")

    def test_health_reads_model_list_without_inference_and_missing_model_is_not_ready(self) -> None:
        state = {"model_ids": ["local-model"], "paths": []}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                state["paths"].append(self.path)
                raw = json.dumps({"data": [{"id": item} for item in state["model_ids"]]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = GatewaySettings(token=TOKEN, execution_mode="LIVE_MODEL", model_name="local-model",
                model_endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions", job_reader_dsn="unused")
            with TestClient(create_gateway_app(settings, self.fixtures, self.journal)) as client:
                ready = client.get("/health")
                self.assertEqual(ready.status_code, 200)
                self.assertEqual(ready.json()["model_connectivity"], "LISTED")
                state["model_ids"] = ["another-model"]
                self.assertEqual(client.get("/health").status_code, 503)
            self.assertEqual(state["paths"], ["/v1/models", "/v1/models"])
            self.assertEqual(self.journal.rows, {})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_provider_slow_body_has_total_deadline_and_no_retry(self) -> None:
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                try:
                    # 每50ms发一字节，低于socket idle timeout，但总响应永远来不及完成。
                    for _ in range(1000):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = GatewaySettings(token=TOKEN, execution_mode="LIVE_MODEL", model_name="local-model",
                model_endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions", job_reader_dsn="unused", timeout_seconds=1)
            started = time.monotonic()
            with self.assertRaises(ModelCallUnknown):
                call_local_model(settings, {})
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(calls, ["/v1/chat/completions"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@unittest.skipUnless(os.environ.get("EGAP_SUPPORT_TEST_GATEWAY_DATABASE_URL"), "requires isolated real gateway PostgreSQL")
class GatewayPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = GatewayStore(os.environ["EGAP_SUPPORT_TEST_GATEWAY_DATABASE_URL"])
        self.store.migrate()
        self.command = {**COMMAND, "job_id": uuid4().hex}
        self.budget_id = "test-" + uuid4().hex

    def begin(self, command):
        return self.store.begin(command, digest(command), "c" * 64, "LIVE_MODEL", 1, 1024, self.budget_id)

    def test_real_commit_survives_new_store_and_duplicate_does_not_reserve_again(self) -> None:
        created, _ = self.begin(self.command)
        self.assertTrue(created)
        restored = GatewayStore(os.environ["EGAP_SUPPORT_TEST_GATEWAY_DATABASE_URL"])
        self.assertEqual(restored.get(self.command["job_id"])["state"], "STARTED")
        self.assertFalse(self.begin(self.command)[0])
        denied = {**self.command, "job_id": uuid4().hex}
        created, row = self.begin(denied)
        self.assertFalse(created)
        self.assertEqual(row["state"], "FAILED")
        self.assertEqual(row["error_code"], "MODEL_BUDGET_EXHAUSTED")
        self.assertEqual(restored.get(denied["job_id"])["state"], "FAILED")
        with psycopg.connect(os.environ["EGAP_SUPPORT_TEST_GATEWAY_DATABASE_URL"], connect_timeout=3) as connection:
            counts = connection.execute("SELECT reserved_calls,reserved_output_tokens FROM egap_gateway.budgets WHERE budget_id=%s",
                                        (self.budget_id,)).fetchone()
        self.assertEqual(counts, (1, 1024))

    def test_unknown_retains_reservation_and_real_tool_receipts(self) -> None:
        self.begin(self.command)
        self.store.observe_tool(self.command["job_id"], {"tool_name": "get_ticket", "arguments": {}, "result_sha256": "d" * 64})
        self.store.finish(self.command["job_id"], "UNKNOWN", error_code="MODEL_RECEIPT_UNKNOWN")
        self.assertEqual(len(self.store.trace(self.command["job_id"])), 1)
        self.assertEqual(self.begin(self.command)[1]["state"], "UNKNOWN")
        created, denied = self.begin({**self.command, "job_id": uuid4().hex})
        self.assertFalse(created)
        self.assertEqual(denied["error_code"], "MODEL_BUDGET_EXHAUSTED")
        self.assertEqual(self.store.get(self.command["job_id"])["state"], "UNKNOWN")

    def test_concurrent_same_job_has_one_execution_owner_and_one_budget_reservation(self) -> None:
        barrier = threading.Barrier(4)
        def compete(_index):
            barrier.wait(timeout=5)
            return self.begin(self.command)[0]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(compete, range(4)))
        self.assertEqual(sum(results), 1)
        self.assertFalse(self.begin(self.command)[0])
        created, denied = self.begin({**self.command, "job_id": uuid4().hex})
        self.assertFalse(created)
        self.assertEqual(denied["error_code"], "MODEL_BUDGET_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
