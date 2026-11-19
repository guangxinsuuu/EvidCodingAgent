"""人工固定计划的 PostgreSQL 执行账本，不授予 Agent 自动派发权限。

调用链：迟到用量 CLI → ExperimentLedger.execute → 受信任的业务适配器 callback。
账本只保证本机操作独占、身份绑定和保守恢复；外部 HTTP 与数据库不是同一事务，
所以 DISPATCHED 只说明已进入可能发送的区间，不能证明请求已经发出或业务成功。
进程在该区间消失时，下一次执行保留 UNKNOWN，必须通过单独的只读观察来对账。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


STATES = ("RESERVED", "DISPATCHED", "COMPLETED", "UNKNOWN", "FAILED")


class LedgerError(RuntimeError):
    """稳定错误码不拼接 DSN、令牌或原始 HTTP 内容；详细证据保存在私有文件。"""

    def __init__(self, code: str, operation: dict | None = None):
        super().__init__(code)
        self.code = code
        self.operation = operation


class IdentityConflict(LedgerError):
    pass


class OperationBusy(LedgerError):
    pass


class OperationUnknown(LedgerError):
    pass


class NotSentError(LedgerError):
    """适配器仅在能够证明尚未发出任何请求时抛出；超时和断连不属于这一类。"""


def canonical(value: object) -> bytes:
    """稳定 JSON 编码用于身份与证据摘要；拒绝 NaN 和非 JSON 类型。"""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise LedgerError("LEDGER_JSON_INVALID") from None


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _public(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _name(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value):
        raise LedgerError("LEDGER_" + label + "_INVALID")


class Dispatch:
    """单次 execute 的上下文；仅在持有该操作的 PG 会话锁时有效。

    callback 在实际 HTTP 返回后调用 record_sent，再返回业务结果 dict。没有回执的
    超时必须保持 request_sent=None；不可把进入 callback 解释为已经发送。SQL/local
    操作可只 record_evidence，不虚构 HTTP 状态。该对象不应传给后台线程或保留复用。
    """

    def __init__(self, ledger: "ExperimentLedger", connection, operation: dict):
        self.ledger = ledger
        self.connection = connection
        self.operation = operation
        self.active = True

    def _check(self):
        if not self.active or self.operation["status"] != "DISPATCHED":
            raise LedgerError("LEDGER_DISPATCH_CLOSED")

    def record_evidence(self, label: str, payload: dict | list) -> dict:
        """保存不可覆盖的原始观察，并在同一操作事件流内登记引用与 SHA-256。"""
        self._check()
        _name(label, "EVIDENCE_LABEL")
        ref = self.ledger._evidence(self.operation, label, payload)
        self.operation = self.ledger._transition(self.connection, self.operation, "DISPATCHED",
                                                "EVIDENCE", evidence=ref)
        return ref

    def record_sent(self, receipt: dict, *, http_status: int | None = None) -> dict:
        """仅接受实际 HTTP 回执；发出与成功是两个概念，4xx/5xx 同样证明已发出。"""
        self._check()
        status = http_status if http_status is not None else receipt.get("http_status", receipt.get("status"))
        if type(status) is not int or not 100 <= status <= 599:
            raise LedgerError("LEDGER_SENT_REQUIRES_HTTP_RECEIPT")
        ref = self.ledger._evidence(self.operation, "http_receipt", {"http_status": status, "receipt": receipt})
        self.operation = self.ledger._transition(self.connection, self.operation, "DISPATCHED",
                                                "REQUEST_SENT", request_sent=True, evidence=ref)
        return ref


class ExperimentLedger:
    """独立 schema 内的实验与操作账本；构造只建表，不创建业务对象或启动服务。

    candidate/configuration/inputs 由调用方先独立核验，register_experiment 将它们冻结。
    一个 operation_key 永久绑定 kind、effect 和 request；即使执行失败也不能换参数。
    会话 advisory lock 横跨外部调用，但数据库事务不横跨网络等待。进程退出会释放
    锁，已提交 DISPATCHED 则留下未知窗口，避免把释放锁误解为可以安全重新发送。
    """

    def __init__(self, dsn: str, evidence_dir: Path, *, schema: str = "late_usage_ledger"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise LedgerError("LEDGER_SCHEMA_INVALID")
        self._dsn = dsn
        self.schema = schema
        self.evidence_dir = Path(evidence_dir).resolve()
        self.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.evidence_dir.chmod(0o700)
        with self._connect() as connection:
            # DDL 同样串行，两个恢复进程同时启动时不能竞争创建 PostgreSQL 系统目录项。
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_id("schema", schema),))
            connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {}.experiments (
                    experiment_id text PRIMARY KEY,
                    candidate jsonb NOT NULL, candidate_sha256 text NOT NULL,
                    configuration jsonb NOT NULL, configuration_sha256 text NOT NULL,
                    inputs jsonb NOT NULL, inputs_sha256 text NOT NULL,
                    qualification text NOT NULL DEFAULT 'MANUAL_FIXED_PLAN_LOCAL_OPERATOR',
                    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )
            """).format(sql.Identifier(schema)))
            connection.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {}.operations (
                    experiment_id text NOT NULL REFERENCES {}.experiments(experiment_id),
                    operation_key text NOT NULL, operation_id text NOT NULL UNIQUE,
                    kind text NOT NULL, effect text NOT NULL CHECK (effect IN ('READ','WRITE')),
                    request jsonb NOT NULL, request_sha256 text NOT NULL,
                    candidate_sha256 text NOT NULL, configuration_sha256 text NOT NULL,
                    inputs_sha256 text NOT NULL,
                    status text NOT NULL CHECK (status IN ('RESERVED','DISPATCHED','COMPLETED','UNKNOWN','FAILED')),
                    dispatch_count integer NOT NULL DEFAULT 0,
                    request_sent boolean DEFAULT false,
                    result jsonb, last_error jsonb, unknown_receipt jsonb,
                    evidence_refs jsonb NOT NULL DEFAULT '[]',
                    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                    PRIMARY KEY(experiment_id, operation_key)
                )
            """).format(sql.Identifier(schema), sql.Identifier(schema)))
            connection.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {}.operation_events (
                    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    experiment_id text NOT NULL, operation_key text NOT NULL,
                    event_kind text NOT NULL, previous_state text, state text NOT NULL,
                    payload jsonb NOT NULL,
                    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                    FOREIGN KEY(experiment_id,operation_key) REFERENCES {}.operations(experiment_id,operation_key)
                )
            """).format(sql.Identifier(schema), sql.Identifier(schema)))

    def _connect(self):
        return psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5,
                               application_name="egap_manual_experiment_ledger")

    def _query(self, query: str):
        return sql.SQL(query).format(schema=sql.Identifier(self.schema))

    def _lock_id(self, experiment_id: str, operation_key: str) -> int:
        return int.from_bytes(hashlib.sha256(canonical([self.schema, experiment_id, operation_key])).digest()[:8],
                              "big", signed=True)

    @contextmanager
    def _locked(self, experiment_id: str, operation_key: str):
        with self._connect() as connection:
            acquired = connection.execute("SELECT pg_try_advisory_lock(%s) AS acquired",
                                          (self._lock_id(experiment_id, operation_key),)).fetchone()["acquired"]
            connection.commit()
            if not acquired:
                raise OperationBusy("LEDGER_OPERATION_BUSY")
            # connection 关闭释放会话锁，包括异常退出；不使用会在 commit 后失效的事务锁。
            yield connection

    def register_experiment(self, experiment_id: str, *, candidate: dict, configuration: dict, inputs: dict) -> dict:
        """创建或重用完全相同的实验；同 ID 的候选、运行配置或输入变化均拒绝。"""
        _name(experiment_id, "EXPERIMENT_ID")
        if not all(isinstance(value, dict) for value in (candidate, configuration, inputs)) or not candidate:
            raise LedgerError("LEDGER_EXPERIMENT_IDENTITY_INVALID")
        identities = [digest(value) for value in (candidate, configuration, inputs)]
        with self._connect() as connection:
            connection.execute(self._query("""
                INSERT INTO {schema}.experiments
                (experiment_id,candidate,candidate_sha256,configuration,configuration_sha256,inputs,inputs_sha256)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """), (experiment_id, Jsonb(candidate), identities[0], Jsonb(configuration), identities[1],
                   Jsonb(inputs), identities[2]))
            row = connection.execute(self._query("SELECT * FROM {schema}.experiments WHERE experiment_id=%s"),
                                     (experiment_id,)).fetchone()
            if [row[key] for key in ("candidate_sha256", "configuration_sha256", "inputs_sha256")] != identities:
                raise IdentityConflict("LEDGER_EXPERIMENT_IDENTITY_CONFLICT")
            return _public(row)

    def get_experiment(self, experiment_id: str) -> dict | None:
        with self._connect() as connection:
            return _public(connection.execute(self._query("SELECT * FROM {schema}.experiments WHERE experiment_id=%s"),
                                              (experiment_id,)).fetchone())

    def _get(self, connection, experiment_id, operation_key):
        return connection.execute(self._query("SELECT * FROM {schema}.operations WHERE experiment_id=%s AND operation_key=%s"),
                                  (experiment_id, operation_key)).fetchone()

    def get_operation(self, experiment_id: str, operation_key: str) -> dict | None:
        with self._connect() as connection:
            return _public(self._get(connection, experiment_id, operation_key))

    def list_operations(self, experiment_id: str) -> list[dict]:
        with self._connect() as connection:
            return _public(connection.execute(self._query("SELECT * FROM {schema}.operations WHERE experiment_id=%s ORDER BY created_at,operation_key"),
                                              (experiment_id,)).fetchall())

    def history(self, experiment_id: str, operation_key: str) -> list[dict]:
        with self._connect() as connection:
            return _public(connection.execute(self._query("SELECT * FROM {schema}.operation_events WHERE experiment_id=%s AND operation_key=%s ORDER BY event_id"),
                                              (experiment_id, operation_key)).fetchall())

    def resume_summary(self, experiment_id: str) -> dict:
        """只读恢复预览；DISPATCHED 也需人工对账，查询本身不更改活跃操作。"""
        rows = self.list_operations(experiment_id)
        counts = {state: sum(row["status"] == state for row in rows) for state in STATES}
        return {"experiment_id": experiment_id, "counts": counts, "total": len(rows),
                "reusable": [row["operation_key"] for row in rows if row["status"] == "COMPLETED"],
                "safe_to_dispatch": [row["operation_key"] for row in rows if row["status"] == "RESERVED"],
                "requires_reconciliation": [row["operation_key"] for row in rows if row["status"] in {"DISPATCHED", "UNKNOWN"}],
                "dispatch_count": sum(row["dispatch_count"] for row in rows)}

    def _reserve(self, connection, experiment_id, operation_key, kind, effect, request):
        for value, label in ((experiment_id, "EXPERIMENT_ID"), (operation_key, "OPERATION_KEY"), (kind, "KIND")):
            _name(value, label)
        if effect not in {"READ", "WRITE"} or not isinstance(request, dict):
            raise LedgerError("LEDGER_OPERATION_INVALID")
        request_sha = digest(request)
        operation_id = digest([experiment_id, operation_key])
        inserted = connection.execute(self._query("""
            INSERT INTO {schema}.operations
            (experiment_id,operation_key,operation_id,kind,effect,request,request_sha256,
             candidate_sha256,configuration_sha256,inputs_sha256,status)
            SELECT experiment_id,%s,%s,%s,%s,%s,%s,candidate_sha256,configuration_sha256,inputs_sha256,'RESERVED'
            FROM {schema}.experiments WHERE experiment_id=%s ON CONFLICT DO NOTHING RETURNING *
        """), (operation_key, operation_id, kind, effect, Jsonb(request), request_sha, experiment_id)).fetchone()
        row = inserted or self._get(connection, experiment_id, operation_key)
        if row is None:
            raise LedgerError("LEDGER_EXPERIMENT_NOT_FOUND")
        if (row["kind"], row["effect"], row["request_sha256"]) != (kind, effect, request_sha):
            raise IdentityConflict("LEDGER_OPERATION_IDENTITY_CONFLICT", _public(row))
        if inserted:
            self._event(connection, row, "RESERVED", None, {})
        connection.commit()
        return row

    def reserve(self, experiment_id: str, operation_key: str, *, kind: str, effect: str, request: dict) -> dict:
        """只登记计划，不取得发送权限；重启后 RESERVED 可由 execute 原子领取。"""
        with self._locked(experiment_id, operation_key) as connection:
            return _public(self._reserve(connection, experiment_id, operation_key, kind, effect, request))

    def _event(self, connection, row, event_kind, previous_state, payload):
        connection.execute(self._query("""
            INSERT INTO {schema}.operation_events
            (experiment_id,operation_key,event_kind,previous_state,state,payload) VALUES (%s,%s,%s,%s,%s,%s)
        """), (row["experiment_id"], row["operation_key"], event_kind, previous_state, row["status"], Jsonb(payload)))

    def _transition(self, connection, row, state, event_kind, **changes):
        previous = row["status"]
        updated = {**row, **{key: value for key, value in changes.items() if key not in {"evidence", "linked_evidence"}}, "status": state}
        ref = changes.get("evidence")
        linked = changes.get("linked_evidence", [])
        if ref or linked:
            updated["evidence_refs"] = row["evidence_refs"] + linked + ([ref] if ref else [])
        connection.execute(self._query("""
            UPDATE {schema}.operations SET status=%s, dispatch_count=%s, request_sent=%s,
                result=%s, last_error=%s, unknown_receipt=%s, evidence_refs=%s, updated_at=clock_timestamp()
            WHERE experiment_id=%s AND operation_key=%s
        """), (state, updated["dispatch_count"], updated["request_sent"], Jsonb(updated["result"]),
               Jsonb(updated["last_error"]), Jsonb(updated["unknown_receipt"]), Jsonb(updated["evidence_refs"]),
               row["experiment_id"], row["operation_key"]))
        current = self._get(connection, row["experiment_id"], row["operation_key"])
        self._event(connection, current, event_kind, previous, _public(changes))
        connection.commit()
        return current

    def _evidence(self, row, label, payload):
        content = canonical({"experiment_id": row["experiment_id"], "operation_id": row["operation_id"],
                             "operation_key": row["operation_key"], "candidate_sha256": row["candidate_sha256"],
                             "configuration_sha256": row["configuration_sha256"], "inputs_sha256": row["inputs_sha256"],
                             "request_sha256": row["request_sha256"], "label": label, "payload": payload})
        path = self.evidence_dir / (row["operation_id"] + "-" + uuid4().hex + ".json")
        # 独占创建 + fsync 先于 DB 提交。若 DB 随后失败，最多留下未引用文件，绝不伪造已保存回执。
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "label": label}

    def verify_evidence(self, reference: dict) -> bool:
        """读取前验证路径和内容；完成回放也拒绝被移走或篡改的本地证据。"""
        try:
            path = Path(reference["path"]).resolve()
            if not path.is_relative_to(self.evidence_dir):
                return False
            content = path.read_bytes()
            return len(content) == reference["bytes"] and hashlib.sha256(content).hexdigest() == reference["sha256"]
        except (OSError, KeyError, TypeError, ValueError):
            return False

    def _unknown(self, connection, row, code, error_type=None):
        error = {"code": code, "exception_type": error_type, "request_sent": row["request_sent"]}
        ref = self._evidence(row, "unknown", error)
        unknown = row["unknown_receipt"] or {"previous_state": row["status"], "error": error, "evidence": ref}
        return self._transition(connection, row, "UNKNOWN", "UNKNOWN", last_error=error,
                                unknown_receipt=unknown, evidence=ref)

    def execute(self, experiment_id: str, operation_key: str, *, kind: str, effect: str,
                request: dict, callback: Callable[[Dispatch], dict]) -> dict:
        """原子领取一次操作并执行；完成回放，未知与失败拒绝隐式重发。

        返回完整操作记录，result 是 callback 结果，replayed 说明本次是否复用回执。
        外部副作用前先提交 DISPATCHED。所有普通异常和 KeyboardInterrupt 均保守记 UNKNOWN；
        os._exit/断电来不及写 UNKNOWN 时，下一次持锁恢复将遗留 DISPATCHED 转为 UNKNOWN。
        """
        with self._locked(experiment_id, operation_key) as connection:
            row = self._reserve(connection, experiment_id, operation_key, kind, effect, request)
            if row["status"] == "COMPLETED":
                if not all(self.verify_evidence(ref) for ref in row["evidence_refs"]):
                    raise LedgerError("LEDGER_EVIDENCE_INTEGRITY_FAILED", _public(row))
                return {**_public(row), "replayed": True}
            if row["status"] == "DISPATCHED":
                row = self._unknown(connection, row, "INTERRUPTED_AFTER_DISPATCH")
            if row["status"] == "UNKNOWN":
                raise OperationUnknown("LEDGER_OPERATION_UNKNOWN_RECONCILE_REQUIRED", _public(row))
            if row["status"] == "FAILED":
                raise LedgerError("LEDGER_OPERATION_FAILED", _public(row))
            row = self._transition(connection, row, "DISPATCHED", "DISPATCHED",
                                   dispatch_count=row["dispatch_count"] + 1, request_sent=None)
            dispatch = Dispatch(self, connection, row)
            try:
                result = callback(dispatch)
                if not isinstance(result, dict):
                    raise LedgerError("LEDGER_CALLBACK_RESULT_INVALID")
                ref = self._evidence(dispatch.operation, "result", result)
                row = self._transition(connection, dispatch.operation, "COMPLETED", "COMPLETED",
                                       result=result, evidence=ref)
                return {**_public(row), "replayed": False}
            except NotSentError as error:
                # 已有真实 HTTP 回执时，适配器不能再声称未发送；保守保留为未知。
                if dispatch.operation["request_sent"] is True:
                    row = self._unknown(connection, dispatch.operation, "CONTRADICTORY_NOT_SENT", type(error).__name__)
                    raise OperationUnknown("LEDGER_OPERATION_UNKNOWN_RECONCILE_REQUIRED", _public(row)) from None
                ref = self._evidence(dispatch.operation, "not_sent", {"code": error.code})
                row = self._transition(connection, dispatch.operation, "FAILED", "FAILED", request_sent=False,
                                       last_error={"code": error.code}, evidence=ref)
                raise LedgerError("LEDGER_OPERATION_FAILED_NOT_SENT", _public(row)) from None
            except BaseException as error:
                row = self._unknown(connection, dispatch.operation, "CALLBACK_INTERRUPTED", type(error).__name__)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise OperationUnknown("LEDGER_OPERATION_UNKNOWN_RECONCILE_REQUIRED", _public(row)) from None
            finally:
                dispatch.active = False

    def resolve_unknown(self, experiment_id: str, operation_key: str, *, reconciliation_key: str,
                        request: dict, callback: Callable[[Dispatch], dict], reason: str) -> dict:
        """显式只读对账：原 UNKNOWN 回执永久保留，观察操作也有独立稳定身份。

        request.method 只允许 GET/HEAD/SELECT，实际 callback 仍由受信任适配器保证只读。
        callback 返回 {outcome: COMPLETED|NOT_APPLIED|UNKNOWN, result: {...}}。COMPLETED
        必须来自实际读回，NOT_APPLIED 仅关闭为 FAILED，均不提供重新发送权限。
        """
        _name(reconciliation_key, "RECONCILIATION_KEY")
        if not isinstance(request, dict) or request.get("method") not in {"GET", "HEAD", "SELECT"}:
            raise LedgerError("LEDGER_RECONCILIATION_MUST_BE_READ_ONLY")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise LedgerError("LEDGER_RECONCILIATION_REASON_REQUIRED")
        with self._locked(experiment_id, operation_key) as connection:
            row = self._get(connection, experiment_id, operation_key)
            if row is None:
                raise LedgerError("LEDGER_OPERATION_NOT_FOUND")
            if row["status"] == "DISPATCHED":
                row = self._unknown(connection, row, "INTERRUPTED_AFTER_DISPATCH")
            if row["status"] != "UNKNOWN":
                raise LedgerError("LEDGER_RECONCILIATION_REQUIRES_UNKNOWN", _public(row))
            resolution = self.execute(experiment_id, "reconcile." + digest([operation_key, reconciliation_key]),
                                      kind="reconcile", effect="READ",
                                      request={"operation_id": row["operation_id"], "reason": reason, "observation": request},
                                      callback=callback)
            observed = resolution["result"]
            outcome = observed.get("outcome")
            if outcome not in {"COMPLETED", "NOT_APPLIED", "UNKNOWN"} or not isinstance(observed.get("result"), dict):
                raise LedgerError("LEDGER_RECONCILIATION_RESULT_INVALID")
            ref = self._evidence(row, "reconciliation", {"reason": reason, "observation_operation": resolution["operation_id"],
                                                         "observation_evidence": resolution["evidence_refs"], "observed": observed})
            state = {"COMPLETED": "COMPLETED", "NOT_APPLIED": "FAILED", "UNKNOWN": "UNKNOWN"}[outcome]
            # 对账只补充当前结论，不删除 unknown_receipt，也不改原请求的实际发送标记。
            # 同时链接只读操作的原始证据，父操作回放也必须验证它们，不能只校验摘要包装文件。
            updated = self._transition(connection, row, state, "RECONCILED", evidence=ref,
                                       linked_evidence=resolution["evidence_refs"],
                                       result=observed["result"] if state == "COMPLETED" else row["result"])
            return {**_public(updated), "replayed": False}
