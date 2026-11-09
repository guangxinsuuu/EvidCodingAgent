"""独立工具/模型网关：Worker → /resolve → 持久调用身份 → 工具 → 模型 → 回执。

只有网关能调用固定本机模型端点，Worker只持内部会话。调用超时后不会重发模型：
已有STARTED/UNKNOWN由相同job_id查询，需外部对账才能解开不确定状态。
这是本机合成校准链，模型响应与工具收据仍不等于独立SUP验收。
"""

from dataclasses import dataclass, field
import hmac
from http.client import HTTPConnection, HTTPException
import json
import re
import socket
from threading import Lock, Timer
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from fastapi import FastAPI, Request as HttpRequest
from fastapi.responses import JSONResponse
import psycopg
from jsonschema import Draft202012Validator

from .business_tools import (WORKFLOW_REVISION, BusinessToolError, BusinessTools, assemble_draft,
                             digest, model_reply_context, scripted_draft)
from .fixtures import FixtureBundle
from .gateway_store import GatewayStore, GatewayStoreError
from .protocol import ProtocolValidationError, validate_support_job


PROMPT_REVISION = "support-policy-decision-v2"
MODEL_REPLY_SCHEMA = {"type": "object", "additionalProperties": False,
                      "required": ["customer_reply_draft"],
                      "properties": {"customer_reply_draft": {"type": "string", "minLength": 1, "maxLength": 16_384}}}
_REPLY_VALIDATOR = Draft202012Validator(MODEL_REPLY_SCHEMA)


@dataclass(frozen=True)
class GatewaySettings:
    """固定部署配置，不允许请求指定模型/URL/预算；本机模型无供应商密钥。"""

    token: str = field(repr=False)
    execution_mode: str = "SCRIPTED_CALIBRATION"
    model_endpoint: str | None = None
    model_name: str | None = None
    job_reader_dsn: str | None = field(default=None, repr=False)
    max_calls: int = 12
    max_output_tokens: int = 1024
    timeout_seconds: float = 120
    budget_id: str = "support-local-baseline-v1"
    # 外部来源身份绑定到配置摘要；同Job不能通过切换来源模式重新生成。
    business_source: str = "SYNTHETIC_FIXTURE"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", self.token):
            raise ValueError("GATEWAY_CONFIGURATION_INVALID")
        if self.execution_mode not in {"SCRIPTED_CALIBRATION", "LIVE_MODEL"}:
            raise ValueError("GATEWAY_CONFIGURATION_INVALID")
        if self.business_source not in {"SYNTHETIC_FIXTURE", "CHATWOOT_LAGO"}:
            raise ValueError("GATEWAY_CONFIGURATION_INVALID")
        if type(self.max_calls) is not int or not 1 <= self.max_calls <= 100 or type(self.max_output_tokens) is not int \
                or not 128 <= self.max_output_tokens <= 4096 or not 1 <= self.timeout_seconds <= 180:
            raise ValueError("GATEWAY_CONFIGURATION_INVALID")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.budget_id):
            raise ValueError("GATEWAY_CONFIGURATION_INVALID")
        if self.execution_mode == "LIVE_MODEL":
            try:
                endpoint = urlsplit(self.model_endpoint or "")
                valid = endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1" and endpoint.port is not None \
                    and endpoint.path == "/v1/chat/completions" and not endpoint.username and not endpoint.password \
                    and not endpoint.query and not endpoint.fragment
            except ValueError:
                valid = False
            if not valid or not self.model_name or len(self.model_name) > 128 or not self.job_reader_dsn:
                raise ValueError("GATEWAY_CONFIGURATION_INVALID")

    def identity(self, fixture_sha256: str) -> str:
        """身份绑定不含token/DSN；换模型/提示词/fixture须新实验，旧job不能悄悄重算。"""
        return digest({"mode": self.execution_mode, "model_endpoint": self.model_endpoint,
                       "model_name": self.model_name, "max_output_tokens": self.max_output_tokens,
                       "fixture_sha256": fixture_sha256, "business_source": self.business_source, "workflow_revision": WORKFLOW_REVISION,
                       "prompt_revision": PROMPT_REVISION, "model_reply_schema_sha256": digest(MODEL_REPLY_SCHEMA)})


class ModelCallUnknown(RuntimeError):
    """请求可能已经到达模型，保留调用和费用预留，禁止自动重新执行。"""


class ModelCallFailed(RuntimeError):
    """已取得明确拒绝或完整但不可接受的响应；与未收到回执的UNKNOWN区分。"""

    def __init__(self, code: str, receipt: dict[str, Any] | None = None) -> None:
        self.receipt = receipt
        super().__init__(code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _ModelDeadline:
    """总时限覆盖建立连接和完整响应体，不让慢分块不断刷新socket空闲超时。

    只终止本请求持有的socket；超时后调用账本保持UNKNOWN，不触发备用模型或重试。
    独立于Worker客户端，网关无需导入Worker/队列角色以获得网络控制能力。
    """
    def __init__(self, seconds: float) -> None:
        self.expired = False
        self.lock = Lock()
        self.sockets: list[socket.socket] = []
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

    def __enter__(self) -> "_ModelDeadline":
        self.timer.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.timer.cancel()


class _ModelConnection(HTTPConnection):
    def __init__(self, host: str, *, deadline: _ModelDeadline, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self.deadline = deadline

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        self.deadline.register(self.sock)


class _ModelHandler(HTTPHandler):
    def __init__(self, deadline: _ModelDeadline) -> None:
        super().__init__()
        self.deadline = deadline

    def http_open(self, request: Request) -> Any:
        return self.do_open(lambda host, **kwargs: _ModelConnection(host, deadline=self.deadline, **kwargs), request)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("INVALID_JSON")
        value[key] = child
    return value


def _no_constant(_text: str) -> None:
    raise ValueError("INVALID_JSON")


def _json(raw: bytes | str) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_no_constant)


def validate_model_reply(value: Any) -> str:
    """模型只能提交文案一个字段，额外资格/政策/动作字段直接拒绝，绝不覆盖工具决定。

    对“已完成退款”等明确完成声明做有限拒绝；这不是完整自然语言真实性Oracle，
    文案仍为人工待审，独立语义评价留给后续Verifier。
    """
    if not _REPLY_VALIDATOR.is_valid(value):
        raise ModelCallFailed("MODEL_REPLY_INVALID")
    text = value["customer_reply_draft"]
    if not text.strip() or re.search(r"已完成退款|已退款|退款已(?:成功|完成|到账)|已为您(?:办理|完成|处理)退款|退款成功", text):
        raise ModelCallFailed("MODEL_REPLY_INVALID")
    return text


def call_local_model(settings: GatewaySettings, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """一次有界真实本机模型调用，无重试、重定向或HTTP代理；只返回回复文案及回执。

    v2输入是实际业务事实和产品工具已计算的决定，响应Schema只有customer_reply_draft。
    没有先让模型决定资格再事后替换答案；结构决定从调用开始就归业务工具所有。
    完整响应不合格明确FAILED，网络/超时缺回执则UNKNOWN。
    """
    system = (
        "你是SaaS客服回复撰写助手。只依据提供的真实合成事实和decision业务工具结果，用简洁中文直接对客户解释情况。"
        "decision已由业务工具根据版本化政策精确计算，你不重新裁定资格，也不能修改政策、动作或证据。"
        "输出且只输出一个JSON对象，唯一字段customer_reply_draft，内容是2至4句面向客户的待审核回复。"
        "不要重复‘请审核是否符合条件并准备草稿’等给助手的操作指令；不要输出分析过程、内部字段名或枚举值。"
        "ELIGIBLE_PENDING_HUMAN_REVIEW表示初步符合政策，但仍需人工审核；"
        "OUTSIDE_REFUND_WINDOW表示申请时间超出适用政策窗口；USAGE_LIMIT_EXCEEDED表示用量超过政策上限；"
        "CONTRACT_TERMS_MISMATCH表示合同条件不符；BILLING_NOT_PAID表示账单未确认已支付；"
        "MISSING开头的理由要求具体补充缺失记录，已经存在的账单不要重复索要。"
        "必须说明decision.reason_codes对应的真实理由，数字只引用已有事实，不自行编造。"
        "所有内容仍是草稿，本系统没有执行退款或发送消息的能力，绝不能声称已退款、已到账、已操作或已联系客户。"
        "不得输出eligibility、proposed_action、policy_ref等其他字段。"
    )
    payload = {"model": settings.model_name, "stream": False, "temperature": 0,
               "max_tokens": settings.max_output_tokens,
               "response_format": {"type": "json_schema", "json_schema": {
                   "name": "support_customer_reply", "strict": True, "schema": MODEL_REPLY_SCHEMA}},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]}
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > 65_536:
        raise ModelCallFailed("MODEL_INPUT_TOO_LARGE")
    started = time.monotonic()
    request = Request(settings.model_endpoint, data=encoded, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _ModelDeadline(settings.timeout_seconds) as deadline:
            opener = build_opener(ProxyHandler({}), _NoRedirect(), _ModelHandler(deadline))
            with opener.open(request, timeout=settings.timeout_seconds) as response:
                if response.status != 200:
                    raise ModelCallFailed("MODEL_HTTP_REJECTED")
                raw = response.read(131_073)
                if deadline.expired:
                    raise TimeoutError("MODEL_TOTAL_DEADLINE")
    except HTTPError as error:
        # 4xx/重定向是明确拒绝；5xx可能发生于计算后，仍按未知回执处理且不重发。
        unknown = error.code >= 500
        error.close()
        if unknown:
            raise ModelCallUnknown("MODEL_RECEIPT_UNKNOWN") from None
        raise ModelCallFailed("MODEL_HTTP_REJECTED") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise ModelCallUnknown("MODEL_RECEIPT_UNKNOWN") from None
    if len(raw) > 131_072:
        raise ModelCallFailed("MODEL_RESPONSE_TOO_LARGE")
    receipt = None
    try:
        body = _json(raw)
        if not isinstance(body.get("model"), str) or not body["model"] or len(body["model"]) > 256:
            raise ValueError("MISSING_MODEL_IDENTITY")
        usage = body.get("usage")
        if usage is not None and (not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens"))):
            usage = None
        receipt = {"provider": "local-openai-compatible", "model_requested": settings.model_name,
                   "model_observed": body["model"], "request_id": body.get("id") if isinstance(body.get("id"), str) else None,
                   "usage": usage, "usage_status": "OBSERVED" if usage is not None else "UNKNOWN",
                   "request_sha256": digest(payload), "response_sha256": digest(body),
                   "elapsed_ms": round((time.monotonic() - started) * 1000), "synthetic_input": True,
                   "workflow_revision": WORKFLOW_REVISION, "prompt_revision": PROMPT_REVISION,
                   "model_reply_schema_sha256": digest(MODEL_REPLY_SCHEMA)}
        choice = body["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ValueError("INCOMPLETE_OUTPUT")
        reply = _json(choice["message"]["content"])
        validate_model_reply(reply)
        return reply, receipt
    except ModelCallFailed as exc:
        raise ModelCallFailed(str(exc), receipt) from None
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise ModelCallFailed("MODEL_OUTPUT_INVALID", receipt) from None


def probe_local_model(settings: GatewaySettings) -> bool:
    """LIVE健康检查只读/v1/models，不触发推理或消耗调用预算；总时限两秒。

    只证明本机模型服务响应并列出配置模型，不证明推理质量或权重资格。
    """
    if settings.execution_mode != "LIVE_MODEL":
        return True
    endpoint = urlsplit(settings.model_endpoint)
    request = Request(f"http://127.0.0.1:{endpoint.port}/v1/models", headers={"Accept": "application/json"})
    try:
        with _ModelDeadline(2) as deadline:
            opener = build_opener(ProxyHandler({}), _NoRedirect(), _ModelHandler(deadline))
            with opener.open(request, timeout=2) as response:
                raw = response.read(65_537)
                if response.status != 200 or len(raw) > 65_536 or deadline.expired:
                    return False
        body = _json(raw)
        return type(body) is dict and type(body.get("data")) is list and any(
            type(row) is dict and row.get("id") == settings.model_name for row in body["data"])
    except HTTPError as error:
        error.close()
        return False
    except (URLError, TimeoutError, OSError, HTTPException, ValueError, TypeError, RecursionError):
        return False


def _verify_job(settings: GatewaySettings, request: dict[str, Any]) -> None:
    """可选只读SUT连接核对持久Job身份；live模式必须配置此能力。

    连接角色只应被授予SELECT支持Job四个身份列；网关无权把SUT Job改成DONE。
    """
    if settings.job_reader_dsn is None:
        return
    try:
        with psycopg.connect(settings.job_reader_dsn, connect_timeout=3,
                             options="-c statement_timeout=4000 -c default_transaction_read_only=on") as connection:
            row = connection.execute("SELECT tenant_id,ticket_id,ticket_revision FROM egap_support.support_jobs WHERE job_id=%s",
                                     (request["job_id"],)).fetchone()
    except psycopg.Error:
        raise GatewayStoreError("JOB_AUTHORITY_UNAVAILABLE") from None
    if row != (request["tenant_id"], request["ticket_id"], request["ticket_revision"]):
        raise BusinessToolError("JOB_IDENTITY_MISMATCH")


def _envelope(row: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    state = row["state"]
    status = state if state in {"COMPLETE", "FAILED"} else "UNKNOWN"
    value = {"schema_version": 1, "status": status, "draft": row["draft"] if status == "COMPLETE" else None,
             "execution_mode": row["execution_mode"], "trace_ref": "gateway:" + row["job_id"]}
    if status == "FAILED":
        value["error_code"] = "PROCESSING_FAILED"
    return (200 if status == "COMPLETE" else 502 if status == "FAILED" else 202), value


class ResolutionGateway:
    """HTTP层调用的编排服务；只允许begin成功的一次执行者产生新模型效果。"""

    def __init__(self, settings: GatewaySettings, fixtures: FixtureBundle, journal: GatewayStore,
                 model: Callable = call_local_model) -> None:
        self.settings, self.fixtures, self.journal, self.model = settings, fixtures, journal, model

    def resolve(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        _verify_job(self.settings, request)
        created, row = self.journal.begin(request, digest(request), self.settings.identity(self.fixtures.content_sha256),
                                          self.settings.execution_mode, self.settings.max_calls,
                                          self.settings.max_output_tokens, self.settings.budget_id)
        if not created:
            return _envelope(row)
        receipt = None
        try:
            sink = lambda event: self.journal.observe_tool(request["job_id"], event)
            if self.settings.business_source == "CHATWOOT_LAGO":
                # 实际API查询在受理阶段冻结并绑定Job。网关仅SELECT快照，既不持
                # Chatwoot写凭据，也不在缺真实来源时悄悄回退合成事实。
                from .integrations.persistence import ExternalSnapshotTools
                tools = ExternalSnapshotTools(self.settings.job_reader_dsn, sink)
            else:
                tools = BusinessTools(self.fixtures, sink)
            context = tools.collect(request["tenant_id"], request["ticket_id"], request["ticket_revision"])
            decision = tools.evaluate_refund_policy(context)
            if self.settings.execution_mode == "SCRIPTED_CALIBRATION":
                draft, receipt = scripted_draft(context, decision), {"provider": "scripted", "model_calls": 0, "usage_status": "NOT_APPLICABLE"}
            else:
                reply, receipt = self.model(self.settings, model_reply_context(context, decision))
                # 只允许模型提供文案；不是先接收其资格判断再用脚本“修正”结果。
                draft = assemble_draft(context, decision, validate_model_reply(reply))
            receipt = {**receipt, "workflow_revision": WORKFLOW_REVISION, "prompt_revision": PROMPT_REVISION,
                       "decision_sha256": digest(decision), "model_reply_schema_sha256": digest(MODEL_REPLY_SCHEMA)}
            validate_support_job({"schema_version": 1, **request, "status": "DONE", "draft": draft, "error_code": None})
            row = self.journal.finish(request["job_id"], "COMPLETE", draft=draft, receipt=receipt)
        except ModelCallUnknown:
            row = self.journal.finish(request["job_id"], "UNKNOWN", error_code="MODEL_RECEIPT_UNKNOWN")
        except (BusinessToolError, ModelCallFailed, ProtocolValidationError) as exc:
            # 结构不合格也保留已取得的模型身份/usage；不能因业务输出失败抹掉成本证据。
            if isinstance(exc, ModelCallFailed) and exc.receipt is not None:
                receipt = exc.receipt
            receipt = {**(receipt or {}), "workflow_revision": WORKFLOW_REVISION, "prompt_revision": PROMPT_REVISION}
            row = self.journal.finish(request["job_id"], "FAILED", receipt=receipt, error_code="PROCESSING_FAILED")
        # 账本失败不得伪造已完成/失败；STARTED仍在真实DB里，调用方进入UNKNOWN。
        return _envelope(row)


def create_gateway_app(settings: GatewaySettings, fixtures: FixtureBundle, journal: GatewayStore,
                       model: Callable = call_local_model) -> FastAPI:
    """根启动器先migrate独立账本，再调用此工厂；HTTP没有schema/URL/模型选择能力。"""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    resolver = ResolutionGateway(settings, fixtures, journal, model)

    def authenticated(request: HttpRequest) -> bool:
        values = request.headers.getlist("authorization")
        return len(values) == 1 and hmac.compare_digest(values[0].encode(), ("Bearer " + settings.token).encode())

    def error(status: int, code: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"schema_version": 1, "error_code": code})

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            journal.health()
            model_ready = probe_local_model(settings)
            if not model_ready:
                return error(503, "MODEL_NOT_AVAILABLE")
            return JSONResponse({"schema_version": 1, "status": "ok", "execution_mode": settings.execution_mode,
                                 "model_connectivity": "LISTED" if settings.execution_mode == "LIVE_MODEL" else "NOT_APPLICABLE"})
        except GatewayStoreError:
            return error(503, "GATEWAY_STORAGE_UNAVAILABLE")

    @app.get("/resolutions/{job_id}")
    def lookup(job_id: str, request: HttpRequest) -> JSONResponse:
        if not authenticated(request):
            return error(401, "UNAUTHORIZED")
        if re.fullmatch(r"[a-f0-9]{32}", job_id) is None:
            return error(422, "INVALID_INPUT")
        try:
            row = journal.get(job_id)
        except GatewayStoreError:
            return error(503, "GATEWAY_STORAGE_UNAVAILABLE")
        if row is None:
            return error(404, "RESOLUTION_NOT_FOUND")
        status, body = _envelope(row)
        return JSONResponse(status_code=status, content=body)

    @app.post("/resolve")
    async def resolve(request: HttpRequest) -> JSONResponse:
        if not authenticated(request):
            return error(401, "UNAUTHORIZED")
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return error(415, "UNSUPPORTED_MEDIA_TYPE")
        raw = bytearray()
        try:
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 4096:
                    return error(413, "REQUEST_TOO_LARGE")
            value = _json(raw)
            if type(value) is not dict or set(value) != {"job_id", "tenant_id", "ticket_id", "ticket_revision"} \
                    or not isinstance(value["job_id"], str) or re.fullmatch(r"[a-f0-9]{32}", value["job_id"]) is None \
                    or type(value["ticket_revision"]) is not int or not 1 <= value["ticket_revision"] <= 9_007_199_254_740_991 \
                    or any(not isinstance(value[key], str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value[key]) is None
                           for key in ("tenant_id", "ticket_id")):
                return error(422, "INVALID_INPUT")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return error(422, "INVALID_INPUT")
        try:
            # 网络和psycopg均为同步有界操作，放在线程池避免堵住对账GET/health。
            from starlette.concurrency import run_in_threadpool
            status, body = await run_in_threadpool(resolver.resolve, value)
            return JSONResponse(status_code=status, content=body)
        except BusinessToolError:
            return error(403, "JOB_IDENTITY_MISMATCH")
        except GatewayStoreError as exc:
            if str(exc) == "GATEWAY_IDENTITY_CONFLICT":
                return error(409, "GATEWAY_IDENTITY_CONFLICT")
            if str(exc) == "GATEWAY_BUDGET_EXHAUSTED":
                return error(429, "GATEWAY_BUDGET_EXHAUSTED")
            return error(503, "GATEWAY_STORAGE_UNAVAILABLE")

    return app
