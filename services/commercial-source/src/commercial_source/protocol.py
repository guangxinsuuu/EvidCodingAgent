"""HTTP/命令共同的输入边界；不依赖 SUT 或其资格函数。

server → parse_command / Settings → CommercialStore。来源只承认本地实验权威，
不接受 approved=true、expected 或 eligibility 等会把实验答案混入事实的字段。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any


SOURCE_KIND = "SYNTHETIC_BUSINESS_SOURCE"
AUTHORITY_SCOPE = "LOCAL_LAB"
REVIEW_STATUSES = {"LOCAL_CALIBRATION", "PENDING_USER_REVIEW"}
COMMAND_TYPES = {
    "put_policy", "put_contract", "activate_subscription", "advance_clock",
    "cancel_renewal", "terminate_subscription", "link_invoice", "create_claim",
    "supplement_claim", "open_coverage", "record_usage_sent", "record_usage_received",
    "reconcile_coverage", "seal_coverage", "record_payment",
}


class SourceError(Exception):
    """仅用固定错误码跨 HTTP 边界；不把原始命令、凭据或数据库异常回传。"""

    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise SourceError("INVALID_TIMESTAMP") from None


def instant(value: Any) -> str:
    return timestamp(value).isoformat()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise SourceError("INVALID_IDENTIFIER")
    return value


def positive_int(value: Any) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise SourceError("INVALID_POSITIVE_INTEGER")
    return value


def number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SourceError("INVALID_NONNEGATIVE_NUMBER")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal("1e24"):
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise SourceError("INVALID_NONNEGATIVE_NUMBER") from None


def period(start: Any, end: Any) -> tuple[str, str]:
    left, right = timestamp(start), timestamp(end)
    if right < left:
        raise SourceError("INVALID_PERIOD")
    return left.isoformat(), right.isoformat()


def reference(value: Any, kind: str) -> dict:
    if not isinstance(value, dict) or set(value) != {kind + "_id", "version"}:
        raise SourceError("INVALID_REFERENCE")
    identifier(value[kind + "_id"])
    if value["version"] is not None:
        identifier(value["version"])
    return dict(value)


def _reject_answers(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"eligibility", "expected", "expected_eligibility", "approved"}:
                raise SourceError("DECISION_OR_APPROVAL_FIELD_FORBIDDEN")
            _reject_answers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_answers(item)


def parse_command(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"operation_key", "type", "payload"}:
        raise SourceError("INVALID_COMMAND")
    identifier(value["operation_key"])
    if not isinstance(value["type"], str) or value["type"] not in COMMAND_TYPES or not isinstance(value["payload"], dict):
        raise SourceError("INVALID_COMMAND")
    identifier(value["payload"].get("tenant_id"))
    _reject_answers(value["payload"])
    return value


def source_document(payload: dict) -> dict:
    """原文和结构摘要必须绑定同一 UTF-8 摘要；这不是法律解释或公司批准。"""
    document = payload.get("source_document")
    if not isinstance(document, dict) or not isinstance(document.get("text"), str) or not document["text"]:
        raise SourceError("SOURCE_DOCUMENT_REQUIRED")
    identifier(document.get("reference"))
    identifier(document.get("origin"))
    expected = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
    if document.get("sha256") != expected or payload.get("structured_source_sha256") != expected:
        raise SourceError("SOURCE_DOCUMENT_HASH_MISMATCH")
    if payload.get("review_status") not in REVIEW_STATUSES:
        raise SourceError("INVALID_REVIEW_STATUS")
    return dict(document)


@dataclass(frozen=True)
class Settings:
    """宿主私有配置：服务 token 只给出租户集合，不接受请求自报权限。"""

    database_url: str
    admin_token: str
    read_token: str
    tenant_scopes: tuple[str, ...]
    instance_id: str
    expires_at: str
    port: int = 0
    read_database_url: str | None = None

    def __post_init__(self):
        for token in (self.admin_token, self.read_token):
            if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
                raise SourceError("INVALID_SERVICE_TOKEN")
        if self.admin_token == self.read_token:
            raise SourceError("SERVICE_ROLES_MUST_DIFFER")
        if not self.tenant_scopes or len(set(self.tenant_scopes)) != len(self.tenant_scopes):
            raise SourceError("TENANT_SCOPES_REQUIRED")
        for tenant in self.tenant_scopes:
            identifier(tenant)
        identifier(self.instance_id)
        timestamp(self.expires_at)
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise SourceError("INVALID_PORT")
        if not isinstance(self.database_url, str) or not self.database_url:
            raise SourceError("DATABASE_REQUIRED")

    @classmethod
    def from_private_file(cls, path: str) -> "Settings":
        """O_NOFOLLOW + fstat 防止跟随凭据软链接；错误不得输出文件内容。"""
        descriptor = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            mode = os.fstat(handle.fileno())
            if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
                raise SourceError("PRIVATE_CONFIG_MODE_REQUIRED")
            value = json.load(handle)
        return cls(database_url=value["database_url"], read_database_url=value.get("read_database_url"),
                   admin_token=value["admin_token"], read_token=value["read_token"],
                   tenant_scopes=tuple(value["tenant_scopes"]), instance_id=value["instance_id"],
                   expires_at=value["expires_at"], port=value.get("port", 0))
