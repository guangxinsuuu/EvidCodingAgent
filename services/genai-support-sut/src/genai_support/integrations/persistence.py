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


class IntegrationStore(Store):
    def migrate(self) -> None:
        """仅由可信启动器调用；必须先完成原有001迁移。"""
        path = Path(__file__).resolve().parents[3] / "migrations/002_integrations.sql"
        with self._transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(722996002)")
            conn.execute(path.read_text(), prepare=False)

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
              message_sha: str, context: dict, receipts: list[dict]) -> dict:
        """同一幂等键只受理一个Job；消息/选中发票改变才产生业务revision。

        授权和所有API归属检查必须在调用前成功。锁序先操作键再工单，避免两个
        并发重试创建两个Job；同key换参数拒绝。用量刷新不会伪造新的客户操作。
        """
        ticket = f"cw:{account}:{conversation}"
        input_sha = digest({"message_sha": message_sha, "invoice": invoice})
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
            snapshot = conn.execute("SELECT input_sha256 FROM external_inputs WHERE tenant_id=%s AND ticket_id=%s AND ticket_revision=%s", (tenant, ticket, revision)).fetchone()
            if snapshot and snapshot["input_sha256"] != input_sha:
                revision += 1
                conn.execute("UPDATE support_tickets SET current_revision=%s WHERE tenant_id=%s AND ticket_id=%s", (revision, tenant, ticket))
            elif snapshot:
                # 同一客户消息/账单重复进入，即使上游误换操作键，也复用已冻结的业务Job。
                prior_job = conn.execute("SELECT job_id FROM support_jobs WHERE tenant_id=%s AND ticket_id=%s AND ticket_revision=%s ORDER BY created_at LIMIT 1", (tenant, ticket, revision)).fetchone()
                if prior_job:
                    conn.execute("INSERT INTO external_request_keys(tenant_id,operation_key,request_sha256,job_id) VALUES(%s,%s,%s,%s)",
                                 (tenant, key, request_sha, prior_job["job_id"]))
                    return {"job_id": prior_job["job_id"], "reused": True}
            frozen = deepcopy(context)
            frozen["ticket"].update(ticket_id=ticket, tenant_id=tenant, revision=revision)
            conn.execute("INSERT INTO external_inputs(tenant_id,ticket_id,ticket_revision,input_sha256,context,source_receipts) VALUES(%s,%s,%s,%s,%s,%s)",
                         (tenant, ticket, revision, input_sha, Jsonb(frozen), Jsonb(receipts)))
            job = uuid4().hex
            conn.execute("INSERT INTO support_jobs(job_id,tenant_id,ticket_id,ticket_revision,status) VALUES(%s,%s,%s,%s,'QUEUED')", (job, tenant, ticket, revision))
            conn.execute("INSERT INTO support_dispatch_outbox(job_id) VALUES(%s)", (job,))
            self._event(conn, job, None, "ACCEPTED", {"source_kind": "CHATWOOT_LAGO", "input_sha256": input_sha})
            conn.execute("INSERT INTO external_operations(tenant_id,operation_key,request_sha256,job_id,subject,account_id,conversation_id,contact_id,invoice_id,message_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (tenant, key, request_sha, job, subject, account, conversation, contact, invoice, message_sha))
            conn.execute("INSERT INTO external_request_keys(tenant_id,operation_key,request_sha256,job_id) VALUES(%s,%s,%s,%s)",
                         (tenant, key, request_sha, job))
            return {"job_id": job, "reused": False}

    def delivery(self, tenant: str, job: str) -> dict | None:
        with self._transaction() as conn:
            return conn.execute("SELECT o.*,j.status,j.draft,j.ticket_id,j.ticket_revision,t.current_revision FROM external_operations o JOIN support_jobs j USING(job_id) JOIN support_tickets t ON t.tenant_id=j.tenant_id AND t.ticket_id=j.ticket_id WHERE o.tenant_id=%s AND o.job_id=%s", (tenant, job)).fetchone()

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
