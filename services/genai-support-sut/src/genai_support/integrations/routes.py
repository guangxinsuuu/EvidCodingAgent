"""mount_integration_routes → OIDC认证 → DisputeService；保留原有v1合成API协议。"""

import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..server import _unique_object, _reject_constant
from ..store import StoreError
from .http import IntegrationError
from .workflow import DisputeService


def mount_integration_routes(app: FastAPI, service: DisputeService) -> None:
    """运行器显式启用真实集成；未配置时不注册，不以mock替代外部依赖。"""
    def failure(exc: Exception) -> JSONResponse:
        if isinstance(exc, IntegrationError):
            return JSONResponse(status_code=exc.status, content={"error_code": exc.code})
        if isinstance(exc, StoreError):
            return JSONResponse(status_code=409 if exc.code == "OPERATION_KEY_CONFLICT" else 503,
                                content={"error_code": exc.code})
        return JSONResponse(status_code=422, content={"error_code": "INVALID_INPUT"})

    async def authenticate(request: Request):
        headers = request.headers.getlist("authorization")
        if len(headers) != 1 or not headers[0].startswith("Bearer "):
            raise IntegrationError("UNAUTHORIZED", status=401)
        return await run_in_threadpool(service.authenticate, headers[0][7:])

    @app.post("/integrations/disputes")
    async def submit(request: Request) -> JSONResponse:
        try:
            identity = await authenticate(request)
            if request.headers.get("content-type", "").split(";")[0] != "application/json" or request.headers.get("content-encoding", "identity") != "identity":
                return JSONResponse(status_code=415, content={"error_code": "UNSUPPORTED_MEDIA_TYPE"})
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 4096:
                    return JSONResponse(status_code=413, content={"error_code": "REQUEST_TOO_LARGE"})
            command = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if not isinstance(command, dict) or set(command) != {"tenant_id", "conversation_id", "invoice_id", "operation_key"}:
                raise ValueError()
            if type(command["conversation_id"]) is not int or command["conversation_id"] < 1 or any(
                    not isinstance(command[k], str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", command[k]) is None
                    for k in ("tenant_id", "invoice_id", "operation_key")):
                raise ValueError()
            result = await run_in_threadpool(service.submit, identity, command)
            return JSONResponse(status_code=202, content=result)
        except (IntegrationError, StoreError, ValueError, TypeError, RecursionError) as exc:
            return failure(exc)

    @app.get("/integrations/disputes/{tenant}/{job}")
    async def status(tenant: str, job: str, request: Request) -> JSONResponse:
        try:
            identity = await authenticate(request)
            if tenant not in identity.tenant_ids:
                raise IntegrationError("CUSTOMER_ACCESS_DENIED", status=403)
            service._binding(tenant, identity.subject)
            value = await run_in_threadpool(service.store.get_job, tenant, job)
            if value is None:
                raise IntegrationError("JOB_NOT_FOUND", status=404)
            delivery = await run_in_threadpool(service.store.delivery, tenant, job)
            return JSONResponse(content={"job": value, "delivery": service._delivery_result(delivery) if delivery else None})
        except (IntegrationError, StoreError) as exc:
            return failure(exc)

    @app.post("/integrations/disputes/{tenant}/{job}/deliver")
    async def deliver(tenant: str, job: str, request: Request) -> JSONResponse:
        try:
            identity = await authenticate(request)
            if tenant not in identity.tenant_ids:
                raise IntegrationError("CUSTOMER_ACCESS_DENIED", status=403)
            service._binding(tenant, identity.subject)
            value = await run_in_threadpool(service.deliver, tenant, job)
            return JSONResponse(content=value)
        except (IntegrationError, StoreError) as exc:
            return failure(exc)
