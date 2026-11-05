"""真实PostgreSQL + Redis + 本机HTTP测试网关的交付/恢复验收。

由EGAP_SUPPORT_DELIVERY_CONFIG显式指定本机私有配置，仅使用delivery_database_url
和Redis DB2。网关是明确的SCRIPTED_CALIBRATION替身，不调用模型，不作业务Oracle。
每例只清理自己创建的Job/事件/心跳及带测试占用标记的固定stream；禁止flush数据库。
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from socketserver import TCPServer
from threading import Lock, Thread
import time
from unittest.mock import patch
import unittest
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from redis import Redis

from genai_support.config import DEFAULT_FIXTURE_PATH
from genai_support.fixtures import load_fixture_bundle
from genai_support.relay import CONSUMER_GROUP, STREAM_KEY, Relay
from genai_support.store import Store
from genai_support.worker import GatewayClient, Worker


PRIVATE_CONFIG = os.environ.get("EGAP_SUPPORT_DELIVERY_CONFIG")
OWNER_KEY = "egap:support:delivery-integration-owner"


class ScriptedGateway:
    """可控HTTP替身：只记录请求次数/工单引用，模拟独立网关已有UNKNOWN回执。"""

    def __init__(self, *, lose_first_response: bool = False) -> None:
        self.token = secrets.token_urlsafe(32)
        self.lock = Lock()
        self.receipts = {}
        self.identities = {}
        self.post_count = 0
        self.get_count = 0
        self.lose_first_response = lose_first_response

    def envelope(self, job_id: str, identity: dict, status: str) -> dict:
        draft = None if status != "COMPLETE" else {
            "resolution_type": "REFUND_ELIGIBILITY_REVIEW", "ticket_id": identity["ticket_id"],
            "ticket_revision": identity["ticket_revision"], "applicable_policy_ref": "POLICY-OCT-14D",
            "billing_record_ref": "BILL-a1042", "eligibility": "NEEDS_MORE_EVIDENCE",
            "proposed_action": "REQUEST_INFORMATION", "evidence_refs": ["BILL-a1042"],
            "customer_reply_draft": "合成交付恢复测试：请补充使用和合同资料。",
        }
        return {"schema_version": 1, "status": status, "draft": draft,
                "execution_mode": "SCRIPTED_CALIBRATION", "trace_ref": "gateway:" + job_id}

    def complete(self, job_id: str) -> None:
        with self.lock:
            self.receipts[job_id] = self.envelope(job_id, self.identities[job_id], "COMPLETE")

    @contextmanager
    def serving(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status: int, value: dict) -> None:
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authenticated(self) -> bool:
                if self.headers.get("Authorization") == "Bearer " + state.token:
                    return True
                self.reply(401, {"error_code": "UNAUTHORIZED"})
                return False

            def do_GET(self) -> None:
                if not self.authenticated():
                    return
                job_id = self.path.removeprefix("/resolutions/")
                with state.lock:
                    state.get_count += 1
                    receipt = state.receipts.get(job_id)
                self.reply(404 if receipt is None else 202 if receipt["status"] == "UNKNOWN" else 200,
                           receipt if receipt is not None else {"error_code": "NOT_FOUND"})

            def do_POST(self) -> None:
                if not self.authenticated():
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/resolve" or not 1 <= length <= 4096:
                    self.reply(422, {"error_code": "INVALID_INPUT"})
                    return
                identity = json.loads(self.rfile.read(length))
                if set(identity) != {"job_id", "tenant_id", "ticket_id", "ticket_revision"}:
                    self.reply(422, {"error_code": "INVALID_INPUT"})
                    return
                with state.lock:
                    state.post_count += 1
                    job_id = identity["job_id"]
                    state.identities[job_id] = identity
                    receipt = state.envelope(job_id, identity, "UNKNOWN" if state.lose_first_response else "COMPLETE")
                    state.receipts[job_id] = receipt
                if state.lose_first_response:
                    # 已登记本逻辑请求，但TCP回执丢失。Worker只能保留UNKNOWN，稍后GET对账。
                    self.close_connection = True
                    return
                self.reply(200, receipt)

            def log_message(self, *_args) -> None:
                pass

        class LocalServer(ThreadingHTTPServer):
            def server_bind(self):
                TCPServer.server_bind(self)
                self.server_name = "127.0.0.1"
                self.server_port = self.server_address[1]

        server = LocalServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            yield GatewayClient(f"http://127.0.0.1:{server.server_port}", self.token, timeout_seconds=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@unittest.skipUnless(PRIVATE_CONFIG, "requires dedicated real delivery PostgreSQL/Redis configuration")
class DeliveryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = json.loads(Path(PRIVATE_CONFIG).read_text())
        cls.dsn = config["delivery_database_url"]
        info = conninfo_to_dict(cls.dsn)
        if info.get("dbname") != "support_delivery_test" or info.get("host") != "127.0.0.1":
            raise RuntimeError("DEDICATED_DELIVERY_DATABASE_REQUIRED")
        url = urlsplit(config["redis_url"])
        if url.scheme != "redis" or url.hostname != "127.0.0.1" or url.query or url.fragment:
            raise RuntimeError("LOCAL_DELIVERY_REDIS_REQUIRED")
        # 明确只用DB2；不复用运行系统DB0，不执行FLUSHDB/FLUSHALL。
        cls.redis = Redis.from_url(urlunsplit(url._replace(path="/2")), decode_responses=True,
                                   socket_connect_timeout=2, socket_timeout=2)
        cls.store = Store(cls.dsn)
        cls.store.migrate_and_seed(load_fixture_bundle(DEFAULT_FIXTURE_PATH))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.redis.close()

    def setUp(self) -> None:
        self.identity = uuid4().hex
        self.ticket = "DELIVERY-" + self.identity
        self.owned_jobs = []
        self.owned_heartbeats = []
        self.owns_stream = False
        self.owns_ticket = False
        self.addCleanup(self.cleanup_owned_resources)
        if not self.redis.set(OWNER_KEY, self.identity, nx=True, ex=300):
            self.fail("delivery Redis test scope is already owned")
        self.owns_stream = True
        if self.redis.exists(STREAM_KEY):
            # 发现已有stream时不能证明它属于本次测试，保留它并只撤销自己的占用标记。
            self.owns_stream = False
            self.redis.delete(OWNER_KEY)
            self.fail("delivery Redis stream already exists; refuse to overwrite")
        with psycopg.connect(self.dsn) as conn:
            if conn.execute("SELECT count(*) FROM egap_support.support_jobs").fetchone()[0] != 0:
                self.fail("delivery database contains unowned jobs; refuse to dispatch them")
            conn.execute("INSERT INTO egap_support.support_tickets(tenant_id,ticket_id,current_revision) VALUES('tenant-a',%s,3)", (self.ticket,))
        self.owns_ticket = True

    def cleanup_owned_resources(self) -> None:
        if self.owned_jobs or self.owned_heartbeats or self.owns_ticket:
            with psycopg.connect(self.dsn) as conn:
                for table in ("support_job_events", "support_job_attempts", "support_dispatch_outbox", "support_jobs"):
                    # 表名是本代码固定白名单；数据值始终绑定参数，只删除本例记录。
                    conn.execute(f"DELETE FROM egap_support.{table} WHERE job_id=ANY(%s)", (self.owned_jobs,))
                conn.execute("DELETE FROM egap_support.support_service_heartbeats WHERE owner=ANY(%s)", (self.owned_heartbeats,))
                if self.owns_ticket:
                    conn.execute("DELETE FROM egap_support.support_tickets WHERE tenant_id='tenant-a' AND ticket_id=%s", (self.ticket,))
        if self.owns_stream:
            removed = self.redis.eval(
                "if redis.call('GET', KEYS[2]) == ARGV[1] then return redis.call('DEL', KEYS[1], KEYS[2]) else return -1 end",
                2, STREAM_KEY, OWNER_KEY, self.identity)
            self.assertGreaterEqual(removed, 1, "lost Redis ownership; foreign stream was not deleted")

    def create_job(self) -> str:
        job_id = self.store.create_job("tenant-a", self.ticket, 3)["job_id"]
        self.owned_jobs.append(job_id)
        return job_id

    def relay(self) -> Relay:
        owner = "delivery-relay-" + self.identity
        self.owned_heartbeats.append(owner)
        return Relay(self.store, self.redis, owner=owner)

    def worker(self, gateway: GatewayClient, *, suffix: str = "") -> Worker:
        owner = "delivery-worker-" + self.identity + suffix
        self.owned_heartbeats.append(owner)
        return Worker(self.store, self.redis, gateway, owner=owner, lease_seconds=10, retry_idle_ms=1, block_ms=1)

    def scalar(self, sql: str, params=()):
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def await_expired_lease(self, job_id: str) -> None:
        """按DB时钟观测租约过期；有界等待条件，不以固定sleep代替事实。"""
        deadline = time.monotonic() + 4
        while not self.scalar("SELECT lease_until<=clock_timestamp() FROM egap_support.support_jobs WHERE job_id=%s", (job_id,)):
            if time.monotonic() >= deadline:
                self.fail("lease did not expire within test observation budget")
            time.sleep(0.02)

    def test_real_delivery_retries_outbox_and_commits_once_before_ack(self) -> None:
        job_id = self.create_job()
        relay = self.relay()
        # 真实Redis已收到XADD，但relay还没能保存投递确认；重启重投是合法故障窗口。
        with patch.object(self.store, "mark_dispatched", side_effect=RuntimeError("injected relay crash after publish")):
            with self.assertRaises(RuntimeError):
                relay.run_once()
        self.assertEqual(self.redis.xlen(STREAM_KEY), 1)
        self.assertEqual(relay.run_once(), 1)
        self.assertEqual(self.redis.xlen(STREAM_KEY), 2)
        gateway = ScriptedGateway()
        with gateway.serving() as client:
            worker = self.worker(client)
            self.assertEqual(worker.run_once()["status"], "DONE")
            self.assertEqual(worker.run_once()["status"], "TERMINAL_DUPLICATE")
        actual = Store(self.dsn).get_job("tenant-a", job_id)
        self.assertEqual(actual["status"], "DONE")
        self.assertEqual(actual["draft"]["ticket_id"], self.ticket)
        self.assertEqual(gateway.post_count, 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_job_events WHERE job_id=%s AND kind='DONE'", (job_id,)), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_job_attempts WHERE job_id=%s", (job_id,)), 1)
        self.assertEqual(self.redis.xpending(STREAM_KEY, CONSUMER_GROUP)["pending"], 0)

    def test_real_pending_and_expired_claim_are_recovered_with_new_fence(self) -> None:
        job_id = self.create_job()
        self.relay().run_once()
        self.redis.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0-0", mkstream=True)
        notification = self.redis.xreadgroup(CONSUMER_GROUP, "crashed-consumer", {STREAM_KEY: ">"}, count=1, block=1)
        self.assertEqual(notification[0][1][0][1], {"job_id": job_id})
        old = self.store.claim_job(job_id, "crashed-worker", lease_seconds=1)
        self.assertIsNotNone(old)
        self.await_expired_lease(job_id)
        gateway = ScriptedGateway()
        with gateway.serving() as client:
            self.assertEqual(self.worker(client).run_once()["status"], "DONE")
        completed = self.store.get_job("tenant-a", job_id)
        self.assertFalse(self.store.complete_job(old, completed["draft"]))
        self.assertEqual(gateway.post_count, 1)
        self.assertEqual(self.scalar("SELECT lease_token FROM egap_support.support_jobs WHERE job_id=%s", (job_id,)), old.lease_token + 1)
        self.assertEqual(self.scalar("SELECT outcome FROM egap_support.support_job_attempts WHERE attempt_id=%s", (old.attempt_id,)), "UNKNOWN")
        self.assertEqual(self.redis.xpending(STREAM_KEY, CONSUMER_GROUP)["pending"], 0)

    def test_lost_gateway_response_reconciles_by_get_without_reposting(self) -> None:
        job_id = self.create_job()
        self.relay().run_once()
        gateway = ScriptedGateway(lose_first_response=True)
        with gateway.serving() as client:
            worker = self.worker(client)
            self.assertEqual(worker.run_once()["status"], "UNKNOWN")
            self.assertEqual(self.store.get_job_status(job_id), "PROCESSING")
            self.assertEqual(gateway.post_count, 1)
            self.assertEqual(self.redis.xpending(STREAM_KEY, CONSUMER_GROUP)["pending"], 1)
            self.await_expired_lease(job_id)
            self.assertEqual(worker.run_once()["status"], "UNKNOWN")
            self.assertEqual(gateway.post_count, 1)
            gateway.complete(job_id)
            self.await_expired_lease(job_id)
            self.assertEqual(self.worker(client, suffix="-recovered").run_once()["status"], "DONE")
        self.assertEqual(gateway.post_count, 1)
        self.assertEqual(gateway.get_count, 3)
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_job_events WHERE job_id=%s AND kind='DONE'", (job_id,)), 1)
        self.assertEqual(self.redis.xpending(STREAM_KEY, CONSUMER_GROUP)["pending"], 0)

    def test_database_recreates_lost_redis_notification_for_original_job(self) -> None:
        job_id = self.create_job()
        relay = self.relay()
        self.assertEqual(relay.run_once(), 1)
        # 仅删除本例占用的DB2 stream模拟Redis通知丢失，绝不flush或操作运行DB0。
        self.assertEqual(self.redis.get(OWNER_KEY), self.identity)
        self.redis.delete(STREAM_KEY)
        self.assertEqual(relay.run_once(), 0)
        self.assertEqual(relay.reconcile_once(), 1)
        gateway = ScriptedGateway()
        with gateway.serving() as client:
            self.assertEqual(self.worker(client).run_once()["status"], "DONE")
        self.assertEqual(gateway.post_count, 1)
        self.assertEqual(self.store.get_job_status(job_id), "DONE")
        self.assertEqual(self.scalar("SELECT count(*) FROM egap_support.support_jobs WHERE ticket_id=%s", (self.ticket,)), 1)


if __name__ == "__main__":
    unittest.main()
