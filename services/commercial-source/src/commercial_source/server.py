"""HTTP 边界：Bearer 身份 → 服务端租户范围 → Store 事务/只读查询。

read_token 仅可 GET /v1/context；admin_token 仅可 POST /v1/commands。
不把 URL 参数当身份，不记录请求正文/headers，不对外暴露 SQL 异常。
"""

from datetime import datetime, timezone
import hmac
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .protocol import AUTHORITY_SCOPE, SOURCE_KIND, Settings, SourceError, parse_command, timestamp
from .store import CommercialStore


MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _error(code: str, status: int):
    return JSONResponse({"error_code": code}, status_code=status)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("NONFINITE_JSON")


def create_app(settings: Settings, store: CommercialStore) -> FastAPI:
    """纯组装入口：__main__ 负责迁移；本函数不建表、不生成实验案例。"""
    app = FastAPI(title="EGAP Local Commercial Source", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def lease_boundary(request: Request, call_next):
        if datetime.now(timezone.utc) >= timestamp(settings.expires_at):
            return _error("SOURCE_LEASE_EXPIRED", 410)
        return await call_next(request)

    def authorized(request: Request, role: str) -> bool:
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            return False
        token = settings.admin_token if role == "admin" else settings.read_token
        return hmac.compare_digest(headers[0].encode("utf-8"), ("Bearer " + token).encode("ascii"))

    @app.get("/health")
    async def health():
        return {"status": "ok", "instance_id": settings.instance_id, "source_kind": SOURCE_KIND,
                "authority_scope": AUTHORITY_SCOPE, "api_version": "commercial-source-v1", "expires_at": settings.expires_at}

    @app.get("/health/ready")
    async def ready():
        try:
            await run_in_threadpool(store.healthy)
        except Exception:
            return _error("SOURCE_DATABASE_UNAVAILABLE", 503)
        return {"status": "ready", "scope": "LOCAL_LAB", "api_version": "commercial-source-v1",
                "instance_id": settings.instance_id, "source_kind": SOURCE_KIND,
                "authority_scope": AUTHORITY_SCOPE, "expires_at": settings.expires_at}

    @app.post("/v1/commands")
    async def command(request: Request):
        if not authorized(request, "admin"):
            return _error("ADMIN_AUTHORIZATION_REQUIRED", 401)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _error("JSON_CONTENT_TYPE_REQUIRED", 415)
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return _error("CONTENT_ENCODING_UNSUPPORTED", 415)
        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_REQUEST_BYTES:
                    return _error("REQUEST_TOO_LARGE", 413)
            value = parse_command(json.loads(body.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant))
            if value["payload"]["tenant_id"] not in settings.tenant_scopes:
                return _error("TENANT_SCOPE_DENIED", 403)
        except SourceError as error:
            return _error(error.code, error.status)
        except (ValueError, UnicodeError, RecursionError):
            return _error("INVALID_JSON", 422)
        try:
            result = await run_in_threadpool(store.execute, value)
            return JSONResponse(result)
        except SourceError as error:
            return _error(error.code, error.status)
        except Exception:
            # 请求可能在提交后丢失回执，明确要求以原操作 key 重试，不生成替代 key。
            return _error("COMMAND_RESULT_UNKNOWN_RETRY_SAME_KEY", 503)

    @app.get("/v1/context")
    async def context(request: Request):
        if not authorized(request, "read"):
            return _error("READ_AUTHORIZATION_REQUIRED", 401)
        fields = {"tenant_id", "customer_id", "subscription_id", "invoice_id", "account_id", "conversation_id"}
        if set(request.query_params.keys()) != fields or len(request.query_params.multi_items()) != len(fields):
            return _error("COMPLETE_UNIQUE_SCOPE_REQUIRED", 422)
        scope = dict(request.query_params)
        if scope["tenant_id"] not in settings.tenant_scopes:
            return _error("TENANT_SCOPE_DENIED", 403)
        try:
            scope["account_id"] = int(scope["account_id"])
            scope["conversation_id"] = int(scope["conversation_id"])
            result = await run_in_threadpool(store.context, scope)
            return JSONResponse(result)
        except SourceError as error:
            return _error(error.code, error.status)
        except ValueError:
            return _error("INVALID_SCOPE", 422)
        except Exception:
            return _error("SOURCE_READ_UNAVAILABLE", 503)

    return app
