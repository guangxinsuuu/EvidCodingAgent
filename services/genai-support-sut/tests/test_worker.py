"""Worker/relay的协议与故障窗口；真实PG/Redis闭环由本轮集成验收另行执行。

这里使用可注入的依赖，专门验证“提交后ACK”、重复通知和UNKNOWN，而不以fake
测试宣称真实持久性或模型运行成功。Gateway响应仍走实际共享JSON Schema。
"""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from socketserver import TCPServer
from threading import Thread
import time
from types import SimpleNamespace
import unittest

from genai_support.relay import Relay, STREAM_KEY
from genai_support.worker import GatewayClient, GatewayResult, Worker


JOB = "a" * 32
TOKEN = "synthetic-gateway-test-token-000000000000"
CLAIM = SimpleNamespace(job_id=JOB, tenant_id="tenant-a", ticket_id="CS-1042", ticket_revision=3,
                        attempt_id="b" * 32, lease_token=1, owner="worker-test")
DRAFT = {"resolution_type": "REFUND_ELIGIBILITY_REVIEW", "ticket_id": "CS-1042", "ticket_revision": 3,
         "applicable_policy_ref": "POLICY-OCT-14D", "billing_record_ref": "BILL-a1042",
         "eligibility": "NEEDS_MORE_EVIDENCE", "proposed_action": "REQUEST_INFORMATION",
         "evidence_refs": ["POLICY-OCT-14D"], "customer_reply_draft": "请补充使用和合同资料。"}


class FakeStore:
    def __init__(self):
        self.status = "QUEUED"
        self.log = []
        self.commit = True
        self.pending = [SimpleNamespace(outbox_id=1, job_id=JOB)]
        self.mark_error = False
        self.commit_error = False

    def heartbeat(self, *_args):
        pass

    def pending_outbox(self, *, limit):
        return self.pending[:limit]

    def mark_dispatched(self, outbox_id):
        self.log.append("outbox-mark")
        if self.mark_error:
            raise OSError("private db detail")
        self.pending = [row for row in self.pending if row.outbox_id != outbox_id]

    def reclaimable_jobs(self, *, limit):
        return [JOB] if self.status in {"QUEUED", "PROCESSING"} else []

    def get_job_status(self, job_id):
        assert job_id == JOB
        return self.status

    def claim_job(self, job_id, owner, *, lease_seconds):
        assert job_id == JOB
        self.status = "PROCESSING"
        self.log.append("claim")
        return CLAIM

    def complete_job(self, claim, draft):
        self.log.append("commit")
        if self.commit_error:
            raise OSError("private commit receipt")
        if self.commit:
            self.status = "DONE"
        return self.commit

    def fail_job(self, claim):
        self.log.append("fail")
        self.status = "FAILED"
        return True

    def release_unknown(self, claim, **kwargs):
        self.log.append(("unknown", kwargs["retry_after_seconds"]))
        return True


class FakeRedis:
    def __init__(self, log):
        self.log = log
        self.messages = [("1-0", {"job_id": JOB})]
        self.reclaimed = []
        self.acked = []
        self.published = []
        self.publish_error = False
        self.ack_error = False

    def xgroup_create(self, *args, **kwargs):
        return True

    def xautoclaim(self, *args, **kwargs):
        messages, self.reclaimed = self.reclaimed, []
        return ["0-0", messages, []]

    def xreadgroup(self, *args, **kwargs):
        if not self.messages:
            return []
        return [(STREAM_KEY, [self.messages.pop(0)])]

    def xack(self, stream, group, message):
        self.log.append("ack")
        if self.ack_error:
            raise OSError("unknown ack")
        self.acked.append(message)

    def xadd(self, stream, fields):
        self.log.append("publish")
        if self.publish_error:
            raise OSError("unknown publish")
        self.published.append((stream, fields))


class FakeGateway:
    def __init__(self, status="COMPLETE"):
        self.status = status
        self.calls = []

    def resolve(self, claim):
        self.calls.append(claim)
        return GatewayResult(self.status, DRAFT if self.status == "COMPLETE" else None,
                             "SCRIPTED_CALIBRATION", "gateway:" + JOB)


class WorkerTests(unittest.TestCase):
    def make_worker(self, status="COMPLETE"):
        store = FakeStore()
        redis = FakeRedis(store.log)
        gateway = FakeGateway(status)
        return Worker(store, redis, gateway, owner="worker-test"), store, redis, gateway

    def test_commit_before_ack_and_terminal_duplicate_does_not_call_gateway(self):
        worker, store, redis, gateway = self.make_worker()
        self.assertEqual(worker.run_once()["status"], "DONE")
        self.assertLess(store.log.index("commit"), store.log.index("ack"))
        redis.messages.append(("2-0", {"job_id": JOB}))
        self.assertEqual(worker.run_once()["status"], "TERMINAL_DUPLICATE")
        self.assertEqual(len(gateway.calls), 1)

    def test_unknown_preserves_processing_pending_and_backs_off(self):
        worker, store, redis, gateway = self.make_worker("UNKNOWN")
        self.assertEqual(worker.run_once()["status"], "UNKNOWN")
        self.assertEqual(store.status, "PROCESSING")
        self.assertIn(("unknown", 5), store.log)
        self.assertEqual(redis.acked, [])
        self.assertNotIn("fail", store.log)

    def test_explicit_gateway_failure_commits_failure_before_ack(self):
        worker, store, redis, gateway = self.make_worker("FAILED")
        self.assertEqual(worker.run_once()["status"], "FAILED")
        self.assertEqual(store.log[-2:], ["fail", "ack"])

    def test_stale_claim_cannot_ack_nonterminal_job(self):
        worker, store, redis, gateway = self.make_worker()
        store.commit = False
        self.assertEqual(worker.run_once()["status"], "STALE_CLAIM")
        self.assertEqual(redis.acked, [])

    def test_commit_receipt_loss_keeps_queue_pending(self):
        worker, store, redis, gateway = self.make_worker()
        store.commit_error = True
        with self.assertRaises(OSError):
            worker.run_once()
        self.assertEqual(redis.acked, [])

    def test_ack_receipt_loss_can_replay_terminal_without_model_effect(self):
        worker, store, redis, gateway = self.make_worker()
        redis.ack_error = True
        with self.assertRaises(OSError):
            worker.run_once()
        self.assertEqual(store.status, "DONE")
        redis.ack_error = False
        redis.reclaimed.append(("1-0", {"job_id": JOB}))
        self.assertEqual(worker.run_once()["status"], "TERMINAL_DUPLICATE")
        self.assertEqual(len(gateway.calls), 1)

    def test_queue_cannot_supply_tenant_or_revision_authority(self):
        worker, store, redis, gateway = self.make_worker()
        redis.messages = [("1-0", {"job_id": JOB, "tenant_id": "tenant-b"})]
        self.assertEqual(worker.run_once()["status"], "INVALID_MESSAGE")
        self.assertEqual(gateway.calls, [])
        self.assertNotIn("claim", store.log)

    def test_pending_claim_is_recovered_before_new_messages(self):
        worker, store, redis, gateway = self.make_worker()
        redis.reclaimed = [(b"old-0", {b"job_id": JOB.encode()})]
        self.assertEqual(worker.run_once()["status"], "DONE")
        self.assertEqual(redis.acked, [b"old-0"])
        self.assertEqual(len(redis.messages), 1)


class RelayTests(unittest.TestCase):
    def test_publish_then_mark_and_unknown_receipt_keeps_original_job(self):
        store = FakeStore()
        redis = FakeRedis(store.log)
        relay = Relay(store, redis)
        store.mark_error = True
        with self.assertRaises(OSError):
            relay.run_once()
        self.assertEqual(store.log, ["publish", "outbox-mark"])
        self.assertEqual(len(store.pending), 1)
        store.mark_error = False
        self.assertEqual(relay.run_once(), 1)
        self.assertEqual(redis.published, [(STREAM_KEY, {"job_id": JOB})] * 2)

    def test_unknown_publish_does_not_mark_outbox(self):
        store = FakeStore()
        redis = FakeRedis(store.log)
        redis.publish_error = True
        with self.assertRaises(OSError):
            Relay(store, redis).run_once()
        self.assertNotIn("outbox-mark", store.log)
        self.assertEqual(len(store.pending), 1)


class GatewayClientTests(unittest.TestCase):
    def client(self, receipts):
        client = GatewayClient("http://127.0.0.1:4322", TOKEN)
        calls = []

        def request(path, body=None):
            calls.append((path, body))
            result = receipts.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        client._request = request
        return client, calls

    def receipt(self, status="COMPLETE"):
        return {"schema_version": 1, "status": status,
                "draft": deepcopy(DRAFT) if status == "COMPLETE" else None,
                "execution_mode": "SCRIPTED_CALIBRATION", "trace_ref": "gateway:" + JOB}

    def test_existing_receipt_is_read_without_post(self):
        client, calls = self.client([(200, self.receipt())])
        self.assertEqual(client.resolve(CLAIM).status, "COMPLETE")
        self.assertEqual(calls, [("/resolutions/" + JOB, None)])

    def test_only_confirmed_not_found_causes_single_post_without_attempt_key(self):
        client, calls = self.client([(404, {}), (200, self.receipt())])
        self.assertEqual(client.resolve(CLAIM).status, "COMPLETE")
        self.assertEqual(set(calls[1][1]), {"job_id", "tenant_id", "ticket_id", "ticket_revision"})
        self.assertEqual(calls[1][1]["job_id"], JOB)

    def test_unknown_query_or_transport_never_posts(self):
        for receipt in ((202, self.receipt("UNKNOWN")), (503, {}), OSError("private token")):
            client, calls = self.client([receipt])
            self.assertEqual(client.resolve(CLAIM).status, "UNKNOWN")
            self.assertEqual(len(calls), 1)

    def test_wrong_trace_or_draft_identity_cannot_complete(self):
        for mutation in (lambda row: row.update(trace_ref="gateway:" + "f" * 32),
                         lambda row: row["draft"].update(ticket_revision=2),
                         lambda row: row.update(schema_version=True),
                         lambda row: row.update(execution_mode={"malformed": True})):
            body = self.receipt()
            mutation(body)
            client, calls = self.client([(200, body)])
            self.assertEqual(client.resolve(CLAIM).status, "UNKNOWN")

    def test_only_bound_explicit_failure_is_terminal(self):
        explicit = {**self.receipt("FAILED"), "error_code": "PROCESSING_FAILED"}
        for code, body, expected in ((502, explicit, "FAILED"), (502, self.receipt("FAILED"), "UNKNOWN"),
                                     (503, explicit, "UNKNOWN"), (502, {}, "UNKNOWN")):
            client, calls = self.client([(code, body)])
            self.assertEqual(client.resolve(CLAIM).status, expected)

    def test_gateway_endpoint_and_token_cannot_redirect_authority(self):
        for url in ("https://example.com", "http://localhost:4322", "http://127.0.0.1:4322/extra",
                    "http://secret@127.0.0.1:4322", "http://127.0.0.1:4322?secret=value"):
            with self.assertRaises(ValueError):
                GatewayClient(url, TOKEN)

    def test_slow_response_bytes_cannot_extend_total_request_deadline(self):
        payload = json.dumps(self.receipt()).encode()

        class SlowGateway(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.01)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        class LocalServer(ThreadingHTTPServer):
            def server_bind(self):
                # HTTPServer默认反向DNS在某些宿主很慢；测试固定loopback，不需要外部解析。
                TCPServer.server_bind(self)
                self.server_name = "127.0.0.1"
                self.server_port = self.server_address[1]

        server = LocalServer(("127.0.0.1", 0), SlowGateway)
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            client = GatewayClient(f"http://127.0.0.1:{server.server_port}", TOKEN, timeout_seconds=0.08)
            start = time.monotonic()
            self.assertEqual(client.resolve(CLAIM).status, "UNKNOWN")
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
