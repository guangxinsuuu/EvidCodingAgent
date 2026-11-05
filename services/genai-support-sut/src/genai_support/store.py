"""HTTP/Relay/Worker → Store → PostgreSQL短事务。

202前原子保存Job和outbox；Worker完成时原子保存草稿和DONE。Redis仅传通知，
数据库保存受理身份、状态、执行租约和恢复事实。本模块没有任意SQL或删除接口。
"""

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .fixtures import FixtureBundle
from .protocol import validate_support_job, validate_support_submission


class StoreError(RuntimeError):
    """只携带稳定错误码，绝不回显DSN、SQL参数或底层连接异常。"""

    def __init__(self, code: str = "STORE_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: int
    job_id: str


@dataclass(frozen=True, slots=True)
class Claim:
    """一次物理执行的条件写凭据；模型调用去重仍使用稳定job_id。

    lease_token只防过期Worker提交，不是认证token；它不能取代数据库权限。
    """

    job_id: str
    tenant_id: str
    ticket_id: str
    ticket_revision: int
    attempt_id: str
    lease_token: int
    owner: str


def _bounded_integer(value: int, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise StoreError("STORE_ARGUMENT_INVALID")


def _owner(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise StoreError("STORE_ARGUMENT_INVALID")


class Store:
    """同步DAO；API通过线程池调用，Relay/Worker在各自进程调用。

    不共享连接或长事务。连接、锁、语句等待都有上限；失败不能被解释成任务不存在。
    事务提交回执丢失时调用者须按原Job身份对账，不能盲目重试POST。
    """

    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn:
            raise StoreError("STORE_CONFIGURATION_INVALID")
        self._dsn = dsn

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection]:
        try:
            with psycopg.connect(self._dsn, connect_timeout=3, row_factory=dict_row,
                    options="-c search_path=egap_support,pg_catalog -c statement_timeout=5000 -c lock_timeout=2000") as conn:
                yield conn
        except psycopg.Error:
            raise StoreError() from None

    def migrate_and_seed(self, fixtures: FixtureBundle) -> None:
        """可信初始化入口调用；固定fixture绑定不允许重启悄悄覆盖已有业务资料。"""
        migration = Path(__file__).resolve().parents[2] / "migrations/001.sql"
        with self._transaction() as conn:
            # 并发初始化先串行化DDL/seed；该锁仅限当前数据库，不干扰其他实验DB。
            conn.execute("SELECT pg_advisory_xact_lock(722996001)")
            conn.execute(migration.read_text(encoding="utf-8"), prepare=False)
            binding = conn.execute("SELECT fixture_sha256 FROM support_fixture_binding WHERE singleton").fetchone()
            if binding and binding["fixture_sha256"] != fixtures.content_sha256:
                raise StoreError("FIXTURE_BINDING_MISMATCH")
            conn.execute("INSERT INTO support_fixture_binding(singleton,schema_version,fixture_sha256) VALUES(true,1,%s) ON CONFLICT DO NOTHING",
                         (fixtures.content_sha256,))
            current: dict[tuple[str, str], int] = {}
            for ticket in fixtures.facts["tickets"]:
                key = (ticket["tenant_id"], ticket["ticket_id"])
                current[key] = max(current.get(key, 0), ticket["revision"])
            for (tenant, ticket), revision in current.items():
                conn.execute("INSERT INTO support_tickets(tenant_id,ticket_id,current_revision) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                             (tenant, ticket, revision))

    def health(self) -> bool:
        """只验证SUT数据库及migration身份；不推断队列、Worker或模型健康。"""
        with self._transaction() as conn:
            row = conn.execute("SELECT schema_version FROM support_fixture_binding WHERE singleton").fetchone()
            return row is not None and row["schema_version"] == 1

    def create_job(self, tenant_id: str, ticket_id: str, ticket_revision: int) -> dict[str, Any]:
        """API受理：确认同租户当前revision，同一事务写Job/outbox/ACCEPTED事件。

        本接口未提供HTTP幂等键，每次合法调用产生新Job。响应丢失不能自动重发。
        """
        _bounded_integer(ticket_revision, 1, 9_007_199_254_740_991)
        job_id = uuid4().hex
        value = {"schema_version": 1, "job_id": job_id, "tenant_id": tenant_id,
                 "ticket_id": ticket_id, "ticket_revision": ticket_revision, "status": "QUEUED"}
        validate_support_submission(value)
        with self._transaction() as conn:
            ticket = conn.execute("SELECT current_revision FROM support_tickets WHERE tenant_id=%s AND ticket_id=%s FOR SHARE",
                                  (tenant_id, ticket_id)).fetchone()
            if ticket is None:
                raise StoreError("TICKET_NOT_FOUND")
            if ticket["current_revision"] != ticket_revision:
                raise StoreError("TICKET_REVISION_CONFLICT")
            conn.execute("INSERT INTO support_jobs(job_id,tenant_id,ticket_id,ticket_revision,status) VALUES(%s,%s,%s,%s,'QUEUED')",
                         (job_id, tenant_id, ticket_id, ticket_revision))
            conn.execute("INSERT INTO support_dispatch_outbox(job_id) VALUES(%s)", (job_id,))
            self._event(conn, job_id, None, "ACCEPTED")
        return value

    def get_job(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        """API查询；同Job但不同tenant与未知Job均None，连接故障则抛错而非伪404。"""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM support_jobs WHERE tenant_id=%s AND job_id=%s", (tenant_id, job_id)).fetchone()
            return self._wire_job(row) if row else None

    def get_job_status(self, job_id: str) -> str | None:
        """仅供可信Worker判定重复消息是否已终止，不暴露跨租户正文。"""
        with self._transaction() as conn:
            row = conn.execute("SELECT status FROM support_jobs WHERE job_id=%s", (job_id,)).fetchone()
            return row["status"] if row else None

    def pending_outbox(self, limit: int = 20) -> list[OutboxRecord]:
        """Relay获取待投递通知；多个Relay可重复发送同Job，不提前消费未确认投递。"""
        _bounded_integer(limit, 1, 100)
        with self._transaction() as conn:
            rows = conn.execute("SELECT outbox_id,job_id FROM support_dispatch_outbox WHERE dispatched_at IS NULL ORDER BY outbox_id LIMIT %s", (limit,)).fetchall()
            return [OutboxRecord(**row) for row in rows]

    def mark_dispatched(self, outbox_id: int) -> None:
        """只在Redis确认XADD后调用。标记回执丢失可重投同Job，不能创建新业务身份。"""
        with self._transaction() as conn:
            conn.execute("UPDATE support_dispatch_outbox SET dispatched_at=COALESCE(dispatched_at,clock_timestamp()) WHERE outbox_id=%s", (outbox_id,))

    def reclaimable_jobs(self, limit: int = 20) -> list[str]:
        """补偿Redis消息丢失/Worker死亡；仅枚举可领取身份，不代替claim的原子判定。"""
        _bounded_integer(limit, 1, 100)
        with self._transaction() as conn:
            rows = conn.execute("SELECT job_id FROM support_jobs WHERE status='QUEUED' OR (status='PROCESSING' AND lease_until<=clock_timestamp()) ORDER BY updated_at,job_id LIMIT %s", (limit,)).fetchall()
            return [row["job_id"] for row in rows]

    def claim_job(self, job_id: str, owner: str, lease_seconds: int = 30) -> Claim | None:
        """队列通知→行锁→租约/fence递增；并发通知最多产生一个有效claim。

        过期attempt保留UNKNOWN，新的attempt只能对账同job的网关结果，不代表可重发模型。
        """
        _owner(owner)
        _bounded_integer(lease_seconds, 1, 300)
        with self._transaction() as conn:
            row = conn.execute("SELECT *, lease_until>clock_timestamp() AS lease_live FROM support_jobs WHERE job_id=%s FOR UPDATE", (job_id,)).fetchone()
            if row is None or row["status"] in ("DONE", "FAILED") or (row["status"] == "PROCESSING" and row["lease_live"]):
                return None
            ticket = conn.execute("SELECT current_revision FROM support_tickets WHERE tenant_id=%s AND ticket_id=%s FOR SHARE", (row["tenant_id"], row["ticket_id"])).fetchone()
            if ticket["current_revision"] != row["ticket_revision"]:
                self._supersede(conn, row)
                return None
            if row["current_attempt_id"]:
                conn.execute("UPDATE support_job_attempts SET outcome='UNKNOWN',finished_at=COALESCE(finished_at,clock_timestamp()) WHERE attempt_id=%s AND outcome='RUNNING'", (row["current_attempt_id"],))
            attempt = uuid4().hex
            token = row["lease_token"] + 1
            conn.execute("INSERT INTO support_job_attempts(attempt_id,job_id,lease_token,owner,outcome) VALUES(%s,%s,%s,%s,'RUNNING')", (attempt, job_id, token, owner))
            conn.execute("UPDATE support_jobs SET status='PROCESSING',lease_token=%s,lease_owner=%s,lease_until=clock_timestamp()+(%s*interval '1 second'),current_attempt_id=%s,updated_at=clock_timestamp() WHERE job_id=%s", (token, owner, lease_seconds, attempt, job_id))
            self._event(conn, job_id, attempt, "CLAIMED", {"lease_token": token})
            return Claim(job_id, row["tenant_id"], row["ticket_id"], row["ticket_revision"], attempt, token, owner)

    def renew_claim(self, claim: Claim, lease_seconds: int = 30) -> bool:
        """心跳只延长仍有效的相同租约；失效Worker不能通过续约重新获得写权限。"""
        _bounded_integer(lease_seconds, 1, 300)
        with self._transaction() as conn:
            result = conn.execute("UPDATE support_jobs SET lease_until=clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp() WHERE job_id=%s AND status='PROCESSING' AND lease_owner=%s AND lease_token=%s AND current_attempt_id=%s AND lease_until>clock_timestamp()",
                                  (lease_seconds, claim.job_id, claim.owner, claim.lease_token, claim.attempt_id))
            return result.rowcount == 1

    def complete_job(self, claim: Claim, draft: dict[str, Any]) -> bool:
        """Worker→完成事务；只有当前租约且当前ticket revision才能保存草稿和DONE。

        先复制/协议校验再进入事务；这里不裁决退款资格或正文语义。更新与DONE事件
        在同一事务，提交后才允许Worker ACK Redis；提交回执未知必须按job对账。
        """
        snapshot = json.loads(json.dumps(draft, allow_nan=False))
        value = {"schema_version": 1, "job_id": claim.job_id, "tenant_id": claim.tenant_id,
                 "ticket_id": claim.ticket_id, "ticket_revision": claim.ticket_revision,
                 "status": "DONE", "draft": snapshot, "error_code": None}
        validate_support_job(value)
        with self._transaction() as conn:
            row = self._owned_claim(conn, claim)
            if row is None:
                return False
            ticket = conn.execute("SELECT current_revision FROM support_tickets WHERE tenant_id=%s AND ticket_id=%s FOR SHARE", (claim.tenant_id, claim.ticket_id)).fetchone()
            if ticket["current_revision"] != claim.ticket_revision:
                self._supersede(conn, row)
                return False
            conn.execute("UPDATE support_jobs SET status='DONE',draft=%s,error_code=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE job_id=%s", (Jsonb(snapshot), claim.job_id))
            conn.execute("UPDATE support_job_attempts SET outcome='SUCCEEDED',finished_at=clock_timestamp() WHERE attempt_id=%s", (claim.attempt_id,))
            self._event(conn, claim.job_id, claim.attempt_id, "DONE")
        return True

    def fail_job(self, claim: Claim) -> bool:
        """只用于已确定的处理失败；网络/模型请求回执不明须用release_unknown。"""
        with self._transaction() as conn:
            if self._owned_claim(conn, claim) is None:
                return False
            conn.execute("UPDATE support_jobs SET status='FAILED',draft=NULL,error_code='PROCESSING_FAILED',lease_until=NULL,updated_at=clock_timestamp() WHERE job_id=%s", (claim.job_id,))
            conn.execute("UPDATE support_job_attempts SET outcome='FAILED',finished_at=clock_timestamp() WHERE attempt_id=%s", (claim.attempt_id,))
            self._event(conn, claim.job_id, claim.attempt_id, "FAILED")
        return True

    def release_unknown(self, claim: Claim, code: str = "EXECUTION_UNKNOWN", retry_after_seconds: int = 5) -> bool:
        """保留PROCESSING和未知attempt，并设置有界对账延迟；不制造FAILED或新模型请求。"""
        _bounded_integer(retry_after_seconds, 1, 300)
        if code not in {"EXECUTION_UNKNOWN", "GATEWAY_UNKNOWN", "GATEWAY_UNAVAILABLE", "COMMIT_UNKNOWN"}:
            code = "EXECUTION_UNKNOWN"
        with self._transaction() as conn:
            if self._owned_claim(conn, claim) is None:
                return False
            # 即刻撤销旧Worker的条件写资格，同时保留future lease作为对账退避。
            conn.execute("UPDATE support_jobs SET lease_owner='awaiting-reconciliation',lease_until=clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp() WHERE job_id=%s", (retry_after_seconds, claim.job_id))
            conn.execute("UPDATE support_job_attempts SET outcome='UNKNOWN',finished_at=clock_timestamp() WHERE attempt_id=%s", (claim.attempt_id,))
            self._event(conn, claim.job_id, claim.attempt_id, "UNKNOWN", {"code": code})
        return True

    def heartbeat(self, role: str, owner: str) -> None:
        if role not in {"relay", "worker"}:
            raise StoreError("STORE_ARGUMENT_INVALID")
        _owner(owner)
        with self._transaction() as conn:
            conn.execute("INSERT INTO support_service_heartbeats(role,owner) VALUES(%s,%s) ON CONFLICT(role,owner) DO UPDATE SET observed_at=clock_timestamp()", (role, owner))

    def service_health(self, max_age_seconds: int = 15) -> dict[str, bool]:
        """按数据库时钟读取最近心跳；此健康信号不证明某Job或模型调用成功。"""
        _bounded_integer(max_age_seconds, 1, 300)
        with self._transaction() as conn:
            rows = conn.execute("SELECT DISTINCT role FROM support_service_heartbeats WHERE observed_at>clock_timestamp()-(%s*interval '1 second')", (max_age_seconds,)).fetchall()
            roles = {row["role"] for row in rows}
            return {"relay": "relay" in roles, "worker": "worker" in roles}

    @staticmethod
    def _owned_claim(conn: psycopg.Connection, claim: Claim) -> dict[str, Any] | None:
        return conn.execute("SELECT * FROM support_jobs WHERE job_id=%s AND tenant_id=%s AND ticket_id=%s AND ticket_revision=%s AND status='PROCESSING' AND lease_owner=%s AND lease_token=%s AND current_attempt_id=%s AND lease_until>clock_timestamp() FOR UPDATE",
                            (claim.job_id, claim.tenant_id, claim.ticket_id, claim.ticket_revision, claim.owner, claim.lease_token, claim.attempt_id)).fetchone()

    @staticmethod
    def _wire_job(row: dict[str, Any]) -> dict[str, Any]:
        value = {key: row[key] for key in ("job_id", "tenant_id", "ticket_id", "ticket_revision", "status", "draft", "error_code")}
        value["schema_version"] = 1
        validate_support_job(value)
        return value

    @staticmethod
    def _event(conn: psycopg.Connection, job_id: str, attempt_id: str | None, kind: str, details: dict | None = None) -> None:
        conn.execute("INSERT INTO support_job_events(job_id,attempt_id,kind,details) VALUES(%s,%s,%s,%s)", (job_id, attempt_id, kind, Jsonb(details or {})))

    def _supersede(self, conn: psycopg.Connection, row: dict[str, Any]) -> None:
        """工单已更新：旧执行以明确失败保留，不覆盖新版本草稿也不无限重投。"""
        conn.execute("UPDATE support_jobs SET status='FAILED',draft=NULL,error_code='PROCESSING_FAILED',lease_until=NULL,updated_at=clock_timestamp() WHERE job_id=%s", (row["job_id"],))
        if row["current_attempt_id"]:
            conn.execute("UPDATE support_job_attempts SET outcome='STALE',finished_at=clock_timestamp() WHERE attempt_id=%s", (row["current_attempt_id"],))
        self._event(conn, row["job_id"], row["current_attempt_id"], "SUPERSEDED")
