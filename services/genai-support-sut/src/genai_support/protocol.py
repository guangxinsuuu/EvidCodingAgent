"""客服 HTTP 协议：路由 → parse_support_request → 共享 JSON Schema。

Schema 来自 packages/contracts/src/support.ts 的构建导出，不能由服务另定验收标准。
这里只校验消息形状与 Job/草稿关联；不证明任务持久化、政策适用或租户授权。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
import json
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator


class ProtocolValidationError(ValueError):
    """固定安全错误；API 将其映射到 422，不回显原始工单或 Schema 细节。"""

    def __init__(self) -> None:
        super().__init__("INVALID_SUPPORT_PROTOCOL")


@dataclass(frozen=True, slots=True)
class SupportRequest:
    """已校验、不可变的工单引用；tenant 必须另从认证上下文取得。"""

    schema_version: int
    ticket_id: str
    ticket_revision: int


# 模块加载时只读本包内的固定资源；不接受请求指定 schema 或远端 $ref。
_bundle = json.loads(files("genai_support").joinpath("schemas/support-v1.json").read_text(encoding="utf-8"))
if _bundle["schema_version"] != 1 or set(_bundle["schemas"]) != {"request", "submission", "job"}:
    raise RuntimeError("SUPPORT_SCHEMA_SNAPSHOT_INVALID")
_validators: dict[str, Draft202012Validator] = {}
for _kind, _schema in _bundle["schemas"].items():
    Draft202012Validator.check_schema(_schema)
    _validators[_kind] = Draft202012Validator(_schema)


def _validated(kind: str, value: Any) -> dict[str, Any]:
    """拒绝额外字段、类型转换与非法状态；错误没有包含用户值的异常链。"""
    if not _validators[kind].is_valid(value):
        raise ProtocolValidationError()
    return value


def _snapshot(value: Any) -> Any:
    """复制并深冻结输出，防止调用方在校验后修改证据引用或状态。"""
    if isinstance(value, dict):
        return MappingProxyType({key: _snapshot(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_snapshot(child) for child in value)
    return value


def parse_support_request(value: Any) -> SupportRequest:
    """接收已解析 JSON，返回工单引用；不接受 tenant、日期、政策或期望答案。

    JSON 的整数值 3.0 与 3 语义相同，与 TS/Ajv 一致；字符串及 bool 不会被转换。
    """
    row = _validated("request", value)
    return SupportRequest(schema_version=1, ticket_id=row["ticket_id"], ticket_revision=int(row["ticket_revision"]))


def validate_support_submission(value: Any) -> Mapping[str, Any]:
    """未来持久层返回值 → 202 消息形状检查；本函数自身不会保存或受理任务。"""
    return _snapshot(_validated("submission", value))


def validate_support_job(value: Any) -> Mapping[str, Any]:
    """验证 Job 状态和草稿同工单/版本不变量，返回只读独立快照。

    JSON Schema 校验 DONE/FAILED 的形状；跨字段相等约束由此处与 TS parser 共同维护。
    对应的跨语言矩阵测试确保两端同时拒绝错工单与旧版本草稿。
    """
    row = _validated("job", value)
    draft = row["draft"]
    if draft is not None and (
        draft["ticket_id"] != row["ticket_id"] or draft["ticket_revision"] != row["ticket_revision"]
    ):
        raise ProtocolValidationError()
    return _snapshot(row)
