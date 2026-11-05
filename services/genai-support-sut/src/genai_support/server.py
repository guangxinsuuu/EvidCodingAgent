"""HTTP骨架：main → create_app → 认证/协议校验 → 尚未接入的持久任务边界。

此阶段没有内存Job替身。缺少Job+outbox事务时合法请求返回503，不能返回202
制造“已可靠受理”的承诺。Fixture只用于启动校验；没有向客户端暴露整包资料。
"""

import hmac
import json
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .fixtures import FixtureBundle
from .protocol import ProtocolValidationError, parse_support_request


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


def _authenticated(request: Request, settings: Settings) -> bool:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return False
    # 比较bytes以避免非ASCII头触发compare_digest异常；不记录header或token。
    actual = values[0].encode("utf-8")
    expected = ("Bearer " + settings.api_token).encode("ascii")
    return hmac.compare_digest(actual, expected)


def create_app(settings: Settings, fixtures: FixtureBundle) -> FastAPI:
    """组装无副作用ASGI应用。由main先校验配置/冻结fixture，再交给Uvicorn。

    health只代表进程；readiness始终503，直到持久性、队列和Worker真实接通。
    FixtureBundle为可信准备输入，但本地进程内只读不等于生产隔离资格。
    """
    app = FastAPI(title="EGAP Synthetic Support Foundation", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"schema_version": 1, "status": "ok"}

    @app.get("/health/ready")
    async def readiness() -> JSONResponse:
        return JSONResponse(status_code=503, content={
            "schema_version": 1, "status": "not_ready", "scope": "support-service-skeleton",
            "checks": {"configuration": "passed", "fixtures": "validated", "http_routes": "registered"},
            "persistence": "not_implemented", "queue": "not_implemented", "worker": "not_implemented",
            "model_access": "not_configured", "business_verification": "not_performed",
        })

    @app.post("/support-resolution-jobs")
    async def submit(request: Request) -> JSONResponse:
        # 先认证再读body，避免未认证调用占用解析资源。tenant只来自settings。
        if not _authenticated(request, settings):
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
        if fixtures.get_ticket(settings.tenant_id, command.ticket_id, command.ticket_revision) is None:
            return _error(404, "TICKET_NOT_FOUND")
        return _error(503, "JOB_BACKEND_NOT_IMPLEMENTED")

    @app.get("/support-resolution-jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> JSONResponse:
        if not _authenticated(request, settings):
            return _error(401, "UNAUTHORIZED")
        if re.fullmatch(r"[a-f0-9]{32}", job_id) is None:
            return _error(422, "INVALID_INPUT")
        # 没有查询真实存储，不能把“尚未实现”伪装为404“任务确定不存在”。
        return _error(503, "JOB_BACKEND_NOT_IMPLEMENTED")

    return app
