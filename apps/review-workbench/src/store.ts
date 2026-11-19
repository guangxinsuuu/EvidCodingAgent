import { createHash, randomUUID } from "node:crypto";
import pg, { type PoolClient } from "pg";
import { REVIEW_ROLES, ReviewError, type Actor, type CommandBasis, type CommandResult, type DecisionCommand,
  type EvidenceRequestCommand, type ImportCase, type ImportEvidence, type ReviewRole } from "./types.js";

const HASH = /^[a-f0-9]{64}$/;
const SHA = /^[a-f0-9]{40}$/;
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
export const digest = (value: string): string => createHash("sha256").update(value).digest("hex");

/** 对象键排序使 HTTP/CLI 字段顺序不影响幂等判定；数组顺序仍是材料的一部分。 */
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
  const result = JSON.stringify(value);
  if (result === undefined) throw new ReviewError("INVALID_REQUEST", 400);
  return result;
}
function text(value: unknown, max: number, min = 1): value is string {
  return typeof value === "string" && value.trim().length >= min && value.length <= max;
}
function object(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value); }

/** 所有入口共用结构校验；未知字段（尤其 actor / tenant）必须拒绝，不能悄悄忽略。 */
export function parseCommand(value: unknown, kind: "DECISION"): DecisionCommand;
export function parseCommand(value: unknown, kind: "EVIDENCE_REQUEST"): EvidenceRequestCommand;
export function parseCommand(value: unknown, kind: "DECISION" | "EVIDENCE_REQUEST"): DecisionCommand | EvidenceRequestCommand {
  const keys = ["idempotency_key", "expected_revision", "candidate_sha", "bundle_digest", "reason", "evidence_refs", kind === "DECISION" ? "decision" : "question"];
  if (!object(value) || Object.keys(value).length !== keys.length || Object.keys(value).some(k => !keys.includes(k))) throw new ReviewError("INVALID_REQUEST", 400);
  if (!text(value.idempotency_key, 120, 8) || !ID.test(value.idempotency_key) || !Number.isSafeInteger(value.expected_revision) || Number(value.expected_revision) < 1
    || typeof value.candidate_sha !== "string" || !SHA.test(value.candidate_sha) || typeof value.bundle_digest !== "string" || !HASH.test(value.bundle_digest)
    || !text(value.reason, 3000) || !Array.isArray(value.evidence_refs) || value.evidence_refs.length > 30
    || value.evidence_refs.some(v => typeof v !== "string" || !ID.test(v)) || new Set(value.evidence_refs).size !== value.evidence_refs.length) throw new ReviewError("INVALID_REQUEST", 400);
  if (kind === "DECISION") {
    if (value.decision !== "ACKNOWLEDGE" && value.decision !== "REQUEST_CHANGES") throw new ReviewError("INVALID_REQUEST", 400);
    if (value.decision === "ACKNOWLEDGE" && value.evidence_refs.length === 0) throw new ReviewError("EVIDENCE_REFERENCE_REQUIRED", 400);
  } else if (!text(value.question, 3000)) throw new ReviewError("INVALID_REQUEST", 400);
  return value as unknown as DecisionCommand | EvidenceRequestCommand;
}

interface CaseRow { id: string; tenant_id: string; bundle: ImportCase; bundle_digest: string; revision: number; review_status: string; created_at: Date }
interface ObligationRow { role: ReviewRole; title: string; instruction: string; status: string }
interface EvidenceRow { id: string; case_id: string; material: ImportEvidence }
interface CommandRow { id: string; actor_id: string; role: ReviewRole; kind: string; payload: DecisionCommand | EvidenceRequestCommand; response: CommandResult; revision: number; created_at: Date }

/**
 * main/import → importCase；Web/CLI → registerReviewRoutes → command。
 * 这里只管理审查对象，不调用 Runtime.projection、模型或业务写工具。
 * 人员确认只能改变自己的 obligation；机器结果与材料摘要始终保持导入时的版本。
 */
export class ReviewStore {
  readonly pool: pg.Pool;
  constructor(databaseUrl: string) { this.pool = new pg.Pool({ connectionString: databaseUrl, max: 5, connectionTimeoutMillis: 3000 }); }
  async close(): Promise<void> { await this.pool.end(); }
  async migrate(): Promise<void> {
    await this.transaction(async c => {
      // 两个本机入口同时启动时也串行建表；IF NOT EXISTS 本身不能避免 PG 类型目录竞争。
      await c.query("SELECT pg_advisory_xact_lock(184321, 6)");
      await c.query(`CREATE SCHEMA IF NOT EXISTS egap_review;
      CREATE TABLE IF NOT EXISTS egap_review.cases (
        id text PRIMARY KEY, tenant_id text NOT NULL, bundle jsonb NOT NULL, bundle_digest text NOT NULL,
        revision integer NOT NULL DEFAULT 1, review_status text NOT NULL DEFAULT 'REQUIRED', created_at timestamptz NOT NULL DEFAULT now());
      CREATE TABLE IF NOT EXISTS egap_review.obligations (
        case_id text NOT NULL REFERENCES egap_review.cases(id), role text NOT NULL, title text NOT NULL, instruction text NOT NULL,
        status text NOT NULL DEFAULT 'PENDING', PRIMARY KEY(case_id,role));
      CREATE TABLE IF NOT EXISTS egap_review.evidence (
        id text PRIMARY KEY, case_id text NOT NULL REFERENCES egap_review.cases(id), material jsonb NOT NULL);
      CREATE TABLE IF NOT EXISTS egap_review.commands (
        id uuid PRIMARY KEY, case_id text NOT NULL REFERENCES egap_review.cases(id), actor_id text NOT NULL, role text NOT NULL,
        kind text NOT NULL, idempotency_key text NOT NULL, payload_digest text NOT NULL, payload jsonb NOT NULL,
        response jsonb NOT NULL, revision integer NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(case_id,idempotency_key));
      CREATE TABLE IF NOT EXISTS egap_review.events (
        case_id text NOT NULL REFERENCES egap_review.cases(id), seq integer NOT NULL, kind text NOT NULL,
        payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(case_id,seq));`);
    });
  }
  private async transaction<T>(fn: (client: PoolClient) => Promise<T>, readOnly = false): Promise<T> {
    const client = await this.pool.connect();
    try {
      // 多条查询组成一个页面时，固定快照避免 revision 与决定列表来自不同提交。
      await client.query(readOnly ? "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" : "BEGIN");
      const result = await fn(client);
      await client.query("COMMIT");
      return result;
    } catch (error) { await client.query("ROLLBACK"); throw error; }
    finally { client.release(); }
  }

  /**
   * 仅可信本机导入命令调用。相同 ID + 相同内容可重放，内容变化须创建新 case，不能覆盖历史。
   * 摘要由服务端重算；来源文件摘要与展示摘录摘要分开，避免把摘录冒充完整原件。
   */
  async importCase(input: ImportCase): Promise<{ id: string; bundle_digest: string; imported: boolean }> {
    validateImport(input);
    const bundleDigest = digest(canonical(input));
    return this.transaction(async c => {
      // INSERT ON CONFLICT 等待并发事务结束；随后比较不可变摘要，避免双重导入。
      const insert = await c.query("INSERT INTO egap_review.cases(id,tenant_id,bundle,bundle_digest) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING id", [input.id, input.tenant_id, input, bundleDigest]);
      if (insert.rowCount === 0) {
        const existing = await c.query<CaseRow>("SELECT * FROM egap_review.cases WHERE id=$1", [input.id]);
        if (existing.rows[0]?.bundle_digest !== bundleDigest) throw new ReviewError("IMMUTABLE_IMPORT_CONFLICT", 409);
        assertBundle(existing.rows[0]);
        return { id: input.id, bundle_digest: bundleDigest, imported: false };
      }
      for (const obligation of input.obligations) await c.query("INSERT INTO egap_review.obligations(case_id,role,title,instruction) VALUES($1,$2,$3,$4)", [input.id, obligation.role, obligation.title, obligation.instruction]);
      for (const evidence of input.evidence) await c.query("INSERT INTO egap_review.evidence(id,case_id,material) VALUES($1,$2,$3)", [evidence.id, input.id, evidence]);
      await c.query("INSERT INTO egap_review.events(case_id,seq,kind,payload) VALUES($1,1,'HISTORICAL_IMPORT',$2)", [input.id, { candidate_sha: input.candidate_sha, bundle_digest: bundleDigest, source_sha256: input.source_sha256, execution_mode: input.execution_mode }]);
      return { id: input.id, bundle_digest: bundleDigest, imported: true };
    });
  }

  /** ACL 条件进入 SQL；未知对象与无权限对象统一 404，避免跨租户枚举。 */
  private async authorizedCase(c: PoolClient, id: string, actor: Actor, lock = false): Promise<CaseRow> {
    const result = await c.query<CaseRow>(`SELECT c.* FROM egap_review.cases c WHERE c.id=$1 AND c.tenant_id=$2
      AND EXISTS(SELECT 1 FROM egap_review.obligations o WHERE o.case_id=c.id AND o.role=$3) ${lock ? "FOR UPDATE OF c" : ""}`, [id, actor.tenant_id, actor.role]);
    if (!result.rows[0]) throw new ReviewError("NOT_FOUND", 404);
    // 不可变 bundle 是版本绑定依据；异常存储修改不能借仍相同的 digest 字段蒙混过关。
    assertBundle(result.rows[0]);
    return result.rows[0];
  }

  async listTasks(actor: Actor): Promise<{ tasks: Record<string, unknown>[] }> {
    const result = await this.pool.query(`SELECT c.bundle,c.id AS case_id,c.bundle->>'title' AS title,c.bundle->>'candidate_sha' AS candidate_sha,
      c.bundle_digest,c.revision,c.review_status,c.bundle->>'execution_mode' AS execution_mode,
      o.role,o.title AS obligation_title,o.status AS obligation_status FROM egap_review.cases c
      JOIN egap_review.obligations o ON o.case_id=c.id WHERE c.tenant_id=$1 AND o.role=$2 ORDER BY c.created_at DESC,c.id`, [actor.tenant_id, actor.role]);
    return { tasks: (result.rows as (Record<string, unknown> & { bundle: ImportCase; bundle_digest: string })[]).map(row => {
      assertBundle(row);
      const { bundle: _bundle, ...task } = row;
      return task;
    }) };
  }

  async getCase(id: string, actor: Actor): Promise<Record<string, unknown>> {
    return this.transaction(async c => {
      const row = await this.authorizedCase(c, id, actor);
      const obligations = (await c.query<ObligationRow>("SELECT role,title,instruction,status FROM egap_review.obligations WHERE case_id=$1 ORDER BY role", [id])).rows;
      const commands = (await c.query<CommandRow>("SELECT id,actor_id,role,kind,payload,response,revision,created_at FROM egap_review.commands WHERE case_id=$1 ORDER BY revision", [id])).rows;
      const events = (await c.query("SELECT seq,kind,payload,created_at FROM egap_review.events WHERE case_id=$1 ORDER BY seq", [id])).rows;
      const present = (command: CommandRow): Record<string, unknown> => ({ id: command.id, actor_id: command.actor_id, role: command.role,
        ...command.payload, idempotency_key: undefined, expected_revision: undefined,
        revision: command.revision, status: command.response.status, created_at: command.created_at });
      return { ...row.bundle, revision: row.revision, bundle_digest: row.bundle_digest, review_status: row.review_status,
        // 内容只能由 evidence 路由按对象 ACL 返回；列表留摘要，避免页面载入全部材料。
        evidence: row.bundle.evidence.map(({ content: _content, ...meta }) => meta), obligations,
        actor_obligation: obligations.find(o => o.role === actor.role),
        decisions: commands.filter(cmd => cmd.kind === "DECISION").map(present),
        evidence_requests: commands.filter(cmd => cmd.kind === "EVIDENCE_REQUEST").map(present), events };
    }, true);
  }

  async getEvidence(id: string, actor: Actor): Promise<Record<string, unknown>> {
    return this.transaction(async c => {
      const result = await c.query<EvidenceRow>("SELECT id,case_id,material FROM egap_review.evidence WHERE id=$1", [id]);
      const row = result.rows[0];
      if (!row) throw new ReviewError("NOT_FOUND", 404);
      const parent = await this.authorizedCase(c, row.case_id, actor);
      assertEvidence(row, parent);
      return { ...row.material, case_id: row.case_id, candidate_sha: parent.bundle.candidate_sha, bundle_digest: parent.bundle_digest };
    }, true);
  }

  async decide(id: string, actor: Actor, value: unknown): Promise<CommandResult> {
    return this.command(id, actor, "DECISION", parseCommand(value, "DECISION"));
  }
  async requestEvidence(id: string, actor: Actor, value: unknown): Promise<CommandResult> {
    return this.command(id, actor, "EVIDENCE_REQUEST", parseCommand(value, "EVIDENCE_REQUEST"));
  }

  /**
   * 一个 case 一把行锁：ACL → 幂等重放 → 版本/证据 → 决定+事件+revision 同事务提交。
   * 幂等重放先于旧 revision 拒绝，保证已提交但客户端超时的命令可恢复同一回执。
   * 没有 Agent 执行桥时补证请求停在 WAITING_CAPABILITY，绝不生成伪实验或 PASS。
   */
  private async command(id: string, actor: Actor, kind: "DECISION" | "EVIDENCE_REQUEST", payload: DecisionCommand | EvidenceRequestCommand): Promise<CommandResult> {
    const commandDigest = digest(canonical({ kind, actor_id: actor.id, role: actor.role, payload }));
    return this.transaction(async c => {
      const row = await this.authorizedCase(c, id, actor, true);
      const prior = await c.query<{ payload_digest: string; response: CommandResult }>("SELECT payload_digest,response FROM egap_review.commands WHERE case_id=$1 AND idempotency_key=$2", [id, payload.idempotency_key]);
      if (prior.rows[0]) {
        if (prior.rows[0].payload_digest !== commandDigest) throw new ReviewError("IDEMPOTENCY_CONFLICT", 409);
        return prior.rows[0].response;
      }
      assertBasis(row, payload);
      const permittedRefs = new Set(row.bundle.evidence.map(e => e.id));
      if (payload.evidence_refs.some(ref => !permittedRefs.has(ref))) throw new ReviewError("INVALID_EVIDENCE_REFERENCE", 400);
      // 引用也必须指向仍完整的材料；不能只在打开证据页时校验，随后确认已变质的镜像。
      const cited = await c.query<EvidenceRow>("SELECT id,case_id,material FROM egap_review.evidence WHERE case_id=$1 AND id=ANY($2::text[])", [id, payload.evidence_refs]);
      if (cited.rows.length !== payload.evidence_refs.length) throw new ReviewError("EVIDENCE_INTEGRITY_ERROR", 409);
      for (const evidence of cited.rows) assertEvidence(evidence, row);
      const decision = kind === "DECISION" ? (payload as DecisionCommand).decision : undefined;
      const status = kind === "EVIDENCE_REQUEST" ? "WAITING_CAPABILITY" : decision === "ACKNOWLEDGE" ? "ACKNOWLEDGED" : "CHANGES_REQUESTED";
      const response: CommandResult = { id: randomUUID(), case_id: id, revision: row.revision + 1, candidate_sha: row.bundle.candidate_sha,
        bundle_digest: row.bundle_digest, kind, status, actor_id: actor.id, role: actor.role };
      await c.query("INSERT INTO egap_review.commands(id,case_id,actor_id,role,kind,idempotency_key,payload_digest,payload,response,revision) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
        [response.id, id, actor.id, actor.role, kind, payload.idempotency_key, commandDigest, payload, response, response.revision]);
      const ownPending = await c.query("SELECT 1 FROM egap_review.commands WHERE case_id=$1 AND role=$2 AND kind='EVIDENCE_REQUEST' LIMIT 1", [id, actor.role]);
      // 决定回执可记录“已确认所引材料”，但本角色未结补证义务不能因此显示为完成。
      await c.query("UPDATE egap_review.obligations SET status=$3 WHERE case_id=$1 AND role=$2", [id, actor.role, ownPending.rowCount ? "WAITING_EVIDENCE" : status]);
      // 补证请求没有消费器或关闭协议，因此即使后续确认材料，未解决请求仍保留为阻塞。
      const pending = await c.query("SELECT 1 FROM egap_review.commands WHERE case_id=$1 AND kind='EVIDENCE_REQUEST' LIMIT 1", [id]);
      const obligations = (await c.query<{ status: string }>("SELECT status FROM egap_review.obligations WHERE case_id=$1", [id])).rows;
      const reviewStatus = pending.rowCount ? "WAITING_EVIDENCE" : obligations.some(o => o.status === "CHANGES_REQUESTED") ? "CHANGES_REQUESTED"
        : obligations.every(o => o.status === "ACKNOWLEDGED") ? "ACKNOWLEDGED" : "REQUIRED";
      await c.query("UPDATE egap_review.cases SET revision=$2,review_status=$3 WHERE id=$1", [id, response.revision, reviewStatus]);
      await c.query("INSERT INTO egap_review.events(case_id,seq,kind,payload) VALUES($1,$2,$3,$4)", [id, response.revision, kind,
        { command_id: response.id, actor_id: actor.id, role: actor.role, candidate_sha: response.candidate_sha, bundle_digest: response.bundle_digest, status }]);
      return response;
    });
  }
}
export { ReviewStore as Store };

function assertBundle(row: { bundle: ImportCase; bundle_digest: string }): void {
  if (digest(canonical(row.bundle)) !== row.bundle_digest) throw new ReviewError("BUNDLE_INTEGRITY_ERROR", 409);
}

/** 镜像的自身 hash 只验证内存一致；还必须与导入时冻结的完整元数据/内容逐项绑定。 */
function assertEvidence(row: EvidenceRow, parent: CaseRow): void {
  const original = parent.bundle.evidence.find(evidence => evidence.id === row.id);
  if (!original || row.case_id !== parent.id || digest(row.material.content) !== row.material.sha256
    || canonical(row.material) !== canonical(original)) throw new ReviewError("EVIDENCE_INTEGRITY_ERROR", 409);
}

function assertBasis(row: CaseRow, command: CommandBasis): void {
  if (row.revision !== command.expected_revision) throw new ReviewError("STALE_REVISION", 409);
  if (row.bundle.candidate_sha !== command.candidate_sha || row.bundle_digest !== command.bundle_digest) throw new ReviewError("STALE_MATERIAL", 409);
}

function validateImport(input: ImportCase): void {
  if (!object(input) || !text(input.id, 160) || !ID.test(input.id) || !text(input.tenant_id, 160) || !ID.test(input.tenant_id)
    || !text(input.title, 500) || !text(input.execution_mode, 160) || !SHA.test(input.candidate_sha)
    || (input.baseline_sha !== null && !SHA.test(input.baseline_sha)) || !text(input.source_ref, 500) || !HASH.test(input.source_sha256)
    || !object(input.machine_result) || !Array.isArray(input.limitations) || input.limitations.some(v => !text(v, 2000))
    || !Array.isArray(input.evidence) || input.evidence.length < 1 || input.evidence.length > 30
    || !Array.isArray(input.obligations) || input.obligations.length !== 3 || canonical(input).length > 2_000_000) throw new ReviewError("INVALID_IMPORT", 400);
  const roles = new Set(input.obligations.map(o => o.role));
  if (roles.size !== 3 || REVIEW_ROLES.some(role => !roles.has(role)) || input.obligations.some(o => !text(o.title, 500) || !text(o.instruction, 5000))) throw new ReviewError("INVALID_IMPORT", 400);
  const refs = new Set<string>();
  for (const e of input.evidence) {
    if (!text(e.id, 160) || !ID.test(e.id) || refs.has(e.id) || !text(e.title, 500) || !text(e.kind, 160) || !text(e.content, 128_000)
      || !HASH.test(e.sha256) || digest(e.content) !== e.sha256 || !text(e.source_ref, 500) || !HASH.test(e.source_sha256)
      || !Array.isArray(e.limitations) || e.limitations.some(v => !text(v, 2000))) throw new ReviewError("INVALID_IMPORT", 400);
    refs.add(e.id);
  }
}
