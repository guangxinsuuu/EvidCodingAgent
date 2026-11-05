"""客服业务工具：Gateway.resolve → collect → 冻结fixture中的租户事实。

工具返回真实合成资料与可追踪调用；这里的政策选择是被测产品逻辑，不是独立Oracle。
v2的evaluate_refund_policy按版本化政策计算结构结论；真实模型只负责客户回复文案。
scripted_draft只供确定性基础校准，两种模式都不读取verification答案。
"""

from collections.abc import Callable, Mapping
from datetime import datetime
import hashlib
import json
from typing import Any

from .fixtures import FixtureBundle


WORKFLOW_REVISION = "support-policy-decision-v2"


def plain(value: Any) -> Any:
    """将已验证只读fixture转为独立JSON快照，不把可变对象反向交回事实包。"""
    if isinstance(value, Mapping):
        return {key: plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(child) for child in value]
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class BusinessToolError(ValueError):
    """固定错误码；工单、账户和模型文本不进入异常信息。"""


class BusinessTools:
    """仅有读取能力的合成工具集；tenant由已认证Worker对应的持久Job提供。

    event_sink在每次实际查询后同步持久化参数与结果摘要，失败即中止模型调用，
    防止“执行过但观察不到”被当作可用证据。此入口不提供退款或客户消息发送。
    """

    def __init__(self, fixtures: FixtureBundle, event_sink: Callable[[dict[str, Any]], None]) -> None:
        self.fixtures = fixtures
        self.event_sink = event_sink

    def _observe(self, name: str, arguments: dict[str, Any], result: Any) -> Any:
        snapshot = plain(result)
        self.event_sink({"tool_name": name, "arguments": arguments,
                         "result_sha256": digest(snapshot), "found": result is not None,
                         "fixture_sha256": self.fixtures.content_sha256})
        return snapshot

    def collect(self, tenant_id: str, ticket_id: str, revision: int) -> dict[str, Any]:
        """读取同租户账单/用量/合同，按续费时间找政策，保留缺失事实为null。

        不能以工单提交日期或当前时钟选latest政策。每个工具参数都来自前一步事实，
        而非请求文本中的自述；同ticket_id的不同tenant仍是不同业务对象。
        """
        ticket = self._observe("get_ticket", {"tenant_id": tenant_id, "ticket_id": ticket_id, "revision": revision},
                               self.fixtures.get_ticket(tenant_id, ticket_id, revision))
        if ticket is None:
            raise BusinessToolError("TICKET_NOT_FOUND")
        subscription = self._observe("get_subscription", {"tenant_id": tenant_id, "subscription_id": ticket["subscription_ref"]},
                                     self.fixtures.get_record("subscriptions", tenant_id, ticket["subscription_ref"]))
        billing = self._observe("get_billing", {"tenant_id": tenant_id, "billing_record_id": ticket["billing_record_ref"]},
                                self.fixtures.get_record("billing_records", tenant_id, ticket["billing_record_ref"]))
        if subscription is None or billing is None:
            raise BusinessToolError("REQUIRED_REFERENCE_MISSING")
        usage = self._observe("get_usage", {"tenant_id": tenant_id, "billing_record_id": billing["billing_record_id"]},
                              next((row for row in self.fixtures.facts["usage_records"]
                                    if row["tenant_id"] == tenant_id and row["billing_record_ref"] == billing["billing_record_id"]), None))
        contract_ref = subscription["contract_ref"]
        contract = self._observe("get_contract", {"tenant_id": tenant_id, "subscription_id": subscription["subscription_id"],
                                                   "contract_id": contract_ref},
                                 None if contract_ref is None else self.fixtures.get_record("contracts", tenant_id, contract_ref))
        renewed = datetime.fromisoformat(billing["renewed_at"].replace("Z", "+00:00"))
        policies = [row for row in self.fixtures.facts["policies"] if row["plan"] == subscription["plan"]
                    and datetime.fromisoformat(row["effective_from"].replace("Z", "+00:00")) <= renewed
                    and (row["effective_to_exclusive"] is None
                         or renewed < datetime.fromisoformat(row["effective_to_exclusive"].replace("Z", "+00:00")))]
        policy = self._observe("find_policy", {"plan": subscription["plan"], "renewed_at": billing["renewed_at"]},
                               policies[0] if len(policies) == 1 else None)
        return {"ticket": ticket, "subscription": subscription, "billing": billing, "usage": usage,
                "contract": contract, "policy": policy, "fixture_clock": self.fixtures.clock,
                "fixture_sha256": self.fixtures.content_sha256}

    def evaluate_refund_policy(self, context: dict[str, Any]) -> dict[str, Any]:
        """第七个产品工具：精确计算政策窗口/使用限制并记录真实调用摘要。

        计算输入绑定先前查到的ticket/billing/policy/usage/contract，不接收期望答案；
        这是SUT可变业务实现，Verifier必须在独立规则和观察上检查它。
        """
        ticket, billing, policy = context["ticket"], context["billing"], context["policy"]
        return self._observe("evaluate_refund_policy", {
            "tenant_id": ticket["tenant_id"], "ticket_id": ticket["ticket_id"], "ticket_revision": ticket["revision"],
            "billing_record_id": billing["billing_record_id"], "policy_id": policy["policy_id"] if policy else None,
            "renewed_at": billing["renewed_at"], "received_at": ticket["received_at"],
            "workflow_revision": WORKFLOW_REVISION,
        }, evaluate_refund_policy(context))


def evaluate_refund_policy(context: dict[str, Any]) -> dict[str, Any]:
    """用已选政策数据计算产品决定，不依赖case ID、运行当天或验证参考文件。

    以UTC经过秒数比较窗口，读政策中的天数、端点是否包含、用量上限与合同条件。
    缺资料保留None/缺失列表，不能把未知用量当0；完整事实可以得到正或负决定。
    此函数没有退款副作用，ELIGIBLE只表示交人工复核。
    """
    ticket, billing, policy = context["ticket"], context["billing"], context["policy"]
    usage, contract = context["usage"], context["contract"]
    renewed = datetime.fromisoformat(billing["renewed_at"].replace("Z", "+00:00"))
    received = datetime.fromisoformat(ticket["received_at"].replace("Z", "+00:00"))
    elapsed = received - renewed
    elapsed_seconds = elapsed.days * 86_400 + elapsed.seconds
    missing, reasons = [], []
    evidence = [billing["billing_record_id"]]
    if policy is None:
        missing.append("POLICY_RECORD")
    else:
        evidence.append(policy["policy_id"])
    if usage is not None:
        evidence.append(usage["usage_record_id"])
    if contract is not None:
        evidence.append(contract["contract_id"])

    window_seconds = within_window = usage_within_limit = contract_terms_match = maximum_usage_units = None
    billing_paid = billing["status"] == "PAID"
    if policy is not None:
        semantics = policy["window_semantics"]
        if semantics["duration_kind"] != "ELAPSED_24_HOUR_DAYS" or semantics["timezone"] != "UTC":
            missing.append("SUPPORTED_POLICY_WINDOW_SEMANTICS")
        else:
            window_seconds = policy["refund_window_days"] * 86_400
            start_ok = elapsed_seconds >= 0 if semantics["start_inclusive"] else elapsed_seconds > 0
            end_ok = elapsed_seconds <= window_seconds if semantics["end_inclusive"] else elapsed_seconds < window_seconds
            within_window = start_ok and end_ok
        required = set(policy["required_evidence"])
        unsupported = required - {"PAID_BILLING", "USAGE_RECORD", "CONTRACT_RECORD"}
        missing.extend(sorted(unsupported))
        maximum_usage_units = policy["eligibility_conditions"]["maximum_usage_units"]
        if usage is None:
            missing.append("USAGE_RECORD")
        elif datetime.fromisoformat(usage["observed_through"].replace("Z", "+00:00")) < received:
            missing.append("USAGE_RECORD_COVERING_RECEIPT")
        else:
            usage_within_limit = usage["units"] <= maximum_usage_units
        if contract is None:
            missing.append("CONTRACT_RECORD")
        else:
            contract_terms_match = contract["terms"] == policy["eligibility_conditions"]["contract_terms"]
    if missing:
        eligibility, action = "NEEDS_MORE_EVIDENCE", "REQUEST_INFORMATION"
        reasons = ["MISSING_" + item for item in missing]
    else:
        if not within_window:
            reasons.append("OUTSIDE_REFUND_WINDOW")
        if not usage_within_limit:
            reasons.append("USAGE_LIMIT_EXCEEDED")
        if not contract_terms_match:
            reasons.append("CONTRACT_TERMS_MISMATCH")
        if not billing_paid:
            reasons.append("BILLING_NOT_PAID")
        eligibility, action = ("INELIGIBLE", "NO_ACTION") if reasons else ("ELIGIBLE", "HUMAN_REVIEW")
        if not reasons:
            reasons.append("ELIGIBLE_PENDING_HUMAN_REVIEW")
    return {"workflow_revision": WORKFLOW_REVISION, "policy_ref": policy["policy_id"] if policy else None,
            "billing_record_ref": billing["billing_record_id"], "elapsed_seconds": elapsed_seconds,
            "window_seconds": window_seconds, "within_window": within_window,
            "usage_units": usage["units"] if usage is not None else None,
            "maximum_usage_units": maximum_usage_units, "usage_within_limit": usage_within_limit,
            "contract_terms_match": contract_terms_match, "billing_paid": billing_paid,
            "missing_evidence": missing, "eligibility": eligibility, "proposed_action": action,
            "reason_codes": reasons, "evidence_refs": evidence}


def assemble_draft(context: dict[str, Any], decision: dict[str, Any], customer_reply: str) -> dict[str, Any]:
    """产品决定与回复文案组合；模型没有资格、动作、政策或证据引用字段的写权限。"""
    ticket = context["ticket"]
    return {"resolution_type": "REFUND_ELIGIBILITY_REVIEW", "ticket_id": ticket["ticket_id"],
            "ticket_revision": ticket["revision"], "applicable_policy_ref": decision["policy_ref"],
            "billing_record_ref": decision["billing_record_ref"], "eligibility": decision["eligibility"],
            "proposed_action": decision["proposed_action"], "evidence_refs": list(decision["evidence_refs"]),
            "customer_reply_draft": customer_reply}


def model_reply_context(context: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """仅给文案生成所需事实和已计算决定，不传原工单中要求助手执行的指令文本。

    工具仍查询原始工单并留摘要；回复阶段只需身份、日期与业务资料，可避免模型
    把“请审核并准备草稿”原样复述给客户。这里没有引入验证期望或模板答案。
    """
    result = plain(context)
    result["ticket"] = {key: value for key, value in result["ticket"].items() if key not in {"body", "subject"}}
    result["decision"] = plain(decision)
    return result


def scripted_draft(context: dict[str, Any], decision: dict[str, Any] | None = None) -> dict[str, Any]:
    """仅用于SCRIPTED_CALIBRATION的明确替身；不是LLM输出，也不授予退款权限。

    复用产品决定并使用明确模板文案，不查verification文件中的case→答案表。
    LIVE分支绝不调用本函数。后续Verifier不能把此函数当作自己的Oracle。
    """
    decision = decision if decision is not None else evaluate_refund_policy(context)
    if decision["eligibility"] == "NEEDS_MORE_EVIDENCE":
        text = "为完成续费争议审核，请补充缺失的使用记录、合同或适用政策资料；当前未执行退款。"
    elif decision["eligibility"] == "ELIGIBLE":
        text = "根据提供的续费、使用及合同资料，您的申请初步符合政策条件，仍需人工审核；当前未执行退款。"
    else:
        text = "根据提供的续费、使用及合同资料，您的申请不满足当前政策条件；当前未执行退款。"
    return assemble_draft(context, decision, text)
