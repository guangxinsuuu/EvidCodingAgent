"""OIDC入口 → 受控客户绑定 → Chatwoot/Lago → 冻结输入 → 原有异步SUT。

本模块是被测产品集成逻辑，不能作为独立Verifier/Oracle。客服生成的是内部待审
草稿；商业来源只提供显式本地实验权益，不能冒充真实企业批准或执行退款。
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..business_tools import digest
from ..store import StoreError
from .chatwoot import ChatwootClient
from .commercial import CommercialClient, commercial_decision, instant
from .http import IntegrationError
from .identity import KeycloakIdentity
from .lago import LagoClient
from .persistence import IntegrationStore


def customer_messages(messages: list[dict]) -> list[dict]:
    """仅客户incoming消息参与revision；内部备注/outgoing/status事件不得触发循环。

    原生消息按id排序，只冻结实际客户正文及时间。上游重投同一消息不会改变摘要。
    """
    result = []
    for row in messages:
        if row.get("private") is False and row.get("message_type") in (0, "incoming"):
            result.append({key: row.get(key) for key in ("id", "content", "created_at")})
    return sorted(result, key=lambda row: row["id"])


def external_decision(context: dict) -> dict:
    """Gateway → 确定性商业判断；未配置来源仍补证，未知不作为零或不符合。"""
    return commercial_decision(context)


def build_context(tenant: str, account: int, conversation: int, messages: list[dict], scope: dict) -> dict:
    """归属核验后的原生对象 → 网关输入；所有未知字段显式保留，不拼造合成事实。"""
    invoice, subscription = scope["invoice"], scope["subscription"]
    timestamp = messages[-1]["created_at"]
    try:
        received_at = datetime.fromtimestamp(timestamp, timezone.utc) if type(timestamp) in {int, float} else datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if received_at.tzinfo is None:
            raise ValueError()
        received = received_at.isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        raise IntegrationError("CUSTOMER_MESSAGE_TIME_INVALID", status=503) from None
    usage = scope.get("usage")
    missing = [*scope.get("missing_evidence", []), "RENEWAL_EVENT", "APPROVED_POLICY", "CONTRACT_RECORD", "USAGE_COMPLETENESS"]
    return {"source_kind": "CHATWOOT_LAGO", "synthetic_test_customer": True,
            "ticket": {"tenant_id": tenant, "ticket_id": f"cw:{account}:{conversation}", "revision": 1,
                       "received_at": received, "body": messages[-1]["content"]},
            "subscription": {"subscription_id": "lago-sub:" + scope["external_subscription_id"],
                             "external_subscription_id": scope["external_subscription_id"],
                             "status": subscription.get("status"), "plan": subscription.get("plan_code"), "contract_ref": None},
            "billing": {"billing_record_id": "lago-invoice:" + scope["invoice_id"],
                        "external_customer_id": scope["external_customer_id"],
                        "invoice_id": scope["invoice_id"], "renewed_at": None,
                        "issuing_date": invoice.get("issuing_date"), "currency": invoice.get("currency"),
                        "total_amount_cents": invoice.get("total_amount_cents"),
                        "status": "PAID" if invoice.get("payment_status") == "succeeded" else "PAYMENT_UNCONFIRMED"},
            "usage": usage, "contract": None, "policy": None,
            "customer_message_refs": messages,
            "lago_query_scope": {"customer_id": scope["external_customer_id"],
                                 "subscription_id": scope["external_subscription_id"], "invoice_id": scope["invoice_id"]},
            "invoice_period": scope.get("invoice_period"),
            "observed_usage_period": scope.get("observed_usage_period"),
            "policy_asset_status": "DRAFT_NOT_APPROVED", "usage_source": scope["usage_source"],
            "usage_binding": scope.get("usage_binding"),
            "missing_evidence": list(dict.fromkeys(missing)),
            "source_observed_at": datetime.now(timezone.utc).isoformat()}


class DisputeService:
    """HTTP路由调用的集成编排器；所有外部URL、凭据、客户映射来自服务器配置。

    request中的tenant只是选择已授权范围，不能授予身份。invoice_id只作线索，
    LagoClient必须先在已确认客户的发票列表中找到该ID再读取详情。
    """
    def __init__(self, store: IntegrationStore, config: dict) -> None:
        self.store, self.config = store, config
        subjects: dict[str, list[str]] = {}
        for binding in config["bindings"]["customers"]:
            for subject in binding["allowed_subjects"]:
                subjects.setdefault(subject, []).append(binding["tenant_id"])
        kc = config["keycloak"]
        self.identity = KeycloakIdentity(kc["issuer"], kc["client_id"], kc["client_secret"],
                                         kc.get("audience", kc["client_id"]), subjects)

    def authenticate(self, token: str) -> Any:
        return self.identity.authenticate(token)

    def _clients(self, correlation: str, receipts: list[dict]) -> tuple[ChatwootClient, LagoClient]:
        def audit(event: dict) -> None:
            self.store.observe(correlation, event)
            receipts.append(event)
        cw, lago = self.config["chatwoot"], self.config["lago"]
        return (ChatwootClient(cw["base_url"], cw["api_token"], cw["account_id"], audit=audit),
                LagoClient(lago["base_url"], lago["api_key"], audit=audit))

    def _binding(self, tenant: str, subject: str, contact: int | None = None) -> dict:
        matches = [row for row in self.config["bindings"]["customers"] if row["tenant_id"] == tenant
                   and row["chatwoot_account_id"] == self.config["chatwoot"]["account_id"]
                   and subject in row["allowed_subjects"] and (contact is None or row["chatwoot_contact_id"] == contact)]
        if len(matches) != 1:
            raise IntegrationError("CUSTOMER_ACCESS_DENIED", status=403)
        return matches[0]

    def submit(self, identity: Any, command: dict) -> dict:
        """普通受理保留按消息/发票复用；新操作 key 不是刷新商业事实的授权。"""
        return self._submit(identity, command)

    def reevaluate(self, identity: Any, tenant: str, job: str, command: dict) -> dict:
        """显式刷新：先校验父评估归属，收集新来源，再事务比较事实/消息摘要。

        原操作重试优先返回原 Job，不发起新的外部读取。父 Job、理由与事实版本
        全部保留；即使换了 key，没有新事实也不会重复模型调用。
        """
        if tenant not in identity.tenant_ids:
            raise IntegrationError("CUSTOMER_ACCESS_DENIED", status=403)
        binding = self._binding(tenant, identity.subject)
        parent = self.store.delivery(tenant, job)
        if parent is None:
            raise IntegrationError("JOB_NOT_FOUND", status=404)
        if parent["contact_id"] != binding["chatwoot_contact_id"] or parent["account_id"] != binding["chatwoot_account_id"]:
            raise IntegrationError("CUSTOMER_ACCESS_DENIED", status=403)
        ingress = {"tenant_id": tenant, "conversation_id": parent["conversation_id"],
                   "invoice_id": parent["invoice_id"], "operation_key": command["operation_key"]}
        return self._submit(identity, ingress, parent_job=job, reason=command["reason"],
                            expected_revision=parent["current_revision"])

    def _submit(self, identity: Any, command: dict, *, parent_job: str | None = None,
                reason: str | None = None, expected_revision: int | None = None) -> dict:
        """两类入口共用归属核验/快照收集；只有显式 reevaluate 传入父评估参数。"""
        tenant = command["tenant_id"]
        if tenant not in identity.tenant_ids:
            raise IntegrationError("CUSTOMER_ACCESS_DENIED", status=403)
        binding = self._binding(tenant, identity.subject)
        # 测试工作区的会话由可信seed/管理员登记。先本地授权，才读取Chatwoot；
        # 不能先取出另一个客户的会话正文，再用contact mismatch补做拒绝。
        if not any(row["tenant_id"] == tenant and row["conversation_id"] == command["conversation_id"]
                   for row in self.config["bindings"]["cases"]):
            raise IntegrationError("CONVERSATION_NOT_AUTHORIZED", status=403)
        fingerprint_input = {**command, "subject": identity.subject}
        if parent_job is not None:
            fingerprint_input.update(mode="REEVALUATE", parent_job_id=parent_job, reason=reason)
        fingerprint = digest(fingerprint_input)
        prior = self.store.operation(tenant, command["operation_key"], fingerprint)
        if prior:
            return {"job_id": prior["job_id"], "reused": True}
        correlation, receipts = uuid4().hex, []
        cw, lago = self._clients(correlation, receipts)
        conversation = cw.get_conversation(command["conversation_id"])
        contact = conversation.get("meta", {}).get("sender", {}).get("id")
        if contact != binding["chatwoot_contact_id"]:
            raise IntegrationError("CUSTOMER_ACCESS_DENIED", status=403)
        messages = customer_messages(cw.get_messages(command["conversation_id"]))
        if not messages:
            raise IntegrationError("CUSTOMER_MESSAGE_REQUIRED", status=409)
        scope = lago.resolve_invoice_scope(binding["lago_customer_id"], command["invoice_id"])
        context = build_context(tenant, cw.account_id, command["conversation_id"], messages, scope)
        context["source_correlation_id"] = correlation
        commercial = self.config.get("commercial_source")
        if commercial is not None:
            def audit(event: dict) -> None:
                self.store.observe(correlation, event)
                receipts.append(event)
            source = CommercialClient(commercial["base_url"], commercial["read_token"], audit=audit).context({
                "tenant_id": tenant, "customer_id": scope["external_customer_id"],
                "subscription_id": scope["external_subscription_id"], "invoice_id": scope["invoice_id"],
                "account_id": cw.account_id, "conversation_id": command["conversation_id"]})
            context["commercial_source"] = source
            context["policy_asset_status"] = "LOCAL_CALIBRATION_NOT_ENTERPRISE_APPROVAL"
            # 申请时点来自不可覆盖的 claim；消息继续参与 revision，但补充消息不重置窗口。
            # 完整归属与原始消息核验由确定性判断负责，缺/坏字段仍输出补证。
            claim = source.get("claim")
            if claim and isinstance(claim.get("claimed_at"), str):
                try:
                    context["ticket"]["received_at"] = instant(claim["claimed_at"]).isoformat()
                except (ValueError, TypeError):
                    pass
            decision = external_decision(context)
            context["missing_evidence"] = decision["missing_evidence"]
            # 保留原始来源集合，下面只是便于文案读取的已选择条款，不授予模型选择权。
            for policy in source["policies"]:
                if policy["policy_id"] + ":" + policy["version"] == decision["policy_ref"]:
                    context["policy"] = policy
            for contract in source["contracts"]:
                if contract.get("contract_id") in decision["evidence_refs"]:
                    context["contract"] = contract
            for event in source["events"]:
                if event.get("event_id") in decision["evidence_refs"]:
                    context["billing"]["renewed_at"] = event.get("effective_at")
        return self.store.admit(tenant=tenant, key=command["operation_key"], request_sha=fingerprint,
            subject=identity.subject, account=cw.account_id, conversation=command["conversation_id"],
            contact=contact, invoice=command["invoice_id"], message_sha=digest(messages), context=context, receipts=receipts,
            **({"parent_job": parent_job, "reevaluation_reason": reason, "expected_revision": expected_revision} if parent_job else {}))

    def deliver(self, tenant: str, job: str) -> dict:
        """DONE → 核对当前客户消息/revision → 私有备注 → 外部ID读回。

        Chatwoot无跨系统CAS；备注始终标注原Job/revision，不能声称与客户更新原子。
        写入回执丢失时仅查marker对账。不存在marker不证明POST未执行，不自动重发。
        """
        row = self.store.delivery(tenant, job)
        if row is None:
            raise IntegrationError("JOB_NOT_FOUND", status=404)
        if row["delivery_state"] in {"COMPLETE", "STALE", "FAILED"}:
            return self._delivery_result(row)
        if row["status"] != "DONE":
            return self._delivery_result(row)
        self._binding(tenant, row["subject"], row["contact_id"])
        receipts: list[dict] = []
        cw, _ = self._clients("delivery:" + job, receipts)
        marker = f"[EGAP draft {job}]"
        # 实验范围由冻结来源确定，不依赖模型是否主动写出免责声明；同一前缀参与
        # 丢回执后的全文核对，不能因恢复而把实验建议变成无范围的业务承诺。
        scope_notice = "仅本地实验资格建议；商业政策、合同接受及付款含模拟来源，不代表真实客户权益或真实退款。\n" if row.get("commercial_source_present") else ""
        content = f"{marker}\n{scope_notice}待人工审核；基于工单输入版本 {row['ticket_revision']}，争议账单 {row['invoice_id']}。未执行退款，未发送客户消息。\n\n{row['draft']['customer_reply_draft']}"
        if row["delivery_state"] in {"DISPATCHING", "UNKNOWN"}:
            matches = [m for m in cw.get_messages(row["conversation_id"]) if m.get("private") is True
                       and content == m.get("content") and m.get("conversation_id") == row["conversation_id"]]
            if len(matches) == 1:
                self.store.finish_delivery(tenant, job, "COMPLETE", message_id=matches[0]["id"])
            else:
                self.store.finish_delivery(tenant, job, "UNKNOWN", error="WRITEBACK_RECEIPT_UNKNOWN")
        else:
            conversation = cw.get_conversation(row["conversation_id"])
            if conversation.get("meta", {}).get("sender", {}).get("id") != row["contact_id"]:
                self.store.finish_delivery(tenant, job, "FAILED", error="CUSTOMER_BINDING_CHANGED")
            elif row["ticket_revision"] != row["current_revision"] or digest(customer_messages(cw.get_messages(row["conversation_id"]))) != row["message_sha256"]:
                self.store.finish_delivery(tenant, job, "STALE", error="BUSINESS_INPUT_CHANGED")
            elif self.store.claim_delivery(tenant, job):
                try:
                    message = cw.create_private_note(row["conversation_id"], content)
                    self.store.finish_delivery(tenant, job, "COMPLETE", message_id=message["id"])
                except IntegrationError as exc:
                    # 任何已进入派发的错误均不授权再次写入；拒绝/未知保留可核对状态。
                    self.store.finish_delivery(tenant, job, "UNKNOWN", error=exc.code)
        return self._delivery_result(self.store.delivery(tenant, job))

    @staticmethod
    def _delivery_result(row: dict) -> dict:
        return {key: row[key] for key in ("job_id", "delivery_state", "external_message_id", "delivery_error")}

    def run_writeback(self, stop: Any) -> None:
        """API宿主后台循环：仅扫描已完成且未派发Job；进程停止后由账本恢复。

        UNKNOWN需要显式只读对账入口，后台不重复发送。外部不可用时保留PENDING，
        下一轮只重做尚未派发的核验；部署生命周期为循环提供总体资源上限。
        """
        while not stop.is_set():
            try:
                for row in self.store.pending_deliveries():
                    if stop.is_set():
                        break
                    try:
                        self.deliver(row["tenant_id"], row["job_id"])
                    except (IntegrationError, StoreError) as exc:
                        self.store.observe("delivery:" + row["job_id"], {"stage": "writeback", "error_code": exc.code})
            except StoreError:
                # 数据库不可用时不持有任何新的派发资格；保持有界退避。
                pass
            stop.wait(2)
