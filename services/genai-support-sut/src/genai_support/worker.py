"""Redis通知 → DB claim → 受控gateway → draft+DONE事务 → Redis ACK。

Worker没有模型供应商密钥，也不把队列tenant/文本当可信事实。gateway以稳定job_id
持久去重；物理attempt和lease_token仅用于DB的陈旧写保护。任何不确定结果保留
PROCESSING与pending通知，之后先读gateway记录对账，不盲目重发模型请求。
"""

from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
import json
import re
import socket
from threading import Event, Lock, Thread, Timer
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from .protocol import ProtocolValidationError, validate_support_job
from .relay import CONSUMER_GROUP, STREAM_KEY


MAX_GATEWAY_RESPONSE_BYTES = 128 * 1024


@dataclass(frozen=True)
class GatewayResult:
    """仅受信HTTP回执的执行状态；COMPLETE不等于业务Oracle PASS。"""

    status: str
    draft: dict[str, Any] | None = None
    execution_mode: str | None = None
    trace_ref: str | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class _RequestDeadline:
    """每个HTTP请求的总时限；慢速分块不能不断重置socket read timeout。

    watchdog只关闭本请求创建的socket。超时表示结果未知，绝不触发备用URL或重试。
    记录socket引用而非conn.sock，兼容HTTPConnection把已交接给response的引用清空。
    """

    def __init__(self, seconds: float) -> None:
        self.lock = Lock()
        self.sockets: list[socket.socket] = []
        self.expired = False
        self.timer = Timer(seconds, self.abort)
        self.timer.daemon = True

    def register(self, owned_socket: socket.socket) -> None:
        with self.lock:
            self.sockets.append(owned_socket)
            if self.expired:
                self._shutdown(owned_socket)

    @staticmethod
    def _shutdown(owned_socket: socket.socket) -> None:
        try:
            owned_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def abort(self) -> None:
        with self.lock:
            self.expired = True
            for owned_socket in self.sockets:
                self._shutdown(owned_socket)

    def __enter__(self) -> "_RequestDeadline":
        self.timer.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.timer.cancel()


class _DeadlineConnection(HTTPConnection):
    def __init__(self, host: str, *, deadline: _RequestDeadline, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self.deadline = deadline

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        self.deadline.register(self.sock)


class _DeadlineHandler(HTTPHandler):
    def __init__(self, deadline: _RequestDeadline) -> None:
        super().__init__()
        self.deadline = deadline

    def http_open(self, request: Request) -> Any:
        return self.do_open(lambda host, **kwargs: _DeadlineConnection(host, deadline=self.deadline, **kwargs), request)


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("DUPLICATE_GATEWAY_FIELD")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("INVALID_GATEWAY_NUMBER")


class GatewayClient:
    """只访问登记的本机网关；无代理、重定向、任意URL或自动请求重试。

    resolve先GET查询同job receipt；仅明确404才POST登记/执行。POST超时不说明
    未发生，下一物理attempt仍查询同job。网关journal承担去重与已发出模型UNKNOWN。
    """

    def __init__(self, endpoint: str, token: str, *, timeout_seconds: float = 135.0) -> None:
        url = urlsplit(endpoint)
        try:
            port = url.port
        except ValueError:
            raise ValueError("INVALID_GATEWAY_ENDPOINT") from None
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not port
                or url.path not in ("", "/") or url.username or url.password or url.query or url.fragment):
            raise ValueError("INVALID_GATEWAY_ENDPOINT")
        if re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token) is None:
            raise ValueError("INVALID_GATEWAY_TOKEN")
        if not 0.05 <= timeout_seconds <= 300:
            raise ValueError("INVALID_GATEWAY_TIMEOUT")
        self.endpoint = endpoint.rstrip("/")
        self._token = token
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, separators=(",", ":")).encode()
        request = Request(self.endpoint + path, data=payload, headers=headers)
        with _RequestDeadline(self.timeout_seconds) as deadline:
            opener = build_opener(ProxyHandler({}), _NoRedirect(), _DeadlineHandler(deadline))
            try:
                response = opener.open(request, timeout=self.timeout_seconds)
            except HTTPError as error:
                response = error
            with response:
                if response.headers.get_content_type() != "application/json":
                    raise ValueError("INVALID_GATEWAY_CONTENT_TYPE")
                raw = response.read(MAX_GATEWAY_RESPONSE_BYTES + 1)
                if len(raw) > MAX_GATEWAY_RESPONSE_BYTES:
                    raise ValueError("GATEWAY_RESPONSE_TOO_LARGE")
                if deadline.expired:
                    raise TimeoutError("GATEWAY_REQUEST_DEADLINE")
                value = json.loads(raw, object_pairs_hook=_unique_json, parse_constant=_reject_constant)
                return response.status, value

    def resolve(self, claim: Any) -> GatewayResult:
        """claim必须来自Store；失败只返回UNKNOWN，不回显URL、token或工具原文。"""
        identity = {"job_id": claim.job_id, "tenant_id": claim.tenant_id,
                    "ticket_id": claim.ticket_id, "ticket_revision": claim.ticket_revision}
        if re.fullmatch(r"[a-f0-9]{32}", claim.job_id) is None:
            raise ValueError("INVALID_CLAIM_IDENTITY")
        try:
            code, body = self._request("/resolutions/" + claim.job_id)
            if code == 404:
                code, body = self._request("/resolve", identity)
            if type(body) is not dict:
                return GatewayResult("UNKNOWN")
            fields = {"schema_version", "status", "draft", "execution_mode", "trace_ref"}
            if set(body) not in (fields, fields | {"error_code"}):
                return GatewayResult("UNKNOWN")
            if (type(body["schema_version"]) is not int or body["schema_version"] != 1
                    or body["trace_ref"] != "gateway:" + claim.job_id
                    or not isinstance(body["execution_mode"], str)
                    or body["execution_mode"] not in {"SCRIPTED_CALIBRATION", "LIVE_MODEL"}):
                return GatewayResult("UNKNOWN")
            if code == 200 and body["status"] == "COMPLETE" and "error_code" not in body:
                validate_support_job({"schema_version": 1, **identity, "status": "DONE",
                                      "draft": body["draft"], "error_code": None})
                return GatewayResult("COMPLETE", body["draft"], body["execution_mode"], body["trace_ref"])
            if (code == 502 and body["status"] == "FAILED" and body["draft"] is None
                    and body.get("error_code") == "PROCESSING_FAILED"):
                return GatewayResult("FAILED", execution_mode=body["execution_mode"], trace_ref=body["trace_ref"])
            return GatewayResult("UNKNOWN", execution_mode=body["execution_mode"], trace_ref=body["trace_ref"])
        except (OSError, URLError, HTTPException, ValueError, RecursionError, ProtocolValidationError):
            return GatewayResult("UNKNOWN")


class _ClaimHeartbeat:
    """长模型调用期间维护进程活性和DB租约；最终写入仍由Store检查fence。

    续约失败不能授权继续提交。失去租约的旧Worker可能仍收到gateway结果，但
    complete_job会拒绝旧token；新Worker可读取同job缓存完成持久化。
    """

    def __init__(self, store: Any, claim: Any, owner: str, lease_seconds: int) -> None:
        self.store, self.claim, self.owner, self.lease_seconds = store, claim, owner, lease_seconds
        self.stop = Event()
        self.thread = Thread(target=self._run, name="support-claim-heartbeat", daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(min(5, self.lease_seconds / 3)):
            try:
                self.store.heartbeat("worker", self.owner)
                if not self.store.renew_claim(self.claim, lease_seconds=self.lease_seconds):
                    return
            except Exception:
                # 留给最终DB条件写裁决，不把临时故障或迟到续约当成功。
                return

    def __enter__(self) -> None:
        self.thread.start()

    def __exit__(self, *_args: Any) -> None:
        self.stop.set()
        self.thread.join(timeout=3)


class Worker:
    """单消费者有界执行器。队列投递次数不等于Job数、模型调用数或草稿效果数。"""

    def __init__(self, store: Any, redis: Any, gateway: GatewayClient, *, owner: str | None = None,
                 lease_seconds: int = 300, retry_idle_ms: int = 5000, block_ms: int = 200) -> None:
        self.store, self.redis, self.gateway = store, redis, gateway
        self.owner = owner or "worker-" + uuid4().hex
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", self.owner) is None:
            raise ValueError("INVALID_WORKER_OWNER")
        if (type(lease_seconds) is not int or not 1 <= lease_seconds <= 300
                or type(retry_idle_ms) is not int or not 1 <= retry_idle_ms <= 600_000
                or type(block_ms) is not int or not 1 <= block_ms <= 1000):
            raise ValueError("INVALID_WORKER_POLICY")
        self.lease_seconds, self.retry_idle_ms, self.block_ms = lease_seconds, retry_idle_ms, block_ms
        self._pending_cursor = "0-0"

    def ensure_group(self) -> None:
        """Redis通知丢失后也可重建group；仅BUSYGROUP表示已经存在，其他失败向上抛。"""
        try:
            self.redis.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0-0", mkstream=True)
        except Exception as error:
            if not str(error).startswith("BUSYGROUP"):
                raise

    def _next_message(self) -> tuple[Any, Mapping[Any, Any]] | None:
        # XAUTOCLAIM恢复崩溃消费者的pending，cursor避免总从头扫描饿死后面的消息。
        reclaimed = self.redis.xautoclaim(STREAM_KEY, CONSUMER_GROUP, self.owner,
                                        self.retry_idle_ms, start_id=self._pending_cursor, count=1)
        self._pending_cursor = reclaimed[0].decode() if isinstance(reclaimed[0], bytes) else reclaimed[0]
        if reclaimed[1]:
            return reclaimed[1][0]
        fresh = self.redis.xreadgroup(CONSUMER_GROUP, self.owner, {STREAM_KEY: ">"}, count=1, block=self.block_ms)
        return fresh[0][1][0] if fresh else None

    def _ack_terminal(self, message_id: Any, job_id: str) -> bool:
        if self.store.get_job_status(job_id) in {"DONE", "FAILED"}:
            self.redis.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
            return True
        return False

    def run_once(self) -> dict[str, Any]:
        """处理至多一条通知。只有已确认终态或明确无效消息才ACK。

        DB/gateway不可达和提交回执未知时异常/UNKNOWN留pending；下次先查DB终态及
        gateway journal，不重新创建Job。消息ACK自身未知也安全：终态重投不重执行。
        """
        self.store.heartbeat("worker", self.owner)
        self.ensure_group()
        message = self._next_message()
        if message is None:
            return {"status": "IDLE"}
        message_id, raw = message
        try:
            fields = {(key.decode() if isinstance(key, bytes) else key):
                      (value.decode() if isinstance(value, bytes) else value) for key, value in raw.items()}
        except (UnicodeError, AttributeError):
            fields = {}
        if set(fields) != {"job_id"} or not isinstance(fields["job_id"], str) or re.fullmatch(r"[a-f0-9]{32}", fields["job_id"]) is None:
            self.redis.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
            return {"status": "INVALID_MESSAGE"}
        job_id = fields["job_id"]
        status = self.store.get_job_status(job_id)
        if status in {"DONE", "FAILED"} or status is None:
            self.redis.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
            return {"status": "TERMINAL_DUPLICATE" if status else "UNKNOWN_JOB", "job_id": job_id}
        claim = self.store.claim_job(job_id, self.owner, lease_seconds=self.lease_seconds)
        if claim is None:
            return {"status": "CLAIM_BUSY", "job_id": job_id}
        with _ClaimHeartbeat(self.store, claim, self.owner, self.lease_seconds):
            result = self.gateway.resolve(claim)
            if result.status == "COMPLETE":
                if self.store.complete_job(claim, result.draft):
                    self.redis.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
                    return {"status": "DONE", "job_id": job_id, "execution_mode": result.execution_mode}
                self._ack_terminal(message_id, job_id)
                return {"status": "STALE_CLAIM", "job_id": job_id}
            if result.status == "FAILED":
                if self.store.fail_job(claim):
                    self.redis.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
                    return {"status": "FAILED", "job_id": job_id}
                self._ack_terminal(message_id, job_id)
                return {"status": "STALE_CLAIM", "job_id": job_id}
            self.store.release_unknown(claim, retry_after_seconds=max(1, (self.retry_idle_ms + 999) // 1000))
            return {"status": "UNKNOWN", "job_id": job_id}

    def run_forever(self, stop: Event, *, poll_seconds: float = 0.25) -> None:
        """命令角色入口；仅输出固定错误码，不泄露连接凭据/业务正文。"""
        if not 0.01 <= poll_seconds <= 10:
            raise ValueError("INVALID_WORKER_POLL_POLICY")
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                print("SUPPORT_WORKER_ITERATION_UNKNOWN", flush=True)
            stop.wait(poll_seconds)
