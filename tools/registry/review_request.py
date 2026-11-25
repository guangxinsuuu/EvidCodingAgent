"""正式审查请求的有界读取与共享协议校验。

可信操作员 → read_review_request / validate_review_request → 发布材料装配器。
请求描述任务和本地确认状态；它不是企业批准、来源 ACL 或实验执行授权。
只有安装侧共享 Schema 决定格式，请求中的字段不能指定新的文件或网络读取。
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import os
from pathlib import Path
import stat

from jsonschema import Draft202012Validator

from .reference_resolver import MaterialError


MAX_REQUEST_BYTES = 128 * 1024
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "protocols/review-request/v1/review-request.schema.json"


def _reject_constant(_value: str):
    raise MaterialError("REVIEW_REQUEST_JSON_INVALID")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """拒绝重复键；否则 Python/TS 对请求身份的理解可能取决于解析器。"""
    value = {}
    for key, item in pairs:
        if key in value:
            raise MaterialError("REVIEW_REQUEST_DUPLICATE_KEY")
        value[key] = item
    return value


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    """仅加载仓库安装侧协议；候选仓库不能替换它的验收格式。"""
    if SCHEMA_PATH.is_symlink():
        raise MaterialError("REVIEW_REQUEST_SCHEMA_INVALID")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_review_request(value: object) -> dict:
    """返回隔离副本；错误只暴露稳定码，不回显需求正文或来源信息。

    Schema 校验结构、类型和边界；这里另外核对需求/缺项身份唯一性及引用。
    来源声明是否可信、要求是否可用于规划，由后续 Runtime 独立判断。
    """
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise MaterialError("REVIEW_REQUEST_INVALID") from None
    if len(encoded) > MAX_REQUEST_BYTES or not _validator().is_valid(value):
        raise MaterialError("REVIEW_REQUEST_INVALID")
    refs = [row["id"] for row in value["requirements"]]
    gaps = [row["id"] for row in value["known_gaps"]]
    if len(refs) != len(set(refs)) or len(gaps) != len(set(gaps)):
        raise MaterialError("REVIEW_REQUEST_DUPLICATE_ID")
    if any(not set(row["requirement_refs"]) <= set(refs) for row in value["known_gaps"]):
        raise MaterialError("REVIEW_REQUEST_UNKNOWN_REQUIREMENT_REF")
    return deepcopy(value)


def read_review_request(path: Path) -> dict:
    """读取一个普通 JSON 文件；拒绝符号链接、FIFO、超大文件和歧义 JSON。

    用同一文件描述符检查类型并读取，避免 stat/open 之间替换文件的窗口。
    O_NONBLOCK 确保误传命名管道时也不会在类型检查前无限等待。
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise MaterialError("REVIEW_REQUEST_REGULAR_FILE_REQUIRED")
            if info.st_size > MAX_REQUEST_BYTES:
                raise MaterialError("REVIEW_REQUEST_TOO_LARGE")
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise MaterialError("REVIEW_REQUEST_TOO_LARGE")
    except OSError:
        raise MaterialError("REVIEW_REQUEST_READ_FAILED") from None
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                           object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise MaterialError("REVIEW_REQUEST_JSON_INVALID") from None
    return validate_review_request(value)
