"""DisputeService → IntegrationStore → 原有Job/outbox与外部输入/交付账本。

受理事务冻结真实API观察，Worker仍使用原有租约和网关协议。内部备注的网络效果
无法与PostgreSQL原子提交：先记DISPATCHING，失去回执只对账、不重新POST。
"""

from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from ..business_tools import digest
from ..store import Store, StoreError


def _stable_fact_value(value: Any) -> Any:
    """去掉观察器元数据，保留业务时点；不对 *_at 做宽泛删除。

    paid_at、claimed_at、effective_at、accepted_at 与原始消息 created_at 仍参与
    比较。收据的观察时点/请求身份/派生摘要不是新的商业事实；实际内容另行投影。
    """
    observer_fields = {"observed_at", "source_observed_at", "updated_at", "generated_at",
                       "retrieved_at", "fetched_at", "request_id", "correlation_id",
                       "raw_response_sha256", "response_sha256"}
    if isinstance(value, dict):
        return {key: _stable_fact_value(item) for key, item in value.items() if key not in observer_fields}
    if isinstance(value, list):
        return [_stable_fact_value(item) for item in value]
    return deepcopy(value)


def _commercial_facts(source: dict | None) -> dict | None:
    """只投影当前发票适用的事件、合同与历史政策，不把全租户目录当当前事实。

    新增重叠政策仍会进入摘要，因它会使适用政策不唯一；其他时期/其他合同的版本
    则不触发刷新。订阅当前业务时钟及后续未关联该发票的周期也不移动历史 claim。
    """
    if not isinstance(source, dict):
        return None
    from .commercial import instant
    scope = source.get("scope") or {}
    events = [row for row in source.get("events", []) if isinstance(row, dict)
              and scope.get("invoice_id") in row.get("invoice_refs", [])]
    references = [row.get("contract_ref") for row in events if isinstance(row.get("contract_ref"), dict)]
    contracts = [row for row in source.get("contracts", []) if isinstance(row, dict) and any(
        row.get("contract_id") == ref.get("contract_id") and row.get("version") == ref.get("version") for ref in references)]
    policies = []
    for policy in source.get("policies", []):
        if not isinstance(policy, dict):
            continue
        for contract in contracts:
            ref, terms = contract.get("policy_ref") or {}, contract.get("terms") or {}
            if policy.get("policy_id") != ref.get("policy_id"):
                continue
            if terms.get("policy_selection") == "PINNED":
                applies = policy.get("version") == ref.get("version")
            else:
                try:
                    applies = any(instant(policy["effective_from"]) <= instant(event["effective_at"])
                        and (policy.get("effective_to") is None or instant(event["effective_at"]) < instant(policy["effective_to"])) for event in events)
                except (KeyError, TypeError, ValueError, AttributeError):
                    # 同一政策ID有损坏版本会影响决定，不能以“不可解析”为由藏掉它。
                    applies = True
            if applies:
                policies.append(policy)
                break
    result = {key: _stable_fact_value(source.get(key)) for key in (
        "source_kind", "authority_scope", "api_version", "scope", "claim", "coverage", "payment", "invoice_link")}
    for key, rows in (("events", events), ("contracts", contracts), ("policies", policies)):
        result[key] = sorted((_stable_fact_value(row) for row in rows), key=digest)
    # 同一集合接收顺序不是新事实；记录版本仍在完整原始快照中保留。
    for key in ("claim", "coverage"):
        if isinstance(result[key], dict):
            result[key].pop("revision", None)
    coverage = result.get("coverage")
    if isinstance(coverage, dict):
        for key in ("expected_events", "sent_event_ids", "received_event_ids"):
            if isinstance(coverage.get(key), list):
                coverage[key] = sorted(coverage[key], key=digest)
        if isinstance(coverage.get("reconciliation"), dict):
            coverage["reconciliation"].pop("ledger_revision", None)
            coverage["reconciliation"].pop("recorded_at", None)
    return result


def assessment_facts_digest(context: dict) -> str:
    """冻结有业务意义的来源内容；读取时间、关联 ID 与全域 revision 不制造新评估。

    消息独立参与 input_sha。商业服务的 revision 可被其他案例推进，因此仅留作
    来源定位，不能单凭它增加本工单 revision。具体条款/事件/coverage 的变化仍计入。
    """
    facts = {key: _stable_fact_value(context.get(key)) for key in (
        "billing", "subscription", "usage", "invoice_period", "observed_usage_period",
        "usage_source", "usage_binding", "lago_query_scope", "missing_evidence")}
    facts["commercial_source"] = _commercial_facts(context.get("commercial_source"))
    # 原生用量只比较影响本决定的字段，current_usage 的展示/缓存时间不制造新Job。
    usage = context.get("usage")
    if isinstance(usage, dict):
        facts["usage"] = {key: _stable_fact_value(usage.get(key)) for key in ("from_datetime", "to_datetime", "lago_invoice_id")}
        rows = usage.get("charges_usage")
        if isinstance(rows, list):
            metrics = {row.get("terms", {}).get("metric_code") for row in (facts.get("commercial_source") or {}).get("policies", [])}
            metrics.discard(None)
            facts["usage"]["charges_usage"] = sorted([{
                "metric_code": (row.get("billable_metric") or {}).get("code"),
                "units": row.get("units"), "unit": row.get("unit", "units")}
                for row in rows if isinstance(row, dict) and (not metrics or (row.get("billable_metric") or {}).get("code") in metrics)], key=digest)
        else:
            facts["usage"]["charges_usage"] = None
    return digest(facts)


class IntegrationStore(Store):
    def migrate(self) -> None:
        """仅由可信启动器调用；必须先完成原有001迁移。"""
        directory = Path(__file__).resolve().parents[3] / "migrations"
        with self._transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(722996002)")
            for name in ("002_integrations.sql", "003_commercial.sql"):
                conn.execute((directory / name).read_text(), prepare=False)

    def observe(self, correlation_id: str, event: dict) -> None:
        """连接器回调保存结构化收据；禁止把凭据/原始响应体交给此入口。"""
        with self._transaction() as conn:
            conn.execute("INSERT INTO external_api_observations(correlation_id,event) VALUES(%s,%s)",
                         (correlation_id, Jsonb(event)))

    def operation(self, tenant: str, key: str, request_sha: str) -> dict | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM external_request_keys WHERE tenant_id=%s AND operation_key=%s",
                               (tenant, key)).fetchone()
            if row and row["request_sha256"] != request_sha:
                raise StoreError("OPERATION_KEY_CONFLICT")
            return row

    def admit(self, *, tenant: str, key: str, request_sha: str, subject: str,
              account: int, conversation: int, contact: int, invoice: str,
              message_sha: str, context: dict, receipts: list[dict],
              parent_job: str | None = None, reevaluation_reason: str | None = None,
              expected_revision: int | None = None) -> dict:
        """同一幂等键只受理一个Job；消息/选中发票改变才产生业务revision。

        授权和所有API归属检查必须在调用前成功。锁序先操作键再工单，避免两个
        并发重试创建两个Job；同key换参数拒绝。普通提交只按消息/发票去重；显式
        reevaluate 才比较来源事实，并保留父 Job。锁内重读当前快照避免并发刷新重复。
        """
        ticket = f"cw:{account}:{conversation}"
        input_sha = digest({"message_sha": message_sha, "invoice": invoice})
        facts_sha = assessment_facts_digest(context)
        source_revision = (context.get("commercial_source") or {}).get("source_revision")
        with self._transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (tenant + ":" + key,))
            prior = conn.execute("SELECT * FROM external_request_keys WHERE tenant_id=%s AND operation_key=%s",
                                 (tenant, key)).fetchone()
            if prior:
                if prior["request_sha256"] != request_sha:
                    raise StoreError("OPERATION_KEY_CONFLICT")
                return {"job_id": prior["job_id"], "reused": True}
            conn.execute("INSERT INTO support_tickets(tenant_id,ticket_id,current_revision) VALUES(%s,%s,1) ON CONFLICT DO NOTHING", (tenant, ticket))
            current = conn.execute("SELECT current_revision FROM support_tickets WHERE tenant_id=%s AND ticket_id=%s FOR UPDATE", (tenant, ticket)).fetchone()
            revision = current["current_revision"]
            if parent_job is not None:
                parent = conn.execute("SELECT o.invoice_id,j.ticket_id FROM external_operations o JOIN support_jobs j USING(job_id) WHERE o.tenant_id=%s AND o.job_id=%s", (tenant, parent_job)).fetchone()
                if parent is None or parent["ticket_id"] != ticket or parent["invoice_id"] != invoice:
                    raise StoreError("ASSESSMENT_PARENT_MISMATCH")
            snapshot = conn.execute("SELECT input_sha256,context,assessment_facts_sha256 FROM external_inputs WHERE tenant_id=%s AND ticket_id=%s AND ticket_revision=%s", (tenant, ticket, revision)).fetchone()
            # 使用同一投影比较新旧内容；旧行保存的摘要可能来自早期算法，不能把
            # 投影升级本身当新商业事实。旧摘要/快照仍不覆盖，供原版本审计。
            facts_changed = bool(snapshot and assessment_facts_digest(snapshot["context"]) != facts_sha)
            changed = bool(snapshot and (snapshot["input_sha256"] != input_sha or (parent_job is not None and facts_changed)))
            if changed:
                # 两个刷新读到同一旧版本但取得不同事实时，较迟的旧观察不能覆盖新版本。
                # 相同事实先由下方复用分支吸收，不会误报并发冲突。
                if parent_job is not None and expected_revision != revision:
                    raise StoreError("ASSESSMENT_REVISION_CONFLICT")
                revision += 1
                conn.execute("UPDATE support_tickets SET current_revision=%s WHERE tenant_id=%s AND ticket_id=%s", (revision, tenant, ticket))
            elif snapshot:
                # 同一客户消息/账单重复进入，即使上游误换操作键，也复用已冻结的业务Job。
                prior_job = conn.execute("SELECT job_id FROM support_jobs WHERE tenant_id=%s AND ticket_id=%s AND ticket_revision=%s ORDER BY created_at LIMIT 1", (tenant, ticket, revision)).fetchone()
                if prior_job:
                    conn.execute("INSERT INTO external_request_keys(tenant_id,operation_key,request_sha256,job_id) VALUES(%s,%s,%s,%s)",
                                 (tenant, key, request_sha, prior_job["job_id"]))
                    result = {"job_id": prior_job["job_id"], "reused": True}
                    if parent_job is not None:
                        result.update(assessment_status="NO_NEW_FACTS", parent_job_id=parent_job)
                    return result
            frozen = deepcopy(context)
            frozen["ticket"].update(ticket_id=ticket, tenant_id=tenant, revision=revision)
            conn.execute("INSERT INTO external_inputs(tenant_id,ticket_id,ticket_revision,input_sha256,context,source_receipts,assessment_facts_sha256,assessment_source_revision) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                         (tenant, ticket, revision, input_sha, Jsonb(frozen), Jsonb(receipts), facts_sha, source_revision))
            job = uuid4().hex
            conn.execute("INSERT INTO support_jobs(job_id,tenant_id,ticket_id,ticket_revision,status) VALUES(%s,%s,%s,%s,'QUEUED')", (job, tenant, ticket, revision))
            conn.execute("INSERT INTO support_dispatch_outbox(job_id) VALUES(%s)", (job,))
            self._event(conn, job, None, "ACCEPTED", {"source_kind": "CHATWOOT_LAGO", "input_sha256": input_sha,
                "assessment_facts_sha256": facts_sha, "assessment_source_revision": source_revision, "parent_job_id": parent_job})
            conn.execute("INSERT INTO external_operations(tenant_id,operation_key,request_sha256,job_id,subject,account_id,conversation_id,contact_id,invoice_id,message_sha256,parent_job_id,reevaluation_reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (tenant, key, request_sha, job, subject, account, conversation, contact, invoice, message_sha, parent_job, reevaluation_reason))
            conn.execute("INSERT INTO external_request_keys(tenant_id,operation_key,request_sha256,job_id) VALUES(%s,%s,%s,%s)",
                         (tenant, key, request_sha, job))
            result = {"job_id": job, "reused": False}
            if parent_job is not None:
                result.update(assessment_status="REEVALUATED", parent_job_id=parent_job)
            return result

    def delivery(self, tenant: str, job: str) -> dict | None:
        with self._transaction() as conn:
            return conn.execute("SELECT o.*,j.status,j.draft,j.ticket_id,j.ticket_revision,t.current_revision,(i.context ? 'commercial_source') AS commercial_source_present FROM external_operations o JOIN support_jobs j USING(job_id) JOIN support_tickets t ON t.tenant_id=j.tenant_id AND t.ticket_id=j.ticket_id JOIN external_inputs i ON i.tenant_id=j.tenant_id AND i.ticket_id=j.ticket_id AND i.ticket_revision=j.ticket_revision WHERE o.tenant_id=%s AND o.job_id=%s", (tenant, job)).fetchone()

    def claim_delivery(self, tenant: str, job: str) -> bool:
        """先持久登记派发意图；崩溃残留DISPATCHING按UNKNOWN对账，绝不再次抢发。"""
        with self._transaction() as conn:
            # 与admit的工单锁协调，确保取得派发资格的瞬间revision确实相同。
            # 锁不跨HTTP持有；远端仍没有CAS，备注必须保留原revision标记。
            current = conn.execute("SELECT j.ticket_revision,t.current_revision FROM support_jobs j JOIN support_tickets t ON t.tenant_id=j.tenant_id AND t.ticket_id=j.ticket_id WHERE j.tenant_id=%s AND j.job_id=%s FOR SHARE OF t", (tenant, job)).fetchone()
            if current is None or current["ticket_revision"] != current["current_revision"]:
                return False
            row = conn.execute("UPDATE external_operations o SET delivery_state='DISPATCHING',updated_at=clock_timestamp() FROM support_jobs j,support_tickets t WHERE o.job_id=j.job_id AND t.tenant_id=j.tenant_id AND t.ticket_id=j.ticket_id AND o.tenant_id=%s AND o.job_id=%s AND o.delivery_state='PENDING' AND j.status='DONE' AND j.ticket_revision=t.current_revision", (tenant, job))
            return row.rowcount == 1

    def finish_delivery(self, tenant: str, job: str, state: str, *, message_id: int | None = None,
                        error: str | None = None) -> None:
        if state not in {"COMPLETE", "UNKNOWN", "STALE", "FAILED"}:
            raise StoreError("INVALID_DELIVERY_STATE")
        if state == "COMPLETE" and (type(message_id) is not int or message_id < 1):
            raise StoreError("DELIVERY_RECEIPT_REQUIRED")
        allowed = ["DISPATCHING", "UNKNOWN"] if state in {"COMPLETE", "UNKNOWN"} else ["PENDING"]
        with self._transaction() as conn:
            conn.execute("UPDATE external_operations SET delivery_state=%s,external_message_id=%s,delivery_error=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND job_id=%s AND delivery_state=ANY(%s)", (state, message_id, error, tenant, job, allowed))

    def pending_deliveries(self) -> list[dict]:
        with self._transaction() as conn:
            return conn.execute("SELECT o.tenant_id,o.job_id FROM external_operations o JOIN support_jobs j USING(job_id) WHERE j.status='DONE' AND o.delivery_state='PENDING' ORDER BY o.created_at LIMIT 20").fetchall()


class ExternalSnapshotTools:
    """Gateway → 只读Job来源快照；实际HTTP收据在Ingress账本，可按job关联。

    网关不持Chatwoot/Lago写凭据。缺快照会失败，不回退合成fixture。
    """
    def __init__(self, reader_dsn: str, event_sink: Any) -> None:
        self.store, self.event_sink = Store(reader_dsn), event_sink

    def collect(self, tenant: str, ticket: str, revision: int) -> dict:
        from ..business_tools import BusinessToolError
        with self.store._transaction() as conn:
            row = conn.execute("SELECT context,source_receipts FROM external_inputs WHERE tenant_id=%s AND ticket_id=%s AND ticket_revision=%s", (tenant, ticket, revision)).fetchone()
        if row is None:
            raise BusinessToolError("EXTERNAL_SNAPSHOT_NOT_FOUND")
        self.event_sink({"tool_name": "read_external_snapshot", "arguments": {"tenant_id": tenant, "ticket_id": ticket, "revision": revision},
                         "result_sha256": digest(row["context"]), "source_kind": "CHATWOOT_LAGO", "source_receipt_count": len(row["source_receipts"]), "found": True})
        return row["context"]

    def evaluate_refund_policy(self, context: dict) -> dict:
        from .workflow import external_decision
        decision = external_decision(context)
        self.event_sink({"tool_name": "evaluate_external_refund_evidence", "arguments": {"billing_record_id": context["billing"]["billing_record_id"]},
                         "result_sha256": digest(decision), "found": True})
        return decision
