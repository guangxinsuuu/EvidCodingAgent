"""独立网关账本：已登记调用 → 工具收据 → 模型回执；不写SUT Job或业务草稿表。

job_id绑定请求和网关配置。STARTED已提交后，无论调用者超时、网关重启或费用未知，
同一请求都只能查询原调用；不能借新worker attempt或重POST再调用供应商。
"""

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class GatewayStoreError(ValueError):
    pass


_DDL = """
CREATE SCHEMA IF NOT EXISTS egap_gateway;
CREATE TABLE IF NOT EXISTS egap_gateway.budgets (
    budget_id text PRIMARY KEY, max_calls integer NOT NULL CHECK(max_calls>0),
    reserved_calls integer NOT NULL DEFAULT 0 CHECK(reserved_calls>=0),
    reserved_output_tokens bigint NOT NULL DEFAULT 0 CHECK(reserved_output_tokens>=0)
);
CREATE TABLE IF NOT EXISTS egap_gateway.calls (
    job_id text PRIMARY KEY CHECK(job_id ~ '^[a-f0-9]{32}$'),
    request_sha256 text NOT NULL, config_sha256 text NOT NULL, request_body jsonb NOT NULL,
    execution_mode text NOT NULL CHECK(execution_mode IN ('SCRIPTED_CALIBRATION','LIVE_MODEL')),
    state text NOT NULL CHECK(state IN ('STARTED','COMPLETE','FAILED','UNKNOWN')),
    draft jsonb, receipt jsonb, error_code text,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((state='COMPLETE' AND draft IS NOT NULL) OR (state<>'COMPLETE' AND draft IS NULL))
);
CREATE TABLE IF NOT EXISTS egap_gateway.tool_events (
    sequence bigserial PRIMARY KEY, job_id text NOT NULL REFERENCES egap_gateway.calls(job_id),
    event jsonb NOT NULL, observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""


class GatewayStore:
    """受信网关专属DB凭据；每次操作短连接/短事务，错误不泄露连接串。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        try:
            with psycopg.connect(self._dsn, connect_timeout=3, row_factory=dict_row,
                                options="-c statement_timeout=5000 -c lock_timeout=3000") as connection:
                yield connection
        except psycopg.Error:
            raise GatewayStoreError("GATEWAY_STORAGE_UNAVAILABLE") from None

    def migrate(self) -> None:
        with self._connection() as connection:
            connection.execute(_DDL)

    def health(self) -> bool:
        with self._connection() as connection:
            connection.execute("SELECT 1 FROM egap_gateway.calls LIMIT 1")
        return True

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return connection.execute("SELECT * FROM egap_gateway.calls WHERE job_id=%s", (job_id,)).fetchone()

    def begin(self, request: dict[str, Any], request_sha256: str, config_sha256: str,
              mode: str, max_calls: int, max_output_tokens: int, budget_id: str) -> tuple[bool, dict[str, Any]]:
        """同job原子去重并预留调用预算；只有created=True的进程可以执行模型。

        budget row锁串行化同预算内的预留，job advisory lock防不同预算配置并发重复。
        UNKNOWN不释放调用预留；本地模型以请求数/输出token上界限额，不宣称美元计费准确。
        """
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (request["job_id"],))
            existing = connection.execute("SELECT * FROM egap_gateway.calls WHERE job_id=%s", (request["job_id"],)).fetchone()
            if existing:
                if existing["request_sha256"] != request_sha256 or existing["config_sha256"] != config_sha256:
                    raise GatewayStoreError("GATEWAY_IDENTITY_CONFLICT")
                return False, existing
            if mode == "LIVE_MODEL":
                connection.execute("INSERT INTO egap_gateway.budgets(budget_id,max_calls) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                                   (budget_id, max_calls))
                budget = connection.execute("SELECT * FROM egap_gateway.budgets WHERE budget_id=%s FOR UPDATE", (budget_id,)).fetchone()
                if budget["max_calls"] != max_calls:
                    raise GatewayStoreError("GATEWAY_BUDGET_CONFIG_CONFLICT")
                if budget["reserved_calls"] >= max_calls:
                    # 已确认尚未派发的拒绝可以落FAILED，避免Worker永久GET404→POST循环。
                    # 它不增加预留，更不能用此分支把此前已发出且无回执的UNKNOWN改失败。
                    denied = connection.execute("""INSERT INTO egap_gateway.calls
                        (job_id,request_sha256,config_sha256,request_body,execution_mode,state,error_code)
                        VALUES(%s,%s,%s,%s,%s,'FAILED','MODEL_BUDGET_EXHAUSTED') RETURNING *""",
                        (request["job_id"], request_sha256, config_sha256, Jsonb(request), mode)).fetchone()
                    return False, denied
                connection.execute("UPDATE egap_gateway.budgets SET reserved_calls=reserved_calls+1,reserved_output_tokens=reserved_output_tokens+%s WHERE budget_id=%s",
                                   (max_output_tokens, budget_id))
            row = connection.execute("""INSERT INTO egap_gateway.calls
                (job_id,request_sha256,config_sha256,request_body,execution_mode,state)
                VALUES(%s,%s,%s,%s,%s,'STARTED') RETURNING *""",
                (request["job_id"], request_sha256, config_sha256, Jsonb(request), mode)).fetchone()
            return True, row

    def observe_tool(self, job_id: str, event: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute("INSERT INTO egap_gateway.tool_events(job_id,event) VALUES(%s,%s)", (job_id, Jsonb(event)))

    def finish(self, job_id: str, state: str, *, draft: dict[str, Any] | None = None,
               receipt: dict[str, Any] | None = None, error_code: str | None = None) -> dict[str, Any]:
        """完整回执可完成STARTED；丢失回执只标UNKNOWN，绝不自动从UNKNOWN重跑。

        调用者必须是本轮begin的执行者；此API不向HTTP暴露。写入失败原STARTED仍可查。
        """
        if state not in {"COMPLETE", "FAILED", "UNKNOWN"}:
            raise GatewayStoreError("GATEWAY_STATE_INVALID")
        with self._connection() as connection:
            row = connection.execute("""UPDATE egap_gateway.calls SET state=%s,draft=%s,receipt=%s,error_code=%s,
                updated_at=clock_timestamp() WHERE job_id=%s AND state='STARTED' RETURNING *""",
                (state, Jsonb(draft) if draft is not None else None, Jsonb(receipt) if receipt is not None else None,
                 error_code, job_id)).fetchone()
            if row is None:
                raise GatewayStoreError("GATEWAY_STATE_CONFLICT")
            return row

    def trace(self, job_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return connection.execute("SELECT sequence,event,observed_at FROM egap_gateway.tool_events WHERE job_id=%s ORDER BY sequence", (job_id,)).fetchall()
