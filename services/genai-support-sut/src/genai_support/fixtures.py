"""合成业务事实加载器；应用启动 → load_fixture_bundle → 摘要/引用校验 → 只读快照。

只读取传入 sut 目录下的两份固定文件，不读取相邻 verification 参考答案。
这是本地校准数据与防误改检查；文件摘要不代表业务批准或运行时物理隔离。
"""

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping


FIXTURE_SHA256 = "806ead4ec1e6f8a2d801181dd99ec4134e0c80d0a163268ac222263e439f2152"
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_MAX_FILE_BYTES = 1_048_576
_COLLECTIONS = {
    "tenants": ("tenant_id", "tenant_id name"),
    "subscriptions": ("subscription_id", "subscription_id tenant_id plan contract_ref"),
    "tickets": ("ticket_id", "ticket_id tenant_id revision subscription_ref billing_record_ref received_at subject body"),
    "billing_records": ("billing_record_id", "billing_record_id tenant_id subscription_ref renewed_at amount_minor currency status"),
    "usage_records": ("usage_record_id", "usage_record_id tenant_id billing_record_ref units observed_through"),
    "contracts": ("contract_id", "contract_id tenant_id subscription_ref terms"),
    "policies": ("policy_id", "policy_id plan effective_from effective_to_exclusive refund_window_days window_semantics required_evidence eligibility_conditions text"),
}


class FixtureError(ValueError):
    """稳定错误码，避免将业务文本、文件内容或宿主路径回显到启动日志。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str = "FIXTURE_SCHEMA_INVALID") -> None:
    if not condition:
        raise FixtureError(code)


def _shape(value: Any, keys: str) -> None:
    _require(type(value) is dict and set(value) == set(keys.split()))


def _text(value: Any, *, reference: bool = False) -> None:
    _require(type(value) is str and 0 < len(value) <= 16_384)
    if reference:
        _require(_REFERENCE.fullmatch(value) is not None)


def _integer(value: Any, minimum: int = 0) -> None:
    # Python 的 bool 是 int 子类，不能将 true 当作工单 revision=1 或金额。
    _require(type(value) is int and minimum <= value <= 9_007_199_254_740_991)


def _time(value: Any) -> datetime:
    _require(type(value) is str and _TIMESTAMP.fullmatch(value) is not None)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FixtureError("FIXTURE_TIME_INVALID") from exc


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "FIXTURE_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise FixtureError("FIXTURE_SCHEMA_INVALID")


def _validate_json_depth(value: Any) -> None:
    """迭代限制嵌套层数，不依赖不同 Python JSON 解码器的递归失败阈值。"""
    pending = [(value, 0)]
    while pending:
        child, depth = pending.pop()
        _require(depth <= 16, "FIXTURE_JSON_INVALID")
        if isinstance(child, dict):
            pending.extend((item, depth + 1) for item in child.values())
        elif isinstance(child, list):
            pending.extend((item, depth + 1) for item in child)


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    _require(not path.is_symlink() and path.is_file(), "FIXTURE_PATH_INVALID")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_FILE_BYTES + 1)
    _require(len(raw) <= _MAX_FILE_BYTES, "FIXTURE_TOO_LARGE")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, FixtureError):
            raise
        raise FixtureError("FIXTURE_JSON_INVALID") from exc
    _validate_json_depth(value)
    _require(type(value) is dict)
    return value, raw


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


@dataclass(frozen=True)
class FixtureBundle:
    """已验证的不可变数据；tenant 参数必须由调用方从认证主体取得。

    get_ticket 仅提供 tenant 范围内的查找，不承担认证；未知和跨租户均返回 None。
    无任何 eligibility 计算、退款副作用或对 verification 目录的访问。
    """

    bundle_id: str
    revision: str
    content_sha256: str
    clock: str
    facts: Mapping[str, Any] = field(repr=False)

    def get_ticket(self, tenant_id: str, ticket_id: str, revision: int) -> Mapping[str, Any] | None:
        """返回同 tenant/id/revision 的只读工单；相同 ticket_id 可以属于不同租户。"""
        if type(revision) is not int:
            return None
        return next((ticket for ticket in self.facts["tickets"]
                     if (ticket["tenant_id"], ticket["ticket_id"], ticket["revision"])
                     == (tenant_id, ticket_id, revision)), None)

    def get_record(self, collection: str, tenant_id: str, record_id: str) -> Mapping[str, Any] | None:
        """工具适配器的只读记录查找入口；仅接受已注册的 tenant 业务记录集合。"""
        if collection not in {"subscriptions", "billing_records", "usage_records", "contracts"}:
            raise FixtureError("FIXTURE_COLLECTION_INVALID")
        key = _COLLECTIONS[collection][0]
        return next((row for row in self.facts[collection]
                     if row["tenant_id"] == tenant_id and row[key] == record_id), None)


def _validate_facts(facts: dict[str, Any]) -> None:
    _shape(facts, "schema_version bundle_id revision synthetic approval_state evidence_scope provenance clock "
           + " ".join(_COLLECTIONS))
    _require(type(facts["schema_version"]) is int and facts["schema_version"] == 1)
    _require(facts["bundle_id"] == "saas-support-synthetic" and facts["revision"] == "synthetic-v1")
    _require(facts["synthetic"] is True and facts["approval_state"] == "DRAFT"
             and facts["evidence_scope"] == "calibration-only")
    _text(facts["provenance"])
    _shape(facts["clock"], "now timezone")
    _require(facts["clock"]["timezone"] == "UTC")
    clock = _time(facts["clock"]["now"])
    indexes: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    for collection, (identity, keys) in _COLLECTIONS.items():
        rows = facts[collection]
        _require(type(rows) is list and 0 < len(rows) <= 128)
        index: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            _shape(row, keys)
            _text(row[identity], reference=True)
            if "tenant_id" in row:
                _text(row["tenant_id"], reference=True)
            key = (row.get("tenant_id"), row[identity])
            if collection == "tickets":
                _integer(row["revision"], 1)
                key += (row["revision"],)
            _require(key not in index, "FIXTURE_DUPLICATE_RECORD")
            index[key] = row
        indexes[collection] = index

    tenants = {row["tenant_id"] for row in facts["tenants"]}
    for row in facts["tenants"]:
        _text(row["name"])

    def linked(collection: str, owner: dict[str, Any], reference: str) -> dict[str, Any]:
        _text(reference, reference=True)
        target = indexes[collection].get((owner["tenant_id"], reference))
        _require(target is not None, "FIXTURE_REFERENCE_INVALID")
        assert target is not None
        return target

    for collection in ("subscriptions", "tickets", "billing_records", "usage_records", "contracts"):
        for row in facts[collection]:
            _require(row["tenant_id"] in tenants, "FIXTURE_REFERENCE_INVALID")
    for row in facts["subscriptions"]:
        _require(row["plan"] == "SAAS_PRO_MONTHLY")
        if row["contract_ref"] is not None:
            contract = linked("contracts", row, row["contract_ref"])
            _require(contract["subscription_ref"] == row["subscription_id"], "FIXTURE_REFERENCE_INVALID")
    for row in facts["contracts"]:
        subscription = linked("subscriptions", row, row["subscription_ref"])
        _require(subscription["contract_ref"] == row["contract_id"], "FIXTURE_REFERENCE_INVALID")
        _require(row["terms"] == "STANDARD_NO_OVERRIDE")
    for row in facts["billing_records"]:
        linked("subscriptions", row, row["subscription_ref"])
        _integer(row["amount_minor"], 1)
        _require(row["currency"] == "USD" and row["status"] == "PAID")
        _require(_time(row["renewed_at"]) <= clock, "FIXTURE_TIME_INVALID")
    usage_by_bill: dict[tuple[str, str], dict[str, Any]] = {}
    for row in facts["usage_records"]:
        billing = linked("billing_records", row, row["billing_record_ref"])
        _integer(row["units"])
        _require(_time(billing["renewed_at"]) <= _time(row["observed_through"]) <= clock, "FIXTURE_TIME_INVALID")
        key = (row["tenant_id"], row["billing_record_ref"])
        _require(key not in usage_by_bill, "FIXTURE_DUPLICATE_RECORD")
        usage_by_bill[key] = row
    for row in facts["tickets"]:
        linked("subscriptions", row, row["subscription_ref"])
        billing = linked("billing_records", row, row["billing_record_ref"])
        _require(billing["subscription_ref"] == row["subscription_ref"], "FIXTURE_REFERENCE_INVALID")
        _require(_time(billing["renewed_at"]) <= _time(row["received_at"]) <= clock, "FIXTURE_TIME_INVALID")
        usage = usage_by_bill.get((row["tenant_id"], row["billing_record_ref"]))
        # 已提供的用量事实必须覆盖工单收到时刻，避免将过早截取的零用量视为完整。
        # 缺失用量仍是合法业务输入；加载器不替后续业务逻辑决定退款或补证结论。
        if usage is not None:
            _require(_time(usage["observed_through"]) >= _time(row["received_at"]), "FIXTURE_TIME_INVALID")
        _text(row["subject"])
        _text(row["body"])
    intervals: list[tuple[datetime, datetime | None]] = []
    for policy in facts["policies"]:
        _require(policy["plan"] == "SAAS_PRO_MONTHLY")
        start = _time(policy["effective_from"])
        end = _time(policy["effective_to_exclusive"]) if policy["effective_to_exclusive"] is not None else None
        _require(start <= clock and (end is None or start < end), "FIXTURE_TIME_INVALID")
        intervals.append((start, end))
        _integer(policy["refund_window_days"], 1)
        _require(policy["refund_window_days"] <= 365)
        semantics = policy["window_semantics"]
        _shape(semantics, "timezone duration_kind start_inclusive end_inclusive request_time_field policy_time_field contract_precedence")
        _require(semantics == {"timezone": "UTC", "duration_kind": "ELAPSED_24_HOUR_DAYS",
                              "start_inclusive": True, "end_inclusive": True,
                              "request_time_field": "ticket.received_at", "policy_time_field": "billing.renewed_at",
                              "contract_precedence": "ONLY_STANDARD_NO_OVERRIDE_SUPPORTED"})
        _require(semantics["start_inclusive"] is True and semantics["end_inclusive"] is True)
        _require(policy["required_evidence"] == ["PAID_BILLING", "USAGE_RECORD", "CONTRACT_RECORD"])
        _shape(policy["eligibility_conditions"], "maximum_usage_units contract_terms")
        _integer(policy["eligibility_conditions"]["maximum_usage_units"])
        _require(policy["eligibility_conditions"] == {"maximum_usage_units": 0, "contract_terms": "STANDARD_NO_OVERRIDE"})
        _text(policy["text"])
    intervals.sort(key=lambda interval: interval[0])
    for previous, current in zip(intervals, intervals[1:]):
        _require(previous[1] is not None and previous[1] <= current[0], "FIXTURE_POLICY_OVERLAP")


def load_fixture_bundle(path: Path | str, *, expected_sha256: str = FIXTURE_SHA256) -> FixtureBundle:
    """加载固定 sut 目录，校验后返回不可变快照；不会读取相邻参考答案。

    expected_sha256 来自可信启动配置/本版本常量，不能从请求体或同目录 manifest
    自动采信；否则同时改 facts 和 manifest 就能绕过防误改检查。允许调用方明确
    传入新摘要用于审查/测试新 fixture，但仍要求 DRAFT v1 schema 和引用一致。
    失败统一抛 FixtureError，不产生数据库、网络或模型副作用。
    """
    directory = Path(path)
    try:
        _require(type(expected_sha256) is str and re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is not None,
                 "FIXTURE_DIGEST_INVALID")
        _require(directory.is_dir() and not directory.is_symlink(), "FIXTURE_PATH_INVALID")
        _require({child.name for child in directory.iterdir()} == {"manifest.json", "facts.json"}, "FIXTURE_LAYOUT_INVALID")
        manifest, _ = _read_json(directory / "manifest.json")
        _shape(manifest, "schema_version bundle_id revision facts_file facts_sha256 synthetic approval_state evidence_scope provenance")
        _require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1)
        _require(manifest["facts_file"] == "facts.json" and manifest["facts_sha256"] == expected_sha256, "FIXTURE_DIGEST_MISMATCH")
        _require(manifest["synthetic"] is True and manifest["approval_state"] == "DRAFT"
                 and manifest["evidence_scope"] == "calibration-only")
        _shape(manifest["provenance"], "kind business_policy_approval isolation expected_results_included")
        _require(manifest["provenance"] == {"kind": "SYNTHETIC_SCENARIO", "business_policy_approval": None,
                                           "isolation": "LOGICAL_ONLY_LOCAL_CALIBRATION", "expected_results_included": False})
        _require(manifest["provenance"]["expected_results_included"] is False)
        facts, raw = _read_json(directory / "facts.json")
        digest = hashlib.sha256(raw).hexdigest()
        _require(digest == expected_sha256, "FIXTURE_DIGEST_MISMATCH")
        _validate_facts(facts)
        _require(manifest["bundle_id"] == facts["bundle_id"] and manifest["revision"] == facts["revision"], "FIXTURE_IDENTITY_MISMATCH")
        return FixtureBundle(facts["bundle_id"], facts["revision"], digest, facts["clock"]["now"], _freeze(facts))
    except OSError as exc:
        raise FixtureError("FIXTURE_READ_FAILED") from exc
