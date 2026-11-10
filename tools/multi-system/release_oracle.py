"""当前商业来源增量版本的独立规则检查；预期永远留在验证侧。

release_check → assess_case(准备事实, Job, 冻结快照, 原生读回) → 断言与候选Finding。
这里不导入任何 genai_support 产品模块，不调用产品资格函数，也不以模型判断PASS。
固定期望来自 Business Source Pack v1 的 R1–R6，准备事实只提供对象身份和时间；
原生观察必须实际由驱动取得。该本地校准器尚不是受保护、签名的企业 Final Oracle。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any


class OracleInputError(ValueError):
    """验证器缺输入与产品FAIL不同；调用方应记为准备/观察不足，不伪造业务结论。"""


@dataclass(frozen=True)
class Expectation:
    eligibility: str
    action: str
    policy_version: str | None = None
    window_seconds: int | None = None
    elapsed_seconds: int | None = None
    missing: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    native_units: str | None = "0"
    selected_units: str | None = "0"


# 这里只是测试矩阵。任何调用来源服务的工具都不应导入或序列化这个表到业务输入。
EXPECTATIONS = {
    "v1_history": Expectation("ELIGIBLE", "HUMAN_REVIEW", "lab-v1", 14 * 86400, 10 * 86400, rules=("R1", "R2", "R3", "R4")),
    "v2_boundary": Expectation("ELIGIBLE", "HUMAN_REVIEW", "lab-v2", 7 * 86400, 7 * 86400, rules=("R1", "R2")),
    "v2_late": Expectation("INELIGIBLE", "NO_ACTION", "lab-v2", 7 * 86400, 7 * 86400 + 1, rules=("R2",)),
    "expired_contract": Expectation("NEEDS_MORE_EVIDENCE", "REQUEST_INFORMATION", missing=("APPLICABLE_CONTRACT",), rules=("R1",)),
    "draft_contract": Expectation("NEEDS_MORE_EVIDENCE", "REQUEST_INFORMATION", missing=("APPLICABLE_CONTRACT",), rules=("R1",)),
    "special_contract": Expectation("NEEDS_MORE_EVIDENCE", "HUMAN_REVIEW", missing=("SPECIAL_CONTRACT_TERMS",), rules=("R1", "R5")),
    "cancelled_renewal": Expectation("NEEDS_MORE_EVIDENCE", "REQUEST_INFORMATION", missing=("RENEWAL_EVENT",), rules=("R1",), native_units=None, selected_units=None),
    "terminated_subscription": Expectation("NEEDS_MORE_EVIDENCE", "REQUEST_INFORMATION", missing=("RENEWAL_EVENT",), rules=("R1", "R4"), native_units=None, selected_units=None),
    "late_usage": Expectation("NEEDS_MORE_EVIDENCE", "REQUEST_INFORMATION", "lab-v2", 7 * 86400, 2 * 86400,
        missing=("USAGE_COMPLETENESS",), rules=("R3",), selected_units=None),
    "post_claim_usage": Expectation("ELIGIBLE", "HUMAN_REVIEW", "lab-v2", 7 * 86400, 2 * 86400, rules=("R3",), native_units="1"),
    "overpaid": Expectation("NEEDS_MORE_EVIDENCE", "REQUEST_INFORMATION", "lab-v2", 7 * 86400,
        missing=("SIMULATED_PAYMENT_FACT",), rules=("R4",)),
    "wrong_currency": Expectation("NEEDS_MORE_EVIDENCE", "REQUEST_INFORMATION", "lab-v2", 7 * 86400,
        missing=("SIMULATED_PAYMENT_FACT",), rules=("R4",)),
}
LATE_USAGE_AFTER_SEAL = Expectation("INELIGIBLE", "NO_ACTION", "lab-v2", 7 * 86400, 2 * 86400,
                                 rules=("R3", "R6"), native_units="1", selected_units="1")


def expectation_for(label: str, stage: str = "initial") -> Expectation:
    """未知场景/阶段必须拒绝，不能悄悄选默认期望而得到假PASS。"""
    if label not in EXPECTATIONS or stage not in {"initial", "after_seal", "supplement"}:
        raise OracleInputError("UNKNOWN_SCENARIO_OR_STAGE")
    if stage == "after_seal":
        if label != "late_usage":
            raise OracleInputError("STAGE_NOT_APPLICABLE")
        return LATE_USAGE_AFTER_SEAL
    if stage == "supplement" and label != "v1_history":
        raise OracleInputError("STAGE_NOT_APPLICABLE")
    return EXPECTATIONS[label]


def expectation_manifest() -> dict:
    """供报告冻结规则矩阵；不能作为来源接口 payload。"""
    return {"qualification": "LOCAL_INDEPENDENT_RULE_CHECK", "rules_ref": "business-source-pack/v1/SPEC.md",
            "scenarios": {name: asdict(value) for name, value in EXPECTATIONS.items()},
            "stages": {"late_usage:after_seal": asdict(LATE_USAGE_AFTER_SEAL)}}


def _time(value: Any) -> datetime:
    if type(value) in (int, float):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str):
        raise OracleInputError("OBSERVATION_TIMESTAMP_MISSING")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError
        return instant.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise OracleInputError("OBSERVATION_TIMESTAMP_INVALID") from None


def _number(value: Any) -> Decimal:
    try:
        if value is None or isinstance(value, bool):
            raise InvalidOperation
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError):
        raise OracleInputError("OBSERVATION_UNITS_INVALID") from None


def _usage(raw: dict | None, metric: str) -> Decimal | None:
    """独立读回指定原生指标；缺失/多行均不是零，也不按顶层units解释。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("charges_usage"), list):
        return None
    rows = [row for row in raw["charges_usage"] if isinstance(row, dict)
            and isinstance(row.get("billable_metric"), dict) and row["billable_metric"].get("code") == metric]
    if len(rows) != 1 or rows[0].get("unit", "units") != "units":
        return None
    return _number(rows[0].get("units"))


def wording_probes(text: str, eligibility: str) -> list[dict]:
    """词汇探针只给待复核Finding，不声称涵盖全部自然语言或法律语义。

    不把没有命中当语义PASS，也不以短语命中直接判业务失败；否定词、引用、语境
    都可能影响解释。确定性结果与私有备注边界由 assess_case 的硬断言负责。
    """
    if not isinstance(text, str):
        return [{"code": "WORDING_MISSING", "status": "REVIEW_REQUIRED", "basis": "draft text missing"}]
    findings = []
    if not any(token in text for token in ("本地实验", "实验规则", "实验室", "模拟资料", "模拟付款")):
        findings.append({"code": "RAW_DRAFT_LAB_SCOPE_NOT_EXPLICIT", "status": "REVIEW_REQUIRED",
                         "basis": "原始模型文案未命中实验/模拟范围词；私有备注包装需另行核验"})
    if eligibility == "NEEDS_MORE_EVIDENCE" and any(token in text for token in ("似乎符合", "初步符合", "符合退款标准", "满足退款条件")):
        findings.append({"code": "ELIGIBILITY_HINT_WITH_MISSING_EVIDENCE", "status": "REVIEW_REQUIRED",
                         "basis": "缺证结果与文案中的资格暗示需要独立人工语义复核"})
    if re.search(r"(?:已|已经)(?:完成|办理|发起|处理)(?:了)?退款|退款(?:已|已经)(?:到账|完成|发起)", text):
        findings.append({"code": "REFUND_EXECUTION_WORDING", "status": "REVIEW_REQUIRED",
                         "basis": "命中退款已执行措辞；该产品只允许内部建议"})
    return findings


def _candidate_shape_errors(result: dict, context: Any, label: str) -> list[str]:
    """候选已返回坏协议/坏业务值应是FAIL，不能伪装成观察器缺输入。

    这里只检查本场景后续硬断言会使用的字段；取消/终止的空claim是合法负例。
    原生API读回仍由assess_case单独检查，不能将依赖缺响应归咎于候选。
    """
    failures = []
    job = result.get("job")
    if not isinstance(job, dict) or not isinstance(result.get("delivery"), dict):
        return ["job_or_delivery_shape"]
    draft = job.get("draft")
    if not isinstance(draft, dict) or not {"eligibility", "proposed_action", "applicable_policy_ref", "customer_reply_draft"}.issubset(draft):
        failures.append("draft_fields")
    if not isinstance(context, dict):
        return failures + ["snapshot_object"]
    for key in ("billing", "subscription", "commercial_source"):
        if not isinstance(context.get(key), dict):
            failures.append(key + "_shape")
    if not isinstance(context.get("missing_evidence"), list) or any(not isinstance(row, str) for row in context.get("missing_evidence", [])):
        failures.append("missing_evidence_shape")
    for key in ("policy", "contract"):
        if context.get(key) is not None and not isinstance(context[key], dict):
            failures.append(key + "_shape")
    source = context.get("commercial_source")
    if not isinstance(source, dict):
        return failures
    if not isinstance(source.get("scope"), dict):
        failures.append("source_scope_shape")
    for key in ("events", "contracts", "policies"):
        if not isinstance(source.get(key), list) or any(not isinstance(row, dict) for row in source.get(key, [])):
            failures.append("source_" + key + "_shape")
    if isinstance(source.get("events"), list):
        if any(not isinstance(row, dict) or not isinstance(row.get("invoice_refs"), list) for row in source["events"]):
            failures.append("event_invoice_refs_shape")
    for key in ("claim", "coverage", "payment", "invoice_link"):
        if source.get(key) is not None and not isinstance(source[key], dict):
            failures.append("source_" + key + "_shape")
    if label not in {"cancelled_renewal", "terminated_subscription"}:
        claim = source.get("claim")
        try:
            if not isinstance(claim, dict) or not isinstance(claim.get("original_message"), dict):
                raise OracleInputError("CLAIM_REQUIRED")
            _time(claim.get("claimed_at"))
            _time(claim["original_message"].get("created_at"))
        except OracleInputError:
            failures.append("claim_business_timestamp_or_reference")
    coverage = source.get("coverage")
    if isinstance(coverage, dict):
        try:
            _time(coverage.get("period_start"))
            _time(coverage.get("period_end"))
            if not isinstance(coverage.get("expected_events"), list):
                raise OracleInputError("EXPECTED_EVENTS_REQUIRED")
            for row in coverage["expected_events"]:
                if not isinstance(row, dict) or not isinstance(row.get("event_id"), str):
                    raise OracleInputError("EVENT_SHAPE")
                _time(row.get("occurred_at"))
                _number(row.get("units"))
            for key in ("sent_event_ids", "received_event_ids"):
                if not isinstance(coverage.get(key), list) or any(not isinstance(row, str) for row in coverage.get(key, [])):
                    raise OracleInputError("EVENT_IDS_SHAPE")
            _number(coverage.get("units"))
        except OracleInputError:
            failures.append("coverage_business_values")
    try:
        _usage(context.get("usage"), "__shape_probe_only__")
        # 指标身份未知也逐行校验已报告数值类型；不是用这些数值重新判资格。
        usage = context.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("charges_usage"), list):
            for row in usage["charges_usage"]:
                if not isinstance(row, dict):
                    raise OracleInputError("USAGE_ROW_SHAPE")
                _number(row.get("units"))
        if label == "expired_contract" and isinstance(source.get("contracts"), list):
            for row in source["contracts"]:
                if isinstance(row, dict):
                    _time(row.get("effective_from"))
                    if row.get("effective_to") is not None:
                        _time(row["effective_to"])
    except OracleInputError:
        failures.append("candidate_business_values")
    return failures


def assess_case(case: dict, result: dict, context: dict, observations: dict, *, stage: str = "initial") -> dict:
    """核验一个已完成Job及其原生读回，不运行产品决策或发送任何请求。

    case是准备器的事实对象，必须给出完整六项身份、metric_code、period_start；
    observations要求messages、conversation、original_message、invoice、usage。
    可选source_context用于商业API独立读回与冻结快照交叉核对。所有FAIL返回集合，
    根驱动决定是否继续其余独立案例；关键观察缺失抛OracleInputError。
    """
    required_case = {"label", "tenant_id", "customer_id", "subscription_id", "invoice_id",
                     "account_id", "conversation_id", "metric_code", "period_start"}
    if not required_case.issubset(case) or not {"messages", "conversation", "original_message", "invoice", "usage"}.issubset(observations):
        raise OracleInputError("COMPLETE_CASE_AND_NATIVE_OBSERVATIONS_REQUIRED")
    expected = expectation_for(case["label"], stage)
    # 观察器自己的输入先验证；有完整原生观察时，候选字段损坏明确记业务验证FAIL。
    if any(not isinstance(observations[key], dict) for key in ("conversation", "original_message", "invoice")) \
            or not isinstance(observations["messages"], list):
        raise OracleInputError("NATIVE_OBSERVATION_SHAPE_INVALID")
    errors = _candidate_shape_errors(result, context, case["label"])
    if errors:
        return {"qualification": "LOCAL_INDEPENDENT_RULE_CHECK", "label": case["label"], "stage": stage,
            "status": "FAIL", "job_id": (result.get("job") or {}).get("job_id") if isinstance(result.get("job"), dict) else None,
            "checks": [{"name": "candidate_protocol_and_business_value_shape", "status": "FAIL", "evidence": {"invalid_fields": errors}}],
            "findings": [], "semantic_quality_status": "NOT_FULLY_EVALUATED"}
    job, delivery = result["job"], result["delivery"]
    draft = job.get("draft") or {}
    source = context.get("commercial_source") or {}
    checks = []

    def check(name: str, condition: bool, **evidence):
        checks.append({"name": name, "status": "PASS" if condition else "FAIL", "evidence": evidence})

    check("job_completed", job.get("status") == "DONE", actual=job.get("status"))
    check("eligibility", draft.get("eligibility") == expected.eligibility,
          expected=expected.eligibility, actual=draft.get("eligibility"), rules=list(expected.rules))
    check("proposed_action", draft.get("proposed_action") == expected.action,
          expected=expected.action, actual=draft.get("proposed_action"))
    missing = context.get("missing_evidence", [])
    check("required_missing_evidence", all(key in missing for key in expected.missing),
          required=list(expected.missing), actual=missing)
    if expected.eligibility != "NEEDS_MORE_EVIDENCE":
        check("complete_case_has_no_missing_evidence", missing == [], actual=missing)
    scope = {key: case[key] for key in ("tenant_id", "customer_id", "subscription_id", "invoice_id", "account_id", "conversation_id")}
    check("commercial_scope_matches_prepared_objects", source.get("scope") == scope, scope=scope)
    check("explicit_lab_authority", source.get("authority_scope") == "LOCAL_LAB"
          and source.get("source_kind") == "SYNTHETIC_BUSINESS_SOURCE")
    check("snapshot_invoice_and_subscription", (context.get("billing") or {}).get("invoice_id") == case["invoice_id"]
          and (context.get("subscription") or {}).get("external_subscription_id") == case["subscription_id"])
    invoice = observations["invoice"]
    native_subs = invoice.get("subscriptions", []) if isinstance(invoice, dict) else []
    check("native_invoice_ownership", isinstance(invoice, dict) and invoice.get("lago_id") == case["invoice_id"]
          and (invoice.get("customer") or {}).get("external_id") == case["customer_id"]
          and len(native_subs) == 1 and native_subs[0].get("external_id") == case["subscription_id"])
    if expected.policy_version is not None:
        policy = context.get("policy") or {}
        check("historical_policy_selection", draft.get("applicable_policy_ref") == "refund-standard:" + expected.policy_version
              and policy.get("version") == expected.policy_version, expected=expected.policy_version,
              actual=draft.get("applicable_policy_ref"))
        check("policy_window_seconds", (policy.get("terms") or {}).get("window_seconds") == expected.window_seconds,
              expected=expected.window_seconds, actual=(policy.get("terms") or {}).get("window_seconds"))
    linked_renewals = [event for event in source.get("events", []) if event.get("event_type") == "SUBSCRIPTION_RENEWAL_EFFECTIVE"
                       and case["invoice_id"] in event.get("invoice_refs", [])]
    if case["label"] in {"cancelled_renewal", "terminated_subscription"}:
        check("no_successful_renewal_for_invoice", len(linked_renewals) == 0
              and (context.get("billing") or {}).get("renewed_at") is None)
    else:
        check("exactly_one_state_derived_renewal", len(linked_renewals) == 1
              and type(linked_renewals[0].get("term_number")) is int and linked_renewals[0]["term_number"] >= 2)
        claim = source.get("claim") or {}
        original = observations["original_message"]
        recorded_original = claim.get("original_message") or {}
        check("original_claim_message_identity", recorded_original.get("message_id") == original.get("id")
              and recorded_original.get("content_sha256") == hashlib.sha256((original.get("content") or "").encode()).hexdigest()
              and _time(recorded_original.get("created_at")) == _time(original.get("created_at")))
        if "claimed_at" not in case:
            raise OracleInputError("PREPARED_CLAIM_TIME_REQUIRED")
        check("claim_time_is_prepared_original", _time(claim.get("claimed_at")) == _time(case["claimed_at"]))
        elapsed = int((_time(claim["claimed_at"]) - _time(case["period_start"])).total_seconds())
        if expected.elapsed_seconds is not None:
            check("independent_elapsed_time", elapsed == expected.elapsed_seconds,
                  expected=expected.elapsed_seconds, actual=elapsed)
        if expected.policy_version == "lab-v1":
            check("historical_crosses_policy_cutover", _time(case["period_start"]) < _time("2026-11-10T00:00:00Z")
                  <= _time(claim["claimed_at"]))
    native_units = _usage(observations["usage"], case["metric_code"])
    frozen_units = _usage(context.get("usage"), case["metric_code"])
    if expected.native_units is not None:
        check("native_selected_metric_units", native_units == _number(expected.native_units),
              expected=expected.native_units, actual=None if native_units is None else str(native_units))
        check("frozen_usage_matches_native_readback", frozen_units is not None and frozen_units == native_units,
              frozen=None if frozen_units is None else str(frozen_units))
        # 用原生费用行独立核对期间；不因为产品已经标CURRENT就相信这个标签。
        fees = [row for row in invoice.get("fees", []) if row.get("external_subscription_id") == case["subscription_id"]]
        charges = [row for row in fees if (row.get("item") or {}).get("type") == "charge"]
        selected_fees = charges or [row for row in fees if (row.get("item") or {}).get("type") == "subscription"]
        spans = {(_time(row.get("from_date")), _time(row.get("to_date"))) for row in selected_fees}
        raw = observations["usage"]
        check("native_fee_and_usage_exact_period", len(spans) == 1 and isinstance(raw, dict)
              and (_time(raw.get("from_datetime")), _time(raw.get("to_datetime"))) in spans
              and _time(raw.get("from_datetime")) == _time(case["period_start"]))
    coverage = source.get("coverage") or {}
    if case["label"] == "late_usage" and stage == "initial":
        check("pending_zero_not_complete", coverage.get("status") == "OPEN" and coverage.get("pending_count", 0) > 0
              and len(coverage.get("expected_events", [])) > 0 and coverage.get("sealed_at") is None)
    elif expected.selected_units is not None and case["label"] not in {"expired_contract", "draft_contract"}:
        check("coverage_sealed_finite_scope", coverage.get("status") == "SEALED"
              and coverage.get("completeness_scope") == "LOCAL_CONTROLLED_GENERATOR_ONLY"
              and coverage.get("pending_count") == 0 and coverage.get("metric_code") == case["metric_code"] and coverage.get("unit") == "units")
        events = coverage.get("expected_events", [])
        ids = {row.get("event_id") for row in events}
        check("expected_sent_received_sets_closed", len(ids) == len(events)
              and ids == set(coverage.get("sent_event_ids", [])) == set(coverage.get("received_event_ids", [])))
        start, finish = _time(coverage.get("period_start")), _time(coverage.get("period_end"))
        selected = sum((_number(row.get("units")) for row in events if start <= _time(row.get("occurred_at")) <= finish), Decimal(0))
        check("independent_business_subinterval_sum", selected == _number(expected.selected_units)
              and _number(coverage.get("units")) == selected, expected=expected.selected_units, independently_summed=str(selected))
    if case["label"] == "post_claim_usage":
        check("whole_period_not_substituted_for_claim_interval", native_units == Decimal(1)
              and _number(coverage.get("units")) == Decimal(0))
    if case["label"] == "v2_boundary":
        unrelated = [row for row in observations["usage"].get("charges_usage", [])
                     if (row.get("billable_metric") or {}).get("code") != case["metric_code"]]
        check("unrelated_metric_is_present_but_not_counted", any(_number(row.get("units")) == Decimal(999) for row in unrelated)
              and native_units == Decimal(0))
    payment = source.get("payment") or {}
    if case["label"] == "overpaid":
        check("prepared_overpayment_is_real_source_fact", payment.get("amount_minor") == invoice.get("total_amount_cents", 0) + 1)
    elif case["label"] == "wrong_currency":
        check("prepared_currency_mismatch_is_real_source_fact", payment.get("currency") != invoice.get("currency"))
    elif expected.eligibility != "NEEDS_MORE_EVIDENCE":
        check("exact_simulated_payment_basis", payment.get("source_kind") == "SIMULATED_PAYMENT"
              and payment.get("authority_scope") == "LOCAL_LAB" and payment.get("status") == "PAID"
              and type(payment.get("amount_minor")) is int and payment["amount_minor"] == invoice.get("total_amount_cents")
              and payment.get("currency") == invoice.get("currency"))
    if case["label"] in {"expired_contract", "draft_contract", "special_contract"}:
        reference = linked_renewals[0].get("contract_ref", {}) if len(linked_renewals) == 1 else {}
        contracts = [row for row in source.get("contracts", []) if row.get("contract_id") == reference.get("contract_id")
                     and row.get("version") == reference.get("version")]
        check("negative_contract_object_exists", len(contracts) == 1)
        if len(contracts) == 1:
            contract = contracts[0]
            if case["label"] == "expired_contract":
                check("contract_valid_at_renewal_expired_at_claim", contract.get("status") == "ACTIVE"
                      and _time(contract.get("effective_from")) <= _time(case["period_start"]) < _time(contract.get("effective_to"))
                      and _time(contract.get("effective_to")) == _time(case["claimed_at"]))
            elif case["label"] == "draft_contract":
                check("draft_not_accepted", contract.get("status") == "DRAFT" and contract.get("acceptance") is None)
            else:
                check("special_clause_is_explicit", (contract.get("terms") or {}).get("special_terms") is True
                      and (contract.get("terms") or {}).get("refund_addendum") == "EGAP-LAB-SPECIAL-1")
    messages, conversation = observations["messages"], observations["conversation"]
    if not isinstance(messages, list) or not isinstance(conversation, dict):
        raise OracleInputError("NATIVE_CHATWOOT_READBACK_REQUIRED")
    marker = "[EGAP draft " + str(job.get("job_id")) + "]"
    notes = [message for message in messages if marker in (message.get("content") or "")]
    check("exact_private_note_delivery", delivery.get("delivery_state") == "COMPLETE" and len(notes) == 1
          and notes[0].get("private") is True and notes[0].get("conversation_id") == case["conversation_id"]
          and notes[0].get("id") == delivery.get("external_message_id"))
    check("conversation_remains_open", conversation.get("status") == "open", actual=conversation.get("status"))
    check("no_public_outgoing_message", all(message.get("private") is True or message.get("message_type") in (0, "incoming") for message in messages))
    check("deterministic_note_lab_scope", len(notes) == 1 and "本地实验" in notes[0].get("content", "")
          and "模拟" in notes[0].get("content", ""))
    if isinstance(observations.get("source_context"), dict):
        actual_source = observations["source_context"]
        check("independent_source_scope_readback", actual_source.get("scope") == scope)
        for key, identifier in (("claim", "claim_id"), ("coverage", "coverage_id"), ("payment", "payment_id")):
            check("independent_source_" + key, (actual_source.get(key) or {}).get(identifier) == (source.get(key) or {}).get(identifier))
            # 不只比同一个ID：候选可能保留ID却篡改付款、申请时点或封存状态。
            # 本检查在每个阶段读回后即执行，阶段之间的事实变化会另建快照。
            observed_fact = json.dumps(actual_source.get(key), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            frozen_fact = json.dumps(source.get(key), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            check("independent_source_" + key + "_content", observed_fact == frozen_fact,
                  observed_sha256=hashlib.sha256(observed_fact.encode()).hexdigest(),
                  frozen_sha256=hashlib.sha256(frozen_fact.encode()).hexdigest())
        for key in ("policies", "contracts", "events", "invoice_link"):
            # 不比较全租户source_revision；这里比较实际材料内容。集合排序不承载业务含义。
            def canonical_fact(value):
                if isinstance(value, list):
                    value = sorted(value, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
                return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            observed_fact, frozen_fact = canonical_fact(actual_source.get(key)), canonical_fact(source.get(key))
            check("independent_source_" + key + "_content", observed_fact == frozen_fact,
                  observed_sha256=hashlib.sha256(observed_fact.encode()).hexdigest(),
                  frozen_sha256=hashlib.sha256(frozen_fact.encode()).hexdigest())
    findings = [{**finding, "label": case["label"], "stage": stage, "job_id": job.get("job_id"),
                 "draft_sha256": hashlib.sha256((draft.get("customer_reply_draft") or "").encode()).hexdigest()}
                for finding in wording_probes(draft.get("customer_reply_draft"), expected.eligibility)]
    return {"qualification": "LOCAL_INDEPENDENT_RULE_CHECK", "label": case["label"], "stage": stage,
            "status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
            "job_id": job.get("job_id"), "checks": checks, "findings": findings,
            "semantic_quality_status": "NOT_FULLY_EVALUATED"}
