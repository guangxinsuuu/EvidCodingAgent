"""LangGraph → 回环 HTTP Runtime；本进程没有数据库、SUT、模型出口凭据。

HTTP 失败不重试 POST。请求可能已被 Runtime 接收，重启后应先 GET 投影，
使用 Runtime 保存的 command/proposal 身份恢复，而不是产生新操作掩盖未知状态。
"""
from __future__ import annotations

import json
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from .protocol import ProtocolError, parse_json


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class RuntimeTransportError(RuntimeError):
    """网络或 Runtime 拒绝；code 可报告，HTTP 正文/凭据不进入异常。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RuntimePort(Protocol):
    def get_run_projection(self, run_id: str) -> dict: ...
    def decide(self, run_id: str, command_id: str, based_on_decision_revision: int) -> dict: ...
    def submit_proposal(self, run_id: str, proposal: dict) -> dict: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        # Bearer 凭据不可随着服务器的重定向离开固定 authority。
        return None


class HttpRuntimePort:
    """本地 V0.1 可信启动参数限定 127.0.0.1，模型不能指定 endpoint/path。"""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 360):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.port is None):
            raise ValueError("LOCAL_RUNTIME_ENDPOINT_REQUIRED")
        if not isinstance(token, str) or not 20 <= len(token) <= 4096 or any(c.isspace() for c in token):
            raise ValueError("INVALID_RUNTIME_TOKEN")
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("INVALID_RUNTIME_TIMEOUT")
        self._base_url, self._token, self._timeout = base_url.rstrip("/"), token, timeout_seconds
        # 不继承系统 HTTP_PROXY，不安装带隐式重试的客户端/SDK。
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @staticmethod
    def _run_path(run_id: str) -> str:
        try:
            if str(UUID(run_id)) != run_id:
                raise ValueError
        except (ValueError, AttributeError) as error:
            raise ValueError("CANONICAL_RUN_ID_REQUIRED") from error
        return "/runs/" + run_id

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        request = Request(self._base_url + path, data=data, method=method,
                          headers={"Authorization": "Bearer " + self._token,
                                   "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RuntimeTransportError("RUNTIME_RESPONSE_TOO_LARGE")
        except HTTPError as error:
            # 不返回服务错误正文，它可能包含调试信息或请求原文。
            raise RuntimeTransportError("RUNTIME_HTTP_" + str(error.code)) from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeTransportError("RUNTIME_TRANSPORT_UNCONFIRMED") from None
        try:
            result = parse_json(raw)
        except ProtocolError:
            raise RuntimeTransportError("RUNTIME_INVALID_JSON") from None
        if not isinstance(result, dict):
            raise RuntimeTransportError("RUNTIME_INVALID_RESPONSE")
        return result

    def get_run_projection(self, run_id: str) -> dict:
        return self._request("GET", self._run_path(run_id))

    def decide(self, run_id: str, command_id: str, based_on_decision_revision: int) -> dict:
        return self._request("POST", self._run_path(run_id) + "/decide", {
            "command_id": command_id,
            "based_on_decision_revision": based_on_decision_revision,
        })

    def submit_proposal(self, run_id: str, proposal: dict) -> dict:
        return self._request("POST", self._run_path(run_id) + "/proposals", proposal)
