"""API入口 → 认证/共享协议 → Store事务；202仅在Job+outbox提交之后返回。

不带Store仍可启动原骨架测试。服务不直调模型，持久通知由relay/Worker消费；
HTTP提交不自动重试，数据库提交回执丢失时保留UNKNOWN语义。
"""

import hmac
import json
import re
from typing import Any, Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .fixtures import FixtureBundle
from .protocol import ProtocolValidationError, parse_support_request
from .store import Store, StoreError


MAX_REQUEST_BYTES = 4096


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"schema_version": 1, "error_code": code})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """HTTP输入不得靠重复键覆盖tenant/revision；固定错误避免泄露原始工单。"""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("INVALID_JSON_NUMBER")


def _authenticated_tenant(request: Request, sessions: Mapping[str, str]) -> str | None:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    # 比较bytes以避免非ASCII头触发compare_digest异常；不记录header或token。
    actual = values[0].encode("utf-8")
    tenant = None
    for token, identity in sessions.items():
        if hmac.compare_digest(actual, ("Bearer " + token).encode("ascii")):
            tenant = identity
    return tenant


def create_app(settings: Settings, fixtures: FixtureBundle, store: Store | None = None,
               *, sessions: Mapping[str, str] | None = None,
               runtime_health: Callable[[], dict[str, Any]] | None = None,
               legacy_enabled: bool = True) -> FastAPI:
    """组装无副作用ASGI应用。由main先校验配置/冻结fixture，再交给Uvicorn。

    health只代表进程；readiness须持久性、队列和Worker真实接通。
    FixtureBundle为可信准备输入，但本地进程内只读不等于生产隔离资格。
    """
    app = FastAPI(title="EGAP Synthetic Support Foundation", docs_url=None, redoc_url=None, openapi_url=None)
    identities = dict(sessions or {settings.api_token: settings.tenant_id})
    for token, tenant_id in identities.items():
        Settings(api_token=token, tenant_id=tenant_id)  # 注册会话受同样配置约束。

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"schema_version": 1, "status": "ok"}

    @app.get("/health/ready")
    async def readiness() -> JSONResponse:
        if store is not None and runtime_health is not None:
            try:
                checks = await run_in_threadpool(runtime_health)
                ready = all(checks.get(key) is True for key in ("database", "redis", "relay", "worker", "gateway"))
            except Exception:
                checks, ready = {"dependency_check": "unavailable"}, False
            return JSONResponse(status_code=200 if ready else 503, content={
                "schema_version": 1, "status": "ready" if ready else "not_ready", "scope": "support-local-calibration",
                "checks": checks, "business_verification": "not_performed",
            })
        return JSONResponse(status_code=503, content={
            "schema_version": 1, "status": "not_ready", "scope": "support-service-skeleton",
            "checks": {"configuration": "passed", "fixtures": "validated", "http_routes": "registered"},
            "persistence": "not_implemented", "queue": "not_implemented", "worker": "not_implemented",
            "model_access": "not_configured", "business_verification": "not_performed",
        })

    @app.post("/support-resolution-jobs")
    async def submit(request: Request) -> JSONResponse:
        if not legacy_enabled:
            # 真实集成只通过OIDC入口；不能借旧静态token绕过客户授权。
            return _error(404, "LEGACY_API_DISABLED")
        # 先认证再读body，避免未认证调用占用解析资源。tenant只来自settings。
        tenant_id = _authenticated_tenant(request, identities)
        if tenant_id is None:
            return _error(401, "UNAUTHORIZED")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error(415, "UNSUPPORTED_MEDIA_TYPE")
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return _error(415, "UNSUPPORTED_CONTENT_ENCODING")
        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_REQUEST_BYTES:
                    return _error(413, "REQUEST_TOO_LARGE")
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            command = parse_support_request(value)
        except (ValueError, UnicodeError, RecursionError, ProtocolValidationError):
            return _error(422, "INVALID_INPUT")
        # 同一租户内读取已冻结的fixture。不存在与跨租户引用统一404，防止枚举。
        if fixtures.get_ticket(tenant_id, command.ticket_id, command.ticket_revision) is None:
            return _error(404, "TICKET_NOT_FOUND")
        if store is None:
            return _error(503, "JOB_BACKEND_NOT_IMPLEMENTED")
        try:
            result = await run_in_threadpool(store.create_job, tenant_id, command.ticket_id, command.ticket_revision)
            return JSONResponse(status_code=202, content=result)
        except StoreError as error:
            if error.code == "TICKET_NOT_FOUND":
                return _error(404, "TICKET_NOT_FOUND")
            if error.code == "TICKET_REVISION_CONFLICT":
                return _error(409, "TICKET_REVISION_CONFLICT")
            return _error(503, "JOB_SUBMISSION_UNKNOWN")

    @app.get("/support-resolution-jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> JSONResponse:
        if not legacy_enabled:
            return _error(404, "LEGACY_API_DISABLED")
        tenant_id = _authenticated_tenant(request, identities)
        if tenant_id is None:
            return _error(401, "UNAUTHORIZED")
        if re.fullmatch(r"[a-f0-9]{32}", job_id) is None:
            return _error(422, "INVALID_INPUT")
        if store is None:
            return _error(503, "JOB_BACKEND_NOT_IMPLEMENTED")
        try:
            result = await run_in_threadpool(store.get_job, tenant_id, job_id)
            return _error(404, "JOB_NOT_FOUND") if result is None else JSONResponse(content=result)
        except StoreError:
            return _error(503, "JOB_LOOKUP_UNAVAILABLE")

    return app
