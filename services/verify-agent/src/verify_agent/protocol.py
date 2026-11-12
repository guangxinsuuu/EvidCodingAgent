"""Python/TS 共用静态协议，不在模型 SDK 中维护第二份宽松的输出类型。"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATH = ROOT / "protocols/verify-agent/v1/decision-proposal.schema.json"


class ProtocolError(ValueError):
    """固定错误码；不把原始模型正文或请求片段转发到日志。"""


def _reject_constant(value: str) -> None:
    raise ProtocolError("NON_JSON_NUMBER")


def parse_json(raw: str | bytes) -> Any:
    """拒绝 Python json 默认接受、而 JSON/TS 不接受的 NaN 和 Infinity。"""
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeError) as error:
        raise ProtocolError("INVALID_JSON") from error


@lru_cache(maxsize=1)
def proposal_validator() -> Draft202012Validator:
    # 根目录按源码位置定位，不能通过模型传来的 schema URL 触发外部读取。
    schema = parse_json(SCHEMA_PATH.read_bytes())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_proposal(proposal: object) -> dict:
    """只做传输结构校验；引用存在性、权限、预算和候选资格由 Runtime 决定。"""
    if not proposal_validator().is_valid(proposal):
        raise ProtocolError("INVALID_DECISION_PROPOSAL")
    # 复制避免调用方后续修改已校验对象；不自行生成跨语言摘要。
    return parse_json(json.dumps(proposal, allow_nan=False))
