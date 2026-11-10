"""实验商业来源适配与确定性资格判断（被测产品，不是独立 Oracle）。

DisputeService 先核验 Chatwoot/Lago 归属，再调用 CommercialClient；受理时冻结
这些事实，Worker 只读快照。来源返回条款和事件，不提供资格答案。本模块只生成
LOCAL_LAB 的人工审核建议，永不执行付款、退款或对客发送。
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any

from .http import IntegrationError, JsonClient


def instant(value: Any) -> datetime:
    """严格的带时区时点；不让服务器本地时区改变政策选择或窗口端点。"""
    if not isinstance(value, str):
        raise ValueError("timestamp required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def number(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("numeric units required")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("nonnegative finite units required")
    return result


def _same_time(left: Any, right: Any) -> bool:
    try:
        return instant(left) == instant(right)
    except (ValueError, TypeError, OverflowError):
        return False


def _message_time(value: Any) -> datetime:
    """Chatwoot 原生秒数与来源服务 ISO 引用按同一时点比较，不改写原始快照。"""
    return datetime.fromtimestamp(value, timezone.utc) if type(value) in {int, float} else instant(value)


def _contains(row: dict, at: datetime) -> bool:
    return instant(row["effective_from"]) <= at and (row.get("effective_to") is None or at < instant(row["effective_to"]))


def _source_valid(row: dict) -> bool:
    """绑定原文与已核对摘要；hash 只能证明一致性，不宣称法律批准。"""
    source = row.get("source_document") or {}
    text = source.get("text")
    return (isinstance(text, str) and bool(text) and bool(source.get("reference"))
            and sha256(text.encode()).hexdigest() == source.get("sha256")
            and row.get("structured_source_sha256") == source.get("sha256")
            and row.get("review_status") == "LOCAL_CALIBRATION")


class CommercialClient:
    """只有服务端配置能指定来源；已配置但不可读时抛 503，绝不回退 fixture。"""

    def __init__(self, base_url: str, read_token: str, *, audit: Any = None) -> None:
        self.http = JsonClient(base_url, {"Authorization": "Bearer " + read_token}, "commercial", audit)

    def context(self, scope: dict) -> dict:
        result = self.http.request("GET", "/v1/context", query=scope)
        if result.get("source_kind") != "SYNTHETIC_BUSINESS_SOURCE" or result.get("authority_scope") != "LOCAL_LAB" \
                or result.get("scope") != scope or type(result.get("source_revision")) is not int \
                or result["source_revision"] < 0 or any(not isinstance(result.get(key), list)
                    or not all(isinstance(row, dict) for row in result[key]) for key in ("policies", "contracts", "events")) \
                or any(result.get(key) is not None and not isinstance(result[key], dict)
                       for key in ("claim", "coverage", "payment", "invoice_link")):
            raise IntegrationError("COMMERCIAL_SOURCE_SCOPE_INVALID")
        return result


def normalized_lago_usage(context: dict, metric_code: str, unit: str) -> Decimal | None:
    """仅归一化已核验订阅/原生期间的指定指标；不混加指标、不读顶层 units。

    Lago 的 units 没有任意业务单位转换能力。首期只接受显式 units，唯一匹配
    billable_metric.code。多行同指标可能代表分段/分组，须单独定义后才能支持。
    """
    usage = context.get("usage")
    if not isinstance(usage, dict) or unit != "units":
        return None
    subscription = context.get("subscription") or {}
    bound = context.get("lago_query_scope") or {}
    if bound.get("subscription_id") != subscription.get("external_subscription_id") \
            or bound.get("customer_id") != context["billing"].get("external_customer_id") \
            or bound.get("invoice_id") != context["billing"].get("invoice_id"):
        return None
    invoice = context["billing"].get("invoice_id")
    if context.get("usage_binding") == "SUBSCRIPTION_CURRENT_PERIOD":
        if context.get("usage_source") != "CURRENT" or usage.get("lago_invoice_id") not in {None, invoice}:
            return None
    elif usage.get("lago_invoice_id") != invoice:
        return None
    expected = context.get("invoice_period") or {}
    observed = context.get("observed_usage_period") or {}
    if not expected or not observed or any(not _same_time(expected.get(key), observed.get(key)) for key in ("from_datetime", "to_datetime")):
        return None
    if any(not _same_time(usage.get(key), expected.get(key)) for key in ("from_datetime", "to_datetime")):
        return None
    rows = usage.get("charges_usage")
    if not isinstance(rows, list):
        return None
    matching = [row for row in rows if isinstance(row, dict) and (row.get("billable_metric") or {}).get("code") == metric_code]
    if len(matching) != 1 or matching[0].get("unit", "units") != unit:
        return None
    try:
        return number(matching[0].get("units"))
    except (InvalidOperation, ValueError):
        return None


def _coverage_units(context: dict, source: dict, event: dict, claim: dict, terms: dict) -> Decimal | None:
    """验证受控事件集合闭合，再以原生 Lago 聚合校准；仅证明实验生成器范围。

    业务时钟与 Lago 真实时钟分列。子区间数值由带时点的受控事件求和，原生整期
    聚合只用于核对全部已发事件；不能直接把整账期聚合当作续费到申请的子区间。
    """
    coverage, link = source.get("coverage") or {}, source.get("invoice_link") or {}
    scope = source["scope"]
    if coverage.get("status") != "SEALED" or coverage.get("completeness_scope") != "LOCAL_CONTROLLED_GENERATOR_ONLY" \
            or coverage.get("interval") != "CLOSED" or coverage.get("pending_count") != 0 \
            or coverage.get("metric_code") != terms["metric_code"] or coverage.get("unit") != terms["unit"] \
            or any(coverage.get(key) != scope[key] for key in ("customer_id", "subscription_id", "invoice_id")):
        return None
    if terms.get("usage_scope") != "RENEWAL_TO_CLAIM" \
            or not _same_time(coverage.get("period_start"), event["effective_at"]) \
            or not _same_time(coverage.get("period_end"), claim["claimed_at"]):
        return None
    expected = coverage.get("expected_events")
    if not isinstance(expected, list):
        return None
    ids = [row["event_id"] for row in expected]
    sent, received = coverage.get("sent_event_ids", []), coverage.get("received_event_ids", [])
    if len(ids) != len(set(ids)) or len(sent) != len(ids) or len(received) != len(ids) \
            or set(ids) != set(sent) or set(ids) != set(received):
        return None
    start, end = instant(coverage["period_start"]), instant(coverage["period_end"])
    if start > end or not coverage.get("sealed_at"):
        return None
    total = sum((number(row["units"]) for row in expected), Decimal(0))
    selected = sum((number(row["units"]) for row in expected if start <= instant(row["occurred_at"]) <= end), Decimal(0))
    native = normalized_lago_usage(context, terms["metric_code"], terms["unit"])
    reconciliation = coverage.get("reconciliation") or {}
    raw = reconciliation.get("raw_response")
    if not isinstance(raw, dict):
        return None
    raw_hash = sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    receipt = reconciliation.get("receipt") or {}
    if reconciliation.get("matched") is not True or receipt.get("service") != "lago" or receipt.get("method") != "GET" \
            or receipt.get("status") != 200 or raw_hash != reconciliation.get("raw_response_sha256") \
            or native is None or native != total or number(reconciliation.get("lago_units")) != total \
            or number(coverage.get("native_expected_units")) != total or number(coverage.get("units")) != selected:
        return None
    # 同时检查当前观察与校准原生区间，防止将另一账期相同的零值拿来充数。
    period = context["invoice_period"]
    if any(not _same_time(reconciliation.get("native_period_" + label), period.get(field))
           or not _same_time(link.get("native_period_" + label), period.get(field))
           for label, field in (("start", "from_datetime"), ("end", "to_datetime"))):
        return None
    native_start, native_end = instant(period["from_datetime"]), instant(period["to_datetime"])
    if any(not native_start <= instant(row["native_occurred_at"]) <= native_end for row in expected):
        return None
    historical = {**context, "usage": raw}
    if normalized_lago_usage(historical, terms["metric_code"], terms["unit"]) != total:
        return None
    return selected


def commercial_decision(context: dict) -> dict:
    """从冻结来源逐项判定。任何必要关系未知都补证；有效资料齐全才比较窗口/用量。

    由 external_decision → 本函数调用。特殊合同首期转人工，不猜例外。所有输出
    都限定 LAB_SCOPED；LOCAL_CALIBRATION 不是用户、法务或真实公司的批准。
    """
    billing, subscription = context["billing"], context["subscription"]
    result = {"workflow_revision": "business-source-pack-v1", "policy_ref": None,
        "billing_record_ref": billing["billing_record_id"], "elapsed_seconds": None,
        "window_seconds": None, "within_window": None, "usage_units": None,
        "maximum_usage_units": None, "usage_within_limit": None, "contract_terms_match": None,
        "billing_paid": None, "missing_evidence": [], "eligibility": "NEEDS_MORE_EVIDENCE",
        "proposed_action": "REQUEST_INFORMATION", "reason_codes": [],
        "evidence_refs": [billing["billing_record_id"], subscription["subscription_id"]],
        "authority_scope": "LOCAL_LAB", "assessment_scope": "LAB_SCOPED", "review_status": "LOCAL_CALIBRATION",
        "payment_simulated": False, "usage_completeness_scope": "LOCAL_CONTROLLED_GENERATOR_ONLY"}
    missing = result["missing_evidence"]
    source = context.get("commercial_source")
    if not source:
        missing.extend(context.get("missing_evidence") or ["COMMERCIAL_SOURCE"])
    else:
        try:
            _assess(context, source, result)
        except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation, OverflowError):
            # 来源响应结构可读但业务字段损坏，不允许默认值制造资格。
            missing.append("COMMERCIAL_FACTS_INVALID")
    result["missing_evidence"] = list(dict.fromkeys(missing))
    if result["missing_evidence"]:
        result["eligibility"] = "NEEDS_MORE_EVIDENCE"
        if "SPECIAL_CONTRACT_TERMS" in missing:
            result["proposed_action"] = "HUMAN_REVIEW"
        result["reason_codes"] = ["MISSING_" + item for item in result["missing_evidence"]]
    result["reason_codes"].append("LOCAL_LAB_ADVISORY_ONLY")
    result["evidence_refs"] = list(dict.fromkeys(result["evidence_refs"]))
    return result


def _assess(context: dict, source: dict, result: dict) -> None:
    """按来源关系装配条款→续费→固定申请→覆盖→模拟付款，不依赖模型裁决。"""
    missing = result["missing_evidence"]
    scope = source["scope"]
    if source.get("authority_scope") != "LOCAL_LAB" or source.get("source_kind") != "SYNTHETIC_BUSINESS_SOURCE":
        missing.append("LAB_SOURCE_AUTHORITY")
        return
    expected = {"tenant_id": context["ticket"]["tenant_id"], "customer_id": context["billing"]["external_customer_id"],
                "subscription_id": context["subscription"]["external_subscription_id"], "invoice_id": context["billing"]["invoice_id"]}
    if any(scope.get(key) != value for key, value in expected.items()):
        missing.append("COMMERCIAL_SCOPE")
        return
    events = [event for event in source["events"] if event.get("event_type") == "SUBSCRIPTION_RENEWAL_EFFECTIVE"
        and type(event.get("term_number")) is int and event["term_number"] >= 2
        and all(event.get(key) == scope[key] for key in ("customer_id", "subscription_id"))
        and scope["invoice_id"] in event.get("invoice_refs", [])]
    if len(events) != 1:
        missing.append("RENEWAL_EVENT")
        return
    event = events[0]
    renewed = instant(event["effective_at"])
    link = source.get("invoice_link") or {}
    if link.get("native_invoice_kind") not in {"INITIAL", "RENEWAL"} or link.get("mapping_kind") != "EXACT_PERIOD_LOCAL_BUSINESS_MAPPING" \
            or not _same_time(link.get("business_period_start"), event.get("period_start")) \
            or not _same_time(link.get("business_period_end"), event.get("period_end")) \
            or not _same_time(link.get("business_period_start"), link.get("native_period_start")) \
            or not _same_time(link.get("business_period_end"), link.get("native_period_end")) \
            or not _same_time(event.get("period_start"), event.get("effective_at")):
        missing.append("RENEWAL_INVOICE_MAPPING")
    result["evidence_refs"].append(event["event_id"])
    claim = source.get("claim")
    if not claim or any(claim.get(key) != scope[key] for key in ("customer_id", "subscription_id", "invoice_id", "account_id", "conversation_id")):
        missing.append("REFUND_CLAIM")
        return
    original = claim.get("original_message") or {}
    messages = context.get("customer_message_refs", [])
    matching = [row for row in messages if row["id"] == original.get("message_id")]
    if len(matching) != 1 or _message_time(matching[0].get("created_at")) != _message_time(original.get("created_at")) \
            or sha256((matching[0].get("content") or "").encode()).hexdigest() != original.get("content_sha256"):
        missing.append("ORIGINAL_CLAIM_MESSAGE")
    claimed = instant(claim["claimed_at"])
    elapsed = (claimed - renewed).total_seconds()
    if elapsed < 0:
        missing.append("CLAIM_BEFORE_RENEWAL")
    result["elapsed_seconds"] = elapsed
    result["evidence_refs"].append(claim["claim_id"])
    reference = event.get("contract_ref") or {}
    contracts = [row for row in source["contracts"] if row.get("contract_id") == reference.get("contract_id")
        and row.get("version") == reference.get("version")
        and all(row.get(key) == scope[key] for key in ("customer_id", "subscription_id")) and row.get("status") == "ACTIVE"
        and _contains(row, renewed) and _contains(row, claimed)]
    if len(contracts) != 1:
        missing.append("APPLICABLE_CONTRACT")
        return
    contract = contracts[0]
    acceptance = contract.get("acceptance") or {}
    if not _source_valid(contract) or acceptance.get("kind") != "SIMULATED_ACCEPTANCE" \
            or instant(acceptance["accepted_at"]) > renewed:
        missing.append("ACCEPTED_CONTRACT_SOURCE")
    if contract["terms"].get("special_terms") is not False:
        missing.append("SPECIAL_CONTRACT_TERMS")
    if contract["terms"].get("refund_addendum") != "EGAP-LAB-REFUND-1":
        missing.append("REFUND_ADDENDUM")
    policy_ref = contract.get("policy_ref") or {}
    mode = contract["terms"].get("policy_selection")
    policies = [row for row in source["policies"] if row.get("policy_id") == policy_ref.get("policy_id") and
        ((mode == "RENEWAL_EFFECTIVE_AT" and _contains(row, renewed)) or
         (mode == "PINNED" and row.get("version") == policy_ref.get("version") and _contains(row, renewed)))]
    if len(policies) != 1:
        missing.append("APPLICABLE_POLICY")
        return
    policy = policies[0]
    if not _source_valid(policy):
        missing.append("REVIEWED_POLICY_SOURCE")
    terms = policy["terms"]
    if terms.get("selection") != "RENEWAL_EFFECTIVE_AT" or terms.get("window_endpoint") != "INCLUSIVE" \
            or type(terms.get("window_seconds")) is not int or terms["window_seconds"] not in {7 * 86400, 14 * 86400} \
            or terms.get("output_scope") != "LOCAL_LAB_ADVISORY_ONLY":
        missing.append("SUPPORTED_POLICY_SEMANTICS")
        return
    result.update(policy_ref=policy["policy_id"] + ":" + policy["version"], window_seconds=terms["window_seconds"],
                  within_window=0 <= elapsed <= terms["window_seconds"], contract_terms_match=not missing)
    # Draft 的 evidence_refs 是协议标识符，不接受路径中的 / 或 #。原文路径仍留
    # 在冻结快照；这里用内容摘要定位，避免模型已经调用后才因草稿 Schema 失败。
    result["evidence_refs"].extend([contract["contract_id"], result["policy_ref"],
        "lab-doc:" + policy["source_document"]["sha256"], "lab-doc:" + contract["source_document"]["sha256"]])
    units = _coverage_units(context, source, event, claim, terms)
    maximum = number(terms["maximum_usage_units"])
    result["maximum_usage_units"] = str(maximum)
    if units is None:
        missing.append("USAGE_COMPLETENESS")
    else:
        result.update(usage_units=str(units), usage_within_limit=units <= maximum)
        result["evidence_refs"].append(source["coverage"]["coverage_id"])
    payment = source.get("payment") or {}
    if terms.get("require_payment") is not True or terms.get("allowed_payment_source") != "SIMULATED_PAYMENT":
        missing.append("SUPPORTED_PAYMENT_RULE")
    elif not (payment.get("source_kind") == "SIMULATED_PAYMENT" and payment.get("authority_scope") == "LOCAL_LAB"
            and payment.get("status") == "PAID" and all(payment.get(key) == scope[key] for key in ("customer_id", "subscription_id", "invoice_id"))
            and payment.get("currency") == context["billing"].get("currency")
            and type(context["billing"].get("total_amount_cents")) is int and context["billing"]["total_amount_cents"] >= 0
            and type(payment.get("amount_minor")) is int and payment["amount_minor"] == context["billing"]["total_amount_cents"]
            and bool(payment.get("receipt_ref")) and instant(payment["paid_at"]) <= claimed):
        missing.append("SIMULATED_PAYMENT_FACT")
    else:
        result.update(billing_paid=True, payment_simulated=True)
        result["evidence_refs"].append(payment["payment_id"])
    if not missing:
        reasons = []
        if not result["within_window"]:
            reasons.append("REFUND_WINDOW_EXCEEDED")
        if not result["usage_within_limit"]:
            reasons.append("USAGE_LIMIT_EXCEEDED")
        result.update(eligibility="INELIGIBLE" if reasons else "ELIGIBLE",
            proposed_action="NO_ACTION" if reasons else "HUMAN_REVIEW",
            reason_codes=reasons or ["ELIGIBLE_PENDING_HUMAN_REVIEW"])
