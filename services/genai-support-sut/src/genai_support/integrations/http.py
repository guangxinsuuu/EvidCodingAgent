"""外部连接器 → JsonClient → 固定服务地址；不重定向、不隐式重试写操作。

审计只保留路径、非秘密查询参数与响应摘要，不记录认证头、表单或原始错误体。
这层记录是产品调用轨迹，仍不等同于独立 Observer 的可信裁决。
"""

from collections.abc import Callable, Mapping
from hashlib import sha256
from http.client import HTTPException
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class IntegrationError(RuntimeError):
    """固定错误码；UNKNOWN 表示写请求可能已生效，调用者必须对账而非再次 POST。"""

    def __init__(self, code: str, status: int = 503, outcome: str = "UNAVAILABLE") -> None:
        self.code, self.status, self.outcome = code, status, outcome
        super().__init__(code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def segment(value: str | int) -> str:
    """把单个原生对象 ID 编码成路径片段，禁止调用方注入下一层路径或查询。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not 1 <= len(str(value)) <= 256:
        raise IntegrationError("INVALID_EXTERNAL_ID", 422, "REJECTED")
    if any(ord(char) < 32 for char in str(value)) or str(value) in {".", ".."}:
        raise IntegrationError("INVALID_EXTERNAL_ID", 422, "REJECTED")
    return quote(str(value), safe="")


def object_field(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get(field), dict):
        raise IntegrationError("EXTERNAL_RESPONSE_INVALID")
    return value[field]


def list_field(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get(field), list) \
            or not all(isinstance(row, dict) for row in value[field]):
        raise IntegrationError("EXTERNAL_RESPONSE_INVALID")
    return value[field]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """外部身份/对象字段不允许靠重复 JSON 键悄悄覆盖。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("NONFINITE_JSON_NUMBER")


class JsonClient:
    """只接受部署配置中的 origin；HTTP 仅允许本机测试地址，远程接入须 HTTPS。

    request 由领域 adapter 调用。write=True 的超时、5xx、损坏回执都保留 UNKNOWN，
    不把未知写入判成未执行；4xx 是明确拒绝。每个响应最多 2 MiB。
    """

    def __init__(self, base_url: str, headers: Mapping[str, str], service: str,
                 audit: Callable[[dict[str, Any]], None] | None = None, *, timeout: float = 15) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password \
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"} \
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise IntegrationError("EXTERNAL_URL_INVALID", 500, "REJECTED")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", service) or not 0 < timeout <= 60:
            raise IntegrationError("EXTERNAL_CONFIG_INVALID", 500, "REJECTED")
        self.base_url, self.service, self.audit = base_url.rstrip("/"), service, audit
        self._headers, self.timeout = dict(headers), timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, *, query: Mapping[str, Any] | None = None,
                json_body: Any = None, form: Mapping[str, str] | None = None, write: bool = False) -> dict[str, Any]:
        if method not in {"GET", "POST", "PUT", "DELETE"} or not path.startswith("/") \
                or path.startswith("//") or "?" in path or "#" in path \
                or (json_body is not None and form is not None):
            raise IntegrationError("EXTERNAL_REQUEST_INVALID", 500, "REJECTED")
        headers = {"Accept": "application/json", **self._headers}
        payload = None
        if form is not None:
            payload, headers["Content-Type"] = urlencode(form).encode(), "application/x-www-form-urlencoded"
        elif json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=False, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        if payload is not None and len(payload) > 262_144:
            raise IntegrationError("EXTERNAL_REQUEST_TOO_LARGE", 422, "REJECTED")
        url = self.base_url + path + ("?" + urlencode(query, doseq=True) if query else "")
        event = {"service": self.service, "method": method, "path": path, "query": dict(query or {})}
        try:
            with self._opener.open(Request(url, data=payload, headers=headers, method=method), timeout=self.timeout) as response:
                raw = response.read(2_097_153)
                event["http_status"] = response.status
                if len(raw) > 2_097_152:
                    raise ValueError("OVERSIZE")
                value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                if not isinstance(value, dict):
                    raise ValueError("OBJECT_REQUIRED")
                event.update(outcome="OBSERVED", response_sha256=sha256(raw).hexdigest())
        except HTTPError as exc:
            event["http_status"] = exc.code
            outcome = "UNKNOWN" if write and exc.code >= 500 else "REJECTED" if exc.code < 500 else "UNAVAILABLE"
            exc.close()
            event["outcome"] = outcome
            if self.audit:
                self.audit(event)
            raise IntegrationError("EXTERNAL_HTTP_REJECTED" if outcome == "REJECTED" else "EXTERNAL_RECEIPT_UNKNOWN"
                                   if outcome == "UNKNOWN" else "EXTERNAL_UNAVAILABLE", 503, outcome) from None
        except (URLError, OSError, TimeoutError, HTTPException, ValueError, UnicodeError, RecursionError):
            outcome = "UNKNOWN" if write else "UNAVAILABLE"
            event["outcome"] = outcome
            if self.audit:
                self.audit(event)
            raise IntegrationError("EXTERNAL_RECEIPT_UNKNOWN" if write else "EXTERNAL_UNAVAILABLE", 503, outcome) from None
        if self.audit:
            # 证据落盘失败必须向上传播，不能让未被记录的外部效果静默成功。
            self.audit(event)
        return value
