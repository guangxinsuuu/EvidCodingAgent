import pg, { type PoolClient } from "pg";
import { RuntimeError, type Json, type RuntimeBundle, type RunState } from "./types.js";

export interface RunRow { bundle: RuntimeBundle; state: RunState; token_hash: string }
/** Runtime 是唯一写入者。Python 只持每个 run 的 HTTP token，不拿 PostgreSQL 凭据。 */
export class Store {
  readonly pool: pg.Pool;
  constructor(url: string) { this.pool = new pg.Pool({ connectionString: url, max: 5, connectionTimeoutMillis: 3000 }); }
  async close(): Promise<void> { await this.pool.end(); }
  async migrate(): Promise<void> {
    // 多 Worker/测试同时启动时，CREATE SCHEMA IF NOT EXISTS 仍可能在 catalog 唯一索引竞态。
    // 迁移使用专用事务 advisory lock；已完整安装时直接返回，不反复 ALTER 持有排他表锁。
    await this.transaction(async c => {
      await c.query("SELECT pg_advisory_xact_lock(671894231)");
      // 当前共有五张表；列检查区分上一版 schema 与本轮有 READ deadline 的 schema。
      const ready = await c.query<{complete:boolean}>(`SELECT
        (SELECT count(*)=5 FROM information_schema.tables WHERE table_schema='verify_runtime'
          AND table_name IN ('runs','calls','operations','outbox','events'))
        AND EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='verify_runtime' AND table_name='calls' AND column_name='request_text')
        AND EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='verify_runtime' AND table_name='operations' AND column_name='deadline') AS complete`);
      if (ready.rows[0]?.complete) return;
      await c.query(`CREATE SCHEMA IF NOT EXISTS verify_runtime;
      CREATE TABLE IF NOT EXISTS verify_runtime.runs (
        id uuid PRIMARY KEY, bundle jsonb NOT NULL, bundle_digest text NOT NULL,
        state jsonb NOT NULL, token_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
      CREATE TABLE IF NOT EXISTS verify_runtime.calls (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES verify_runtime.runs(id), command_id text NOT NULL,
        basis integer NOT NULL, status text NOT NULL, context_ref text NOT NULL,
        request jsonb NOT NULL, request_text text NOT NULL, raw_response text, proposal jsonb, usage jsonb, error_code text,
        deadline timestamptz, applied boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(run_id,command_id));
      ALTER TABLE verify_runtime.calls ADD COLUMN IF NOT EXISTS request_text text;
      CREATE TABLE IF NOT EXISTS verify_runtime.operations (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES verify_runtime.runs(id), proposal_id uuid NOT NULL,
        operation_key text NOT NULL, proposal jsonb NOT NULL, status text NOT NULL, result jsonb,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(run_id,proposal_id), UNIQUE(run_id,operation_key));
      ALTER TABLE verify_runtime.operations ADD COLUMN IF NOT EXISTS deadline timestamptz;
      CREATE TABLE IF NOT EXISTS verify_runtime.outbox (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES verify_runtime.runs(id), kind text NOT NULL,
        status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
      CREATE TABLE IF NOT EXISTS verify_runtime.events (
        run_id uuid NOT NULL REFERENCES verify_runtime.runs(id), seq integer NOT NULL,
        kind text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(run_id,seq));`);
    });
  }
  /** 同一个 client 内事务、行锁和记账；不得把中途查询分发给 pool 中另一条连接。 */
  async transaction<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
    const c = await this.pool.connect();
    try { await c.query("BEGIN"); const result = await fn(c); await c.query("COMMIT"); return result; }
    catch (e) { await c.query("ROLLBACK"); throw e; } finally { c.release(); }
  }
  async lock(c: PoolClient, id: string): Promise<RunRow> {
    const rows = await c.query<RunRow>("SELECT bundle,state,token_hash FROM verify_runtime.runs WHERE id=$1 FOR UPDATE", [id]);
    if (!rows.rows[0]) throw new RuntimeError("RUN_NOT_FOUND", 404);
    return rows.rows[0];
  }
  async save(c: PoolClient, state: RunState): Promise<void> {
    await c.query("UPDATE verify_runtime.runs SET state=$2 WHERE id=$1", [state.run_id, state]);
  }
  async event(c: PoolClient, state: RunState, kind: string, payload: Json): Promise<void> {
    state.event_seq += 1;
    await c.query("INSERT INTO verify_runtime.events(run_id,seq,kind,payload) VALUES($1,$2,$3,$4)", [state.run_id, state.event_seq, kind, payload]);
  }
}
