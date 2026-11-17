"""可信纯 fixture 装配器：能力绑定 → 固定合同 → 类型检查 → 独立输入副本。

本模块不导入产品函数、不执行候选、不连接网络/数据库，也不生成期望结果。
实际执行需要另一层经过授权的隔离 Runner；prepare_probe 只返回可追溯的输入。
产品输出形状校验使用 validate_result，不能据此签发独立业务 PASS。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "protocols/verify-agent/capabilities/commercial-decision.v1.json"
CAPABILITY = "probe.commercial_decision"
SCENARIO = "commercial_unit"
STAGE = "commercial_unit.evaluate"
SOURCE_PATH = "services/genai-support-sut/src/genai_support/integrations/commercial.py"
DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")


class FixtureBindingError(ValueError):
    """稳定错误码不回显模型输入、路径或潜在敏感值。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise FixtureBindingError(code)


def canonical_bytes(value: Any) -> bytes:
    """固定为 UTF-8、码点排序键、无空白、数组保序；fixture 的所有键为 ASCII。

    数值只有安全整数，拒绝 NaN/Infinity。该编码用于数据身份，不能用摘要证明业务正确。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _path_value(context: dict, name: str) -> tuple[dict | list, str | int]:
    """只解析固定合同中已登记的 dotpath；路径不用于 Python 属性、文件或代码执行。"""
    parts = name.split(".")
    parent: Any = context
    for part in parts[:-1]:
        if isinstance(parent, dict) and part in parent:
            parent = parent[part]
        elif isinstance(parent, list) and part.isdigit() and int(part) < len(parent):
            parent = parent[int(part)]
        else:
            raise FixtureBindingError("REGISTERED_FIXTURE_PATH_MISSING")
    key: str | int = int(parts[-1]) if isinstance(parent, list) and parts[-1].isdigit() else parts[-1]
    _require(isinstance(parent, dict) and key in parent or
             isinstance(parent, list) and isinstance(key, int) and key < len(parent), "REGISTERED_FIXTURE_PATH_MISSING")
    return parent, key


def load_contract() -> dict:
    """读取唯一受信合同，并核对基础 fixture 与已审源码摘要；调用者不能换路径/函数。"""
    _require(not CONTRACT_PATH.is_symlink(), "CAPABILITY_CONTRACT_SYMLINK")
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    _require(contract.get("schema_version") == 1 and contract.get("id") == "commercial-decision.v1"
             and contract.get("capability_ref") == CAPABILITY and contract.get("scenario_ref") == SCENARIO
             and contract.get("stage_ref") == STAGE and contract.get("execution_mode") == "PURE_FIXTURE"
             and contract.get("dispatch_enabled") is False, "CAPABILITY_CONTRACT_IDENTITY_INVALID")
    _require(contract.get("entrypoint") == {"path": SOURCE_PATH, "symbol": "commercial_decision"}, "CAPABILITY_ENTRYPOINT_INVALID")
    fixture = contract["fixture"]
    _require(fixture.get("hash_encoding") == "canonical-json-sort-keys-utf8-v1"
             and sha256(canonical_bytes(fixture["context"])).hexdigest() == fixture.get("sha256"), "TRUSTED_FIXTURE_DIGEST_MISMATCH")
    _require(sha256((ROOT / SOURCE_PATH).read_bytes()).hexdigest() == contract.get("source_sha256"), "TRUSTED_SOURCE_DIGEST_MISMATCH")
    for parameter in contract["inputs"]:
        _path_value(fixture["context"], parameter["name"])
    return contract


def _validate_input(value: Any, parameter: dict) -> None:
    kind = parameter["type"]
    valid = (kind == "string" and isinstance(value, str)) or (kind == "integer" and type(value) is int) \
        or (kind == "boolean" and type(value) is bool)
    _require(valid, "INPUT_TYPE_INVALID")
    if "enum" in parameter:
        _require(value in parameter["enum"], "INPUT_ENUM_INVALID")
    if kind == "integer":
        _require(abs(value) <= 9007199254740991 and parameter.get("minimum", value) <= value <= parameter.get("maximum", value), "INPUT_RANGE_INVALID")
    if parameter.get("format") == "date-time":
        _require(DATE_TIME.fullmatch(value) is not None, "INPUT_DATE_TIME_INVALID")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            _require(parsed.tzinfo is not None, "INPUT_DATE_TIME_INVALID")
        except ValueError:
            raise FixtureBindingError("INPUT_DATE_TIME_INVALID") from None


def prepare_probe(payload: dict) -> dict:
    """装配一个 primary 或 control 条件；每次从原始 fixture 深拷贝，互不污染。

    payload 的唯一外部参数是固定能力/场景/阶段与已类型化 inputs。不会猜测 string
    的布尔/数值含义，也不会为使结果通过而联动重写合同、期望或其他依赖事实。
    返回值包含全部输入及摘要，但不意味着阶段前提已满足、实验已执行或获准派发。
    """
    _require(isinstance(payload, dict) and set(payload) == {"capability_ref", "scenario_ref", "stage_ref", "inputs"}, "PROBE_PAYLOAD_FIELDS_INVALID")
    _require(payload["capability_ref"] == CAPABILITY and payload["scenario_ref"] == SCENARIO
             and payload["stage_ref"] == STAGE, "PROBE_BINDING_NOT_REGISTERED")
    contract = load_contract()
    definitions = {row["name"]: row for row in contract["inputs"]}
    inputs = payload["inputs"]
    _require(isinstance(inputs, list) and len(inputs) <= len(definitions), "PROBE_INPUTS_INVALID")
    context = deepcopy(contract["fixture"]["context"])
    seen = set()
    for row in inputs:
        _require(isinstance(row, dict) and set(row) == {"name", "value"} and isinstance(row["name"], str), "PROBE_INPUT_FIELDS_INVALID")
        name = row["name"]
        _require(name in definitions, "INPUT_NOT_REGISTERED")
        _require(name not in seen, "DUPLICATE_INPUT")
        seen.add(name)
        _validate_input(row["value"], definitions[name])
        parent, key = _path_value(context, name)
        parent[key] = row["value"]
    return {"schema_version": 1, "contract_id": contract["id"], "capability_ref": CAPABILITY,
            "scenario_ref": SCENARIO, "stage_ref": STAGE, "execution_mode": "PURE_FIXTURE",
            "fixture_id": contract["fixture"]["id"], "fixture_sha256": contract["fixture"]["sha256"],
            "source_sha256": contract["source_sha256"], "applied_inputs": deepcopy(inputs),
            "context": context, "context_sha256": sha256(canonical_bytes(context)).hexdigest(),
            "stage_prerequisites": deepcopy(contract["stage_prerequisites"]),
            "prerequisite_status": "NOT_CHECKED", "dispatch_qualified": False,
            "evidence_scope": "SYNTHETIC_FIXTURE_INPUT_ONLY"}


def validate_result(result: dict) -> dict:
    """核对实际函数返回的类型与枚举；输出自述不升级为独立观察、断言或 PASS。"""
    contract = load_contract()
    outputs = {row["field"].removeprefix("decision."): row for row in contract["outputs"]}
    _require(isinstance(result, dict) and set(result) == set(outputs), "PRODUCT_OUTPUT_FIELDS_INVALID")
    for name, descriptor in outputs.items():
        value = result[name]
        if value is None:
            _require(descriptor.get("nullable") is True, "PRODUCT_OUTPUT_NULL_INVALID")
            continue
        kind = descriptor["type"]
        valid = kind == "string" and isinstance(value, str) or kind == "boolean" and type(value) is bool \
            or kind == "number" and type(value) in {int, float} and math.isfinite(value) \
            or kind == "string[]" and isinstance(value, list) and all(isinstance(item, str) for item in value)
        _require(valid, "PRODUCT_OUTPUT_TYPE_INVALID")
        if "enum" in descriptor:
            _require(value in descriptor["enum"], "PRODUCT_OUTPUT_ENUM_INVALID")
    return {"decision": deepcopy(result), "schema_conformance": "VALID",
            "evidence_scope": "PRODUCT_FUNCTION_OUTPUT_ONLY", "business_verdict": "NOT_ESTABLISHED",
            "cross_system_coverage": False, "release_authorized": False}
