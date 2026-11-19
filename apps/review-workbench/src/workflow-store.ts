import { randomUUID } from "node:crypto";
import type { Pool, PoolClient } from "pg";
import { parseEvidenceReviewInput, evidenceReviewDigest, type EvidenceReviewInput } from "@egap/contracts";
import { canonical, digest } from "./store.js";
import { ReviewError, type Actor } from "./types.js";
import type { Budget, ExecutionContext, EvidenceItem, EvidenceMaterial, RequestSnapshot, RunMapping, RuntimeResult, WorkflowBasis, WorkflowSeed, WorkItem } from "./workflow-types.js";

const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const HASH = /^[a-f0-9]{64}$/;
const BASE_KEYS = ["idempotency_key", "expected_task_revision", "expected_assignment_revision", "expected_case_scope_revision", "reviewed_bundle_id", "reviewed_bundle_digest", "candidate_sha"];
export const REVIEW_POLICY = "evidence-review-local-v1";
export const ITEM_TEMPLATES = [
  { kind: "SAVED_PRODUCT_DRAFT", title: "已保存客服草稿", acceptance_criteria: ["指定 Job、阶段和 revision 的已保存草稿文本；不得冒充模型原始响应"] },
  { kind: "DELIVERED_PRIVATE_NOTE", title: "实际最终私有备注", acceptance_criteria: ["指定 Job 的最终消息正文与准确消息 ID"] },
  { kind: "DELIVERY_RECEIPT", title: "草稿与最终交付关联", acceptance_criteria: ["交付回执的 Job 与最终消息 ID 和正文属于同一对象"] },
  { kind: "RAW_PROVIDER_RESPONSE", title: "模型供应商原始响应", acceptance_criteria: ["必须是采集的模型原件，不能从草稿重建；未采集明确报告缺口"] },
];
interface CaseRow { id: string; tenant_id: string; data: Record<string, unknown>; scope_revision: number; event_seq: number; current_bundle_id: string; owner_id: string; actors: Actor[]; archive: WorkflowSeed["archive"]; budget_cap: Budget; budget_reserved: Budget; budget_consumed: Budget }
interface TaskRow { id: string; case_id: string; obligation_id: string; role: string; title: string; assignee_id: string | null; task_revision: number; assignment_revision: number; status: string; claimed: boolean; bound_bundle_id: string; bound_bundle_digest: string }
interface BundleRow { id: string; case_id: string; parent_id: string | null; digest: string; manifest: Record<string, unknown>[] }
interface RequestRow { id: string; case_id: string; task_id: string; request_spec_revision: number; spec_digest: string; input: EvidenceReviewInput; input_digest: string; execution_state: string; review_state: string; fulfillment_state: string; current_response_id: string | null; withdrawn: boolean; clarification: string | null; execution_context: Partial<ExecutionContext> }
function object(v: unknown): v is Record<string, unknown> { return v !== null && typeof v === "object" && !Array.isArray(v); }
function fail(code: string, status = 409): never { throw new ReviewError(code, status); }
function str(v: unknown, max = 3000): v is string { return typeof v === "string" && v.trim().length > 0 && v.length <= max; }
function budget(v: Budget): void { if (![v.model_calls, v.read_calls].every(x => Number.isSafeInteger(x) && x >= 0)) fail("INVALID_BUDGET", 400); }
function plus(a: Budget, b: Budget): Budget { return { model_calls: a.model_calls + b.model_calls, read_calls: a.read_calls + b.read_calls }; }
function minus(a: Budget, b: Budget): Budget { return { model_calls: a.model_calls - b.model_calls, read_calls: a.read_calls - b.read_calls }; }
function within(a: Budget, b: Budget): boolean { return a.model_calls <= b.model_calls && a.read_calls <= b.read_calls; }
const ZERO = { model_calls: 0, read_calls: 0 };
function parseBasis(value: unknown, extras: string[]): WorkflowBasis & Record<string, unknown> {
  if (!object(value) || Object.keys(value).some(k => ![...BASE_KEYS, ...extras].includes(k)) || BASE_KEYS.some(k => !(k in value))) fail("INVALID_REQUEST", 400);
  if (!str(value.idempotency_key, 120) || !ID.test(value.idempotency_key)
    || ![value.expected_task_revision, value.expected_assignment_revision, value.expected_case_scope_revision].every(v => Number.isSafeInteger(v) && Number(v) >= 1)
    || !str(value.reviewed_bundle_id, 160) || !str(value.reviewed_bundle_digest, 64) || !HASH.test(value.reviewed_bundle_digest)
    || !str(value.candidate_sha, 40) || !/^[a-f0-9]{40}$/.test(value.candidate_sha)) fail("INVALID_REQUEST", 400);
  return value as unknown as WorkflowBasis & Record<string, unknown>;
}
function materialMeta(material: EvidenceMaterial): Record<string, unknown> {
  return { material_ref: material.material_ref, kind: material.kind, title: material.title, sha256: material.sha256, provenance: material.provenance };
}

/**
 * v2 是独立增量模型，不改写 egap_review 的历史 ACK/命令语义。
 * HTTP → 人员命令 → 本库事务；Worker → outbox/mapping → Runtime → inbox → 本库事务。
 * 复用 ReviewStore.pool；本类不持有连接生命周期，也不会在事务中等待模型或 MCP。
 */
export class WorkflowStore {
  constructor(readonly pool: Pool) {}
  private async tx<T>(fn: (c: PoolClient) => Promise<T>, readonly = false): Promise<T> {
    const c = await this.pool.connect();
    try { await c.query(readonly ? "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" : "BEGIN"); const v = await fn(c); await c.query("COMMIT"); return v; }
    catch (e) { await c.query("ROLLBACK"); throw e; } finally { c.release(); }
  }
  async migrate(): Promise<void> {
    await this.tx(async c => {
      await c.query("SELECT pg_advisory_xact_lock(184321,7)");
      await c.query(`CREATE SCHEMA IF NOT EXISTS egap_review_v2;
      CREATE TABLE IF NOT EXISTS egap_review_v2.cases(id text PRIMARY KEY,tenant_id text NOT NULL,data jsonb NOT NULL,scope_revision int NOT NULL DEFAULT 1,event_seq int NOT NULL DEFAULT 0,current_bundle_id text NOT NULL,owner_id text NOT NULL,actors jsonb NOT NULL,archive jsonb NOT NULL,budget_cap jsonb NOT NULL,budget_reserved jsonb NOT NULL,budget_consumed jsonb NOT NULL);
      CREATE TABLE IF NOT EXISTS egap_review_v2.bundles(id text PRIMARY KEY,case_id text NOT NULL REFERENCES egap_review_v2.cases(id),parent_id text,digest text NOT NULL,manifest jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(case_id,digest));
      CREATE TABLE IF NOT EXISTS egap_review_v2.tasks(id text PRIMARY KEY,case_id text NOT NULL REFERENCES egap_review_v2.cases(id),obligation_id text NOT NULL,role text NOT NULL,title text NOT NULL,assignee_id text,task_revision int NOT NULL DEFAULT 1,assignment_revision int NOT NULL DEFAULT 1,status text NOT NULL DEFAULT 'ASSIGNED',claimed boolean NOT NULL DEFAULT false,bound_bundle_id text NOT NULL,bound_bundle_digest text NOT NULL,UNIQUE(case_id,obligation_id));
      CREATE TABLE IF NOT EXISTS egap_review_v2.requests(id text PRIMARY KEY,case_id text NOT NULL REFERENCES egap_review_v2.cases(id),task_id text NOT NULL REFERENCES egap_review_v2.tasks(id),request_spec_revision int NOT NULL,spec_digest text NOT NULL,input jsonb NOT NULL,input_digest text NOT NULL,execution_state text NOT NULL DEFAULT 'QUEUED',review_state text NOT NULL DEFAULT 'PENDING_REVIEW',fulfillment_state text NOT NULL DEFAULT 'UNASSESSED',current_response_id text,withdrawn boolean NOT NULL DEFAULT false,clarification text,created_at timestamptz NOT NULL DEFAULT now());
      CREATE TABLE IF NOT EXISTS egap_review_v2.specs(request_id text NOT NULL REFERENCES egap_review_v2.requests(id),revision int NOT NULL,input jsonb NOT NULL,input_digest text NOT NULL,spec_digest text NOT NULL,predecessor_run_id text,answer text,PRIMARY KEY(request_id,revision));
      ALTER TABLE egap_review_v2.requests ADD COLUMN IF NOT EXISTS execution_context jsonb NOT NULL DEFAULT '{}'::jsonb;
      ALTER TABLE egap_review_v2.specs ADD COLUMN IF NOT EXISTS execution_context jsonb NOT NULL DEFAULT '{}'::jsonb;
      CREATE TABLE IF NOT EXISTS egap_review_v2.responses(id text PRIMARY KEY,request_id text NOT NULL REFERENCES egap_review_v2.requests(id),spec_revision int NOT NULL,digest text NOT NULL,body jsonb NOT NULL,bundle_id text NOT NULL,bundle_digest text NOT NULL,review_state text NOT NULL DEFAULT 'PENDING_REVIEW',archived boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now(),read_receipts jsonb NOT NULL DEFAULT '[]'::jsonb);
      ALTER TABLE egap_review_v2.responses ADD COLUMN IF NOT EXISTS read_receipts jsonb NOT NULL DEFAULT '[]'::jsonb;
      CREATE TABLE IF NOT EXISTS egap_review_v2.commands(id text PRIMARY KEY,case_id text NOT NULL REFERENCES egap_review_v2.cases(id),idempotency_key text NOT NULL,payload_digest text NOT NULL,response jsonb NOT NULL,UNIQUE(case_id,idempotency_key));
      CREATE TABLE IF NOT EXISTS egap_review_v2.events(case_id text NOT NULL REFERENCES egap_review_v2.cases(id),seq int NOT NULL,kind text NOT NULL,payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(case_id,seq));
      CREATE TABLE IF NOT EXISTS egap_review_v2.outbox(id text PRIMARY KEY,request_id text NOT NULL REFERENCES egap_review_v2.requests(id),spec_revision int NOT NULL,state text NOT NULL DEFAULT 'PENDING',worker_id text,lease_until timestamptz,attempts int NOT NULL DEFAULT 0,last_error text,UNIQUE(request_id,spec_revision));
      CREATE TABLE IF NOT EXISTS egap_review_v2.run_mappings(request_id text NOT NULL REFERENCES egap_review_v2.requests(id),spec_revision int NOT NULL,data jsonb NOT NULL,PRIMARY KEY(request_id,spec_revision));
      CREATE TABLE IF NOT EXISTS egap_review_v2.inbox(effect_id text PRIMARY KEY,payload_digest text NOT NULL,result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());`);
    });
  }
  /** 本地可信导入器调用；目录是冻结的授权范围，不能由 HTTP 提交任意路径。 */
  async seedCase(seed: WorkflowSeed): Promise<void> {
    const { legacy, archive, actors } = seed;
    if (!actors.some(a => a.id === seed.owner_id && a.role === "BUSINESS") || !actors.some(a => a.id === seed.qa_assignee_id && a.role === "QA") || actors.some(a => a.tenant_id !== legacy.tenant_id)) fail("INVALID_SEED", 400);
    const manifest = legacy.evidence.map(e => ({ material_ref: e.id, kind: "HISTORICAL_EXCERPT", title: e.title, sha256: e.sha256, source_ref: e.source_ref, source_sha256: e.source_sha256 }));
    const bundleDigest = digest(canonical(manifest)); const bundleId = `${legacy.id}:b1`;
    await this.tx(async c => {
      const result = await c.query("INSERT INTO egap_review_v2.cases(id,tenant_id,data,current_bundle_id,owner_id,actors,archive,budget_cap,budget_reserved,budget_consumed) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$9) ON CONFLICT DO NOTHING RETURNING id", [legacy.id, legacy.tenant_id, { title: legacy.title, candidate_sha: legacy.candidate_sha, historical_run_id: archive.historical_run_id, review_policy_version: archive.review_policy_version ?? REVIEW_POLICY }, bundleId, seed.owner_id, JSON.stringify(actors), archive, { model_calls: 24, read_calls: 48 }, ZERO]);
      if (!result.rowCount) {
        const existing = (await c.query<CaseRow>("SELECT * FROM egap_review_v2.cases WHERE id=$1", [legacy.id])).rows[0]!;
        if (existing.data.candidate_sha !== legacy.candidate_sha || canonical(existing.archive) !== canonical(archive)) fail("IMMUTABLE_WORKFLOW_IMPORT_CONFLICT");
        // 登录身份配置可新增合格人员；不能静默更换已有任务接手人。
        await c.query("UPDATE egap_review_v2.cases SET actors=$2 WHERE id=$1", [legacy.id, JSON.stringify(actors)]); return;
      }
      await c.query("INSERT INTO egap_review_v2.bundles(id,case_id,digest,manifest) VALUES($1,$2,$3,$4)", [bundleId, legacy.id, bundleDigest, JSON.stringify(manifest)]);
      for (const [key, title] of [["EVIDENCE_LINKAGE", "证据关联核对"], ["SEMANTIC_REVIEW", "业务语义审查"]]) {
        await c.query("INSERT INTO egap_review_v2.tasks(id,case_id,obligation_id,role,title,assignee_id,bound_bundle_id,bound_bundle_digest) VALUES($1,$2,$3,'QA',$4,$5,$6,$7)", [`${legacy.id}:${key!.toLowerCase().replaceAll("_", "-")}`, legacy.id, key, title, seed.qa_assignee_id, bundleId, bundleDigest]);
      }
      await this.event(c, legacy.id, "WORKFLOW_IMPORTED", { legacy_case_id: legacy.id, owner_id: seed.owner_id });
    });
  }
  private async event(c: PoolClient, caseId: string, kind: string, payload: unknown): Promise<void> {
    const row = await c.query<{ event_seq: number }>("UPDATE egap_review_v2.cases SET event_seq=event_seq+1 WHERE id=$1 RETURNING event_seq", [caseId]);
    await c.query("INSERT INTO egap_review_v2.events(case_id,seq,kind,payload) VALUES($1,$2,$3,$4)", [caseId, row.rows[0]!.event_seq, kind, payload]);
  }
  private async case(c: PoolClient, id: string, actor?: Actor, lock = false): Promise<CaseRow> {
    const row = (await c.query<CaseRow>(`SELECT * FROM egap_review_v2.cases WHERE id=$1 ${lock ? "FOR UPDATE" : ""}`, [id])).rows[0];
    if (!row || (actor && (row.tenant_id !== actor.tenant_id || !row.actors.some(a => a.id === actor.id && a.role === actor.role)))) fail("NOT_FOUND", 404);
    return row;
  }
  private taskView(task: TaskRow, row: CaseRow, actor: Actor): Record<string, unknown> {
    return { ...task, case_scope_revision: row.scope_revision, candidate_sha: row.data.candidate_sha, can_act: task.assignee_id === actor.id,
      can_claim: task.assignee_id === actor.id && !task.claimed, can_reassign: task.assignee_id === actor.id || row.owner_id === actor.id,
      eligible_assignees: row.actors.filter(a => a.role === task.role) };
  }
  async listTasks(actor: Actor): Promise<{ tasks: Record<string, unknown>[] }> {
    return this.tx(async c => {
      // 同租户可包含互不授权的独立 case；先在 SQL 限定成员，不能因其中一个无权 case 使整个任务列表 404。
      const rows = (await c.query<TaskRow>(`SELECT t.* FROM egap_review_v2.tasks t JOIN egap_review_v2.cases c ON t.case_id=c.id
        WHERE c.tenant_id=$1 AND EXISTS(SELECT 1 FROM jsonb_array_elements(c.actors) a WHERE a->>'id'=$2 AND a->>'role'=$3)
        ORDER BY t.case_id,t.id`, [actor.tenant_id, actor.id, actor.role])).rows;
      const result = [];
      for (const task of rows) { const parent = await this.case(c, task.case_id, actor); if (task.role === actor.role || parent.owner_id === actor.id) result.push(this.taskView(task, parent, actor)); }
      return { tasks: result };
    }, true);
  }
  async getTask(id: string, actor: Actor): Promise<Record<string, unknown>> {
    return this.tx(async c => {
      const task = (await c.query<TaskRow>("SELECT * FROM egap_review_v2.tasks WHERE id=$1", [id])).rows[0]; if (!task) fail("NOT_FOUND", 404);
      const row = await this.case(c, task.case_id, actor);
      const bundle = (await c.query<BundleRow>("SELECT * FROM egap_review_v2.bundles WHERE id=$1", [task.bound_bundle_id])).rows[0]!;
      if (digest(canonical(bundle.manifest)) !== bundle.digest) fail("BUNDLE_INTEGRITY_ERROR");
      const requests = (await c.query<RequestRow>("SELECT * FROM egap_review_v2.requests WHERE task_id=$1 ORDER BY created_at,id", [id])).rows;
      const enriched = [];
      for (const request of requests) {
        const responses = (await c.query("SELECT id,digest,body,bundle_id,bundle_digest,review_state,archived,created_at,read_receipts FROM egap_review_v2.responses WHERE request_id=$1 ORDER BY created_at,id", [request.id])).rows;
        // 目录只向浏览器展示元数据；内容必须经授权材料接口读取。
        enriched.push({ ...request, input: { ...request.input, materials: request.input.materials.map(materialMeta) }, current_response: responses.find(r => r.id === request.current_response_id) ?? null, responses });
      }
      return { task: this.taskView(task, row, actor), case: { id: row.id, ...row.data, case_scope_revision: row.scope_revision, owner_id: row.owner_id, budget_cap: row.budget_cap, budget_reserved: row.budget_reserved, budget_consumed: row.budget_consumed }, bundle,
        requests: enriched, allowed_targets: row.archive.targets, item_templates: ITEM_TEMPLATES, eligible_assignees: row.actors.filter(a => a.role === task.role), actor, required_obligation_coverage: row.archive.targets.flatMap(target => ITEM_TEMPLATES.slice(0, 3).map(template => ({ kind: template.kind, target }))),
        events: (await c.query("SELECT seq,kind,payload,created_at FROM egap_review_v2.events WHERE case_id=$1 ORDER BY seq", [row.id])).rows };
    }, true);
  }
  async getMaterial(caseId: string, materialRef: string, actor: Actor): Promise<EvidenceMaterial> {
    return this.tx(async c => { const row = await this.case(c, caseId, actor); const material = row.archive.materials.find(m => m.material_ref === materialRef); if (!material) fail("NOT_FOUND", 404); if (digest(material.content) !== material.sha256) fail("EVIDENCE_INTEGRITY_ERROR"); return material; }, true);
  }
  /** 任务行的乐观版本只约束人员命令；共享 Case 行锁只用于极短的事件与 manifest 合并。 */
  private async human(taskId: string, actor: Actor, kind: string, body: WorkflowBasis & Record<string, unknown>, fn: (c: PoolClient, task: TaskRow, row: CaseRow) => Promise<Record<string, unknown>>, ownerAllowed = false): Promise<Record<string, unknown>> {
    return this.tx(async c => {
      const lookup = (await c.query<TaskRow>("SELECT * FROM egap_review_v2.tasks WHERE id=$1", [taskId])).rows[0]; if (!lookup) fail("NOT_FOUND", 404);
      const row = await this.case(c, lookup.case_id, actor, true);
      const task = (await c.query<TaskRow>("SELECT * FROM egap_review_v2.tasks WHERE id=$1 FOR UPDATE", [taskId])).rows[0]!;
      const payloadDigest = digest(canonical({ task_id: taskId, kind, actor_id: actor.id, body }));
      const prior = (await c.query<{ payload_digest: string; response: Record<string, unknown> }>("SELECT payload_digest,response FROM egap_review_v2.commands WHERE case_id=$1 AND idempotency_key=$2", [row.id, body.idempotency_key])).rows[0];
      if (prior) { if (prior.payload_digest !== payloadDigest) fail("IDEMPOTENCY_CONFLICT"); return prior.response; }
      if (task.assignee_id !== actor.id && !(ownerAllowed && row.owner_id === actor.id)) fail("NOT_TASK_ASSIGNEE", 403);
      if (task.task_revision !== body.expected_task_revision) fail("STALE_TASK_REVISION");
      if (task.assignment_revision !== body.expected_assignment_revision) fail("STALE_ASSIGNMENT_REVISION");
      if (row.scope_revision !== body.expected_case_scope_revision || row.data.candidate_sha !== body.candidate_sha) fail("STALE_CASE_SCOPE");
      if (task.bound_bundle_id !== body.reviewed_bundle_id || task.bound_bundle_digest !== body.reviewed_bundle_digest) fail("STALE_REVIEWED_BUNDLE");
      const bound = (await c.query<BundleRow>("SELECT * FROM egap_review_v2.bundles WHERE id=$1", [task.bound_bundle_id])).rows[0];
      if (!bound || bound.digest !== task.bound_bundle_digest || digest(canonical(bound.manifest)) !== bound.digest) fail("BUNDLE_INTEGRITY_ERROR");
      const payload = await fn(c, task, row);
      const updated = (await c.query<TaskRow>("UPDATE egap_review_v2.tasks SET task_revision=task_revision+1 WHERE id=$1 RETURNING *", [taskId])).rows[0]!;
      const response = { id: randomUUID(), kind, case_id: row.id, task_id: taskId, task_revision: updated.task_revision, assignment_revision: updated.assignment_revision, ...payload };
      await c.query("INSERT INTO egap_review_v2.commands(id,case_id,idempotency_key,payload_digest,response) VALUES($1,$2,$3,$4,$5)", [response.id, row.id, body.idempotency_key, payloadDigest, response]);
      await this.event(c, row.id, kind, { ...response, actor_id: actor.id }); return response;
    });
  }
  async claim(taskId: string, actor: Actor, value: unknown): Promise<Record<string, unknown>> {
    return this.human(taskId, actor, "TASK_CLAIMED", parseBasis(value, []), async (c, task) => {
      if (task.claimed) fail("TASK_ALREADY_CLAIMED"); await c.query("UPDATE egap_review_v2.tasks SET claimed=true,status='IN_PROGRESS' WHERE id=$1", [taskId]); return { status: "IN_PROGRESS" };
    });
  }
  async reassign(taskId: string, actor: Actor, value: unknown): Promise<Record<string, unknown>> {
    const body = parseBasis(value, ["assignee_id"]);
    return this.human(taskId, actor, "TASK_REASSIGNED", body, async (c, task, row) => {
      if (!row.actors.some(a => a.id === body.assignee_id && a.role === task.role)) fail("INELIGIBLE_ASSIGNEE", 400);
      if (task.assignee_id === body.assignee_id) fail("ALREADY_ASSIGNED");
      await c.query("UPDATE egap_review_v2.tasks SET assignee_id=$2,assignment_revision=assignment_revision+1,claimed=false WHERE id=$1", [taskId, body.assignee_id]); return { assignee_id: body.assignee_id, status: task.status };
    }, true);
  }
  private makeInput(row: CaseRow, task: TaskRow, requestId: string, rev: number, goal: string, items: EvidenceItem[], continuation: EvidenceReviewInput["continuation"] = null): EvidenceReviewInput {
    if (items.length < 1 || items.length > 12 || items.some(x => !object(x) || !str(x.item_id, 160) || !object(x.target)) || new Set(items.map(x => x.item_id)).size !== items.length) fail("INVALID_REQUEST_ITEMS", 400);
    for (const item of items) {
      const template = ITEM_TEMPLATES.find(t => t.kind === item.kind);
      if (!template || !row.archive.targets.some(t => canonical(t) === canonical(item.target))) fail("UNAUTHORIZED_EVIDENCE_TARGET", 400);
      // 该 profile 只验证冻结模板中的来源/关联条件；自由调查描述放在 goal，不能变成未经实现的公司质量门禁。
      // 否则调用方可把“所有退款已获批准”塞进 criteria，借原件齐全得到误导性的 COMPLETE。
      if (canonical(item.acceptance_criteria ?? null) !== canonical(template.acceptance_criteria)) fail("UNSUPPORTED_ACCEPTANCE_CRITERIA", 400);
    }
    const spec = { goal, continuation, request_items: items, source_bundle_id: task.bound_bundle_id, source_bundle_sha256: task.bound_bundle_digest };
    try { return parseEvidenceReviewInput({ schema_version: 2, task_mode: "EVIDENCE_REVIEW", scope: "LOCAL_ARCHIVED_EVIDENCE", tenant_id: row.tenant_id, candidate_sha: row.data.candidate_sha, historical_run_id: row.archive.historical_run_id,
      request_id: requestId, request_spec_revision: rev, request_spec_sha256: evidenceReviewDigest(spec), case_id: row.id, case_scope_revision: row.scope_revision,
      task_id: task.id, obligation_id: task.obligation_id, ...spec,
      reviewed_policy: { policy_id: "local-evidence-linkage", policy_version: String(row.data.review_policy_version), sha256: evidenceReviewDigest(ITEM_TEMPLATES) },
      authorized_manifest: { manifest_id: `${row.id}:authorized-archive`, manifest_version: "1", sha256: evidenceReviewDigest(row.archive.materials.map(materialMeta)) }, materials: row.archive.materials }); }
    catch { fail("INVALID_EVIDENCE_REVIEW_INPUT", 400); }
  }
  private async queue(c: PoolClient, input: EvidenceReviewInput, predecessor: string | null = null, answer: string | null = null): Promise<void> {
    await c.query("INSERT INTO egap_review_v2.specs(request_id,revision,input,input_digest,spec_digest,predecessor_run_id,answer) VALUES($1,$2,$3,$4,$5,$6,$7)", [input.request_id, input.request_spec_revision, input, evidenceReviewDigest(input), input.request_spec_sha256, predecessor, answer]);
    await c.query("INSERT INTO egap_review_v2.outbox(id,request_id,spec_revision) VALUES($1,$2,$3)", [randomUUID(), input.request_id, input.request_spec_revision]);
  }
  async requestEvidence(taskId: string, actor: Actor, value: unknown): Promise<Record<string, unknown>> {
    const body = parseBasis(value, ["goal", "items"]);
    if (!str(body.goal) || !Array.isArray(body.items)) fail("INVALID_REQUEST", 400);
    return this.human(taskId, actor, "EVIDENCE_REQUESTED", body, async (c, task, row) => {
      if (!task.claimed) fail("TASK_NOT_CLAIMED");
      const requestId = randomUUID(); const input = this.makeInput(row, task, requestId, 1, body.goal as string, body.items as EvidenceItem[]);
      await c.query("INSERT INTO egap_review_v2.requests(id,case_id,task_id,request_spec_revision,spec_digest,input,input_digest) VALUES($1,$2,$3,1,$4,$5,$6)", [requestId, row.id, taskId, input.request_spec_sha256, input, evidenceReviewDigest(input)]);
      await this.queue(c, input); await c.query("UPDATE egap_review_v2.tasks SET status='WAITING_EVIDENCE' WHERE id=$1", [taskId]);
      return { request_id: requestId, request_spec_revision: 1, status: "QUEUED" };
    });
  }
  private async requestTask(id: string): Promise<string> { const r = (await this.pool.query<{ task_id: string }>("SELECT task_id FROM egap_review_v2.requests WHERE id=$1", [id])).rows[0]; if (!r) fail("NOT_FOUND", 404); return r.task_id; }
  async withdraw(id: string, actor: Actor, value: unknown): Promise<Record<string, unknown>> {
    const body = parseBasis(value, ["reason"]); if (!str(body.reason)) fail("INVALID_REQUEST", 400);
    return this.human(await this.requestTask(id), actor, "EVIDENCE_REQUEST_WITHDRAWN", body, async (c) => {
      await c.query("UPDATE egap_review_v2.requests SET withdrawn=true,execution_state='CANCELLED' WHERE id=$1", [id]);
      return { request_id: id, status: "CANCELLED", reason: body.reason };
    });
  }
  /** CLARIFY 不是重置 Run；保留旧规格和成本，后继 Run 使用新冻结输入。 */
  async clarify(id: string, actor: Actor, value: unknown): Promise<Record<string, unknown>> {
    const body = parseBasis(value, ["answer"]); if (!str(body.answer, 2000)) fail("INVALID_REQUEST", 400);
    return this.human(await this.requestTask(id), actor, "EVIDENCE_CLARIFIED", body, async (c, task, row) => {
      const request = (await c.query<RequestRow>("SELECT * FROM egap_review_v2.requests WHERE id=$1 FOR UPDATE", [id])).rows[0]!;
      if (request.withdrawn || request.execution_state !== "BLOCKED") fail("REQUEST_NOT_CLARIFIABLE");
      const mapping = (await c.query<{ data: RunMapping }>("SELECT data FROM egap_review_v2.run_mappings WHERE request_id=$1 AND spec_revision=$2", [id, request.request_spec_revision])).rows[0];
      const continuation = mapping ? {
        predecessor_run_id: mapping.data.run_id, predecessor_request_spec_revision: request.request_spec_revision,
        read_receipt_refs: (request.execution_context.read_receipts ?? []).map(receipt => String(receipt.receipt_ref)),
        remaining_gaps: request.execution_context.remaining_gaps ?? (request.clarification ? [request.clarification] : []),
        human_answer: body.answer as string,
      } : null;
      // 旧 Run 的 receipt 身份带入输入用于导航，但绝不复制成后继 Runtime 的 read_receipts。
      const input = this.makeInput(row, task, id, request.request_spec_revision + 1, `${request.input.goal}\n人员澄清：${body.answer}`, request.input.request_items, continuation);
      await this.queue(c, input, mapping?.data.run_id ?? null, body.answer as string);
      await c.query("UPDATE egap_review_v2.requests SET request_spec_revision=$2,spec_digest=$3,input=$4,input_digest=$5,execution_state='QUEUED',review_state='PENDING_REVIEW',fulfillment_state='UNASSESSED',current_response_id=NULL,clarification=NULL,execution_context='{}'::jsonb WHERE id=$1", [id, input.request_spec_revision, input.request_spec_sha256, input, evidenceReviewDigest(input)]);
      return { request_id: id, request_spec_revision: input.request_spec_revision, status: "QUEUED" };
    });
  }
  async acceptResponse(id: string, actor: Actor, value: unknown): Promise<Record<string, unknown>> {
    const body = parseBasis(value, ["request_spec_revision", "response_id", "response_digest", "review_policy_version"]);
    if (!Number.isSafeInteger(body.request_spec_revision) || !str(body.response_id, 160) || !str(body.response_digest, 64)) fail("INVALID_REQUEST", 400);
    return this.human(await this.requestTask(id), actor, "EVIDENCE_RESPONSE_ACCEPTED", body, async (c, task, row) => {
      const request = (await c.query<RequestRow>("SELECT * FROM egap_review_v2.requests WHERE id=$1 FOR UPDATE", [id])).rows[0]!;
      if (!task.claimed) fail("TASK_NOT_CLAIMED");
      if (request.withdrawn || request.request_spec_revision !== body.request_spec_revision || request.input.case_scope_revision !== row.scope_revision) fail("STALE_REQUEST_SPEC");
      if (request.current_response_id !== body.response_id) fail("STALE_RESPONSE_POINTER");
      const response = (await c.query<{ digest: string; body: Record<string, unknown>; bundle_id: string; bundle_digest: string; archived: boolean }>("SELECT * FROM egap_review_v2.responses WHERE id=$1 AND request_id=$2", [body.response_id, id])).rows[0];
      if (!response || response.archived || response.digest !== body.response_digest || this.responseDigest(response.body) !== response.digest) fail("RESPONSE_INTEGRITY_ERROR");
      if (body.review_policy_version !== row.data.review_policy_version) fail("STALE_REVIEW_POLICY");
      // reviewed bundle 是当前任务实际打开的包；可包含本响应之后合法合入的其他材料。
      await c.query("UPDATE egap_review_v2.responses SET review_state='ACCEPTED' WHERE id=$1", [body.response_id]);
      await c.query("UPDATE egap_review_v2.requests SET review_state='ACCEPTED' WHERE id=$1", [id]);
      const unfulfilled = await c.query("SELECT 1 FROM egap_review_v2.requests WHERE task_id=$1 AND withdrawn=false AND (review_state<>'ACCEPTED' OR fulfillment_state<>'COMPLETE') LIMIT 1", [task.id]);
      const acceptedRows = (await c.query<{ input: EvidenceReviewInput; body: Record<string, unknown> }>(`SELECT r.input,p.body FROM egap_review_v2.requests r JOIN egap_review_v2.responses p ON p.id=r.current_response_id
        WHERE r.task_id=$1 AND r.withdrawn=false AND r.review_state='ACCEPTED' AND p.archived=false`, [task.id])).rows;
      const coverage = new Set<string>();
      for (const accepted of acceptedRows) for (const item of accepted.body.items as { item_id: string; validated_disposition: string }[]) {
        const requested = accepted.input.request_items.find(i => i.item_id === item.item_id);
        if (requested && item.validated_disposition === "PROVIDED") coverage.add(canonical({ kind: requested.kind, target: requested.target }));
      }
      // 公司业务语义尚未定义；这里只闭合本案冻结的 3 种原件 × 全部目标关联义务。
      const fullCoverage = row.archive.targets.every(target => ITEM_TEMPLATES.slice(0, 3).every(template => coverage.has(canonical({ kind: template.kind, target }))));
      const status = task.obligation_id === "EVIDENCE_LINKAGE" && !unfulfilled.rowCount && fullCoverage ? "COMPLETED" : task.obligation_id === "SEMANTIC_REVIEW" ? "PENDING_REVIEW" : "WAITING_EVIDENCE";
      await c.query("UPDATE egap_review_v2.tasks SET status=$2 WHERE id=$1", [task.id, status]);
      return { request_id: id, response_id: body.response_id, review_state: "ACCEPTED", fulfillment_state: request.fulfillment_state, obligation_coverage: { provided: coverage.size, required: row.archive.targets.length * 3 }, status };
    });
  }
  async loadRequest(id: string, specRevision?: number): Promise<RequestSnapshot> {
    return this.tx(async c => {
      const row = (await c.query<RequestRow>("SELECT * FROM egap_review_v2.requests WHERE id=$1", [id])).rows[0]; if (!row) fail("NOT_FOUND", 404);
      const revision = specRevision ?? row.request_spec_revision;
      const spec = (await c.query<{ input: EvidenceReviewInput; input_digest: string; spec_digest: string; execution_context: Partial<ExecutionContext> }>("SELECT input,input_digest,spec_digest,execution_context FROM egap_review_v2.specs WHERE request_id=$1 AND revision=$2", [id, revision])).rows[0]; if (!spec) fail("NOT_FOUND", 404);
      if (evidenceReviewDigest(spec.input) !== spec.input_digest) fail("FROZEN_INPUT_INTEGRITY_ERROR");
      const mapping = (await c.query<{ data: RunMapping }>("SELECT data FROM egap_review_v2.run_mappings WHERE request_id=$1 AND spec_revision=$2", [id, revision])).rows[0]?.data ?? null;
      return { ...row, ...spec, request_spec_revision: revision, mapping };
    }, true);
  }
  /** SKIP LOCKED 让多个 Worker 接手不同任务；过期 lease 重放原 mapping，不生成新 Run。 */
  async claimWork(workerId: string, leaseMs = 30_000): Promise<WorkItem | null> {
    if (!ID.test(workerId) || !Number.isSafeInteger(leaseMs) || leaseMs < 1 || leaseMs > 300_000) fail("INVALID_WORKER", 400);
    return this.tx(async c => {
      const result = await c.query<{ id: string; request_id: string; spec_revision: number; attempts: number }>(`SELECT o.* FROM egap_review_v2.outbox o JOIN egap_review_v2.requests r ON r.id=o.request_id
        WHERE (o.state='PENDING' OR (o.state='LEASED' AND o.lease_until<now())) AND r.withdrawn=false AND o.spec_revision=r.request_spec_revision
        ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT 1`);
      const row = result.rows[0]; if (!row) return null;
      await c.query("UPDATE egap_review_v2.outbox SET state='LEASED',worker_id=$2,lease_until=now()+($3::int*interval '1 millisecond'),attempts=attempts+1 WHERE id=$1", [row.id, workerId, leaseMs]);
      return { id: row.id, request_id: row.request_id, request_spec_revision: row.spec_revision, worker_id: workerId, attempts: row.attempts + 1 };
    });
  }
  /** UNKNOWN 只进入有界只读对账，不重新派发模型或 MCP，也不重新预留预算。 */
  async claimReconciliationWork(workerId: string, leaseMs = 30_000): Promise<WorkItem | null> {
    if (!ID.test(workerId) || !Number.isSafeInteger(leaseMs) || leaseMs < 1 || leaseMs > 300_000) fail("INVALID_WORKER", 400);
    return this.tx(async c => {
      const row = (await c.query<{ id: string; request_id: string; spec_revision: number; attempts: number }>(`SELECT * FROM egap_review_v2.outbox
        WHERE state IN ('UNKNOWN','RECONCILING') AND (lease_until IS NULL OR lease_until<now()) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1`)).rows[0];
      if (!row) return null;
      await c.query("UPDATE egap_review_v2.outbox SET state='RECONCILING',worker_id=$2,lease_until=now()+($3::int*interval '1 millisecond'),attempts=attempts+1 WHERE id=$1", [row.id, workerId, leaseMs]);
      return { id: row.id, request_id: row.request_id, request_spec_revision: row.spec_revision, worker_id: workerId, attempts: row.attempts + 1 };
    });
  }
  /** 心跳只续租同一次 claim；旧 Worker 即使复用名称也不能延长后继 claim。 */
  async renewWork(work: WorkItem, leaseMs = 30_000): Promise<boolean> {
    const r = await this.pool.query("UPDATE egap_review_v2.outbox SET lease_until=now()+($5::int*interval '1 millisecond') WHERE id=$1 AND worker_id=$2 AND spec_revision=$3 AND attempts=$4 AND state IN ('LEASED','RECONCILING') AND lease_until>now()", [work.id, work.worker_id, work.request_spec_revision, work.attempts, leaseMs]);
    return r.rowCount === 1;
  }
  /** 在每个外部动作之前检查撤回/范围与 lease；转派不改变执行授权。 */
  async isDispatchable(work: WorkItem): Promise<boolean> {
    const r = await this.pool.query(`SELECT 1 FROM egap_review_v2.outbox o JOIN egap_review_v2.requests r ON r.id=o.request_id JOIN egap_review_v2.cases c ON c.id=r.case_id
      WHERE o.id=$1 AND o.worker_id=$2 AND o.attempts=$3 AND o.state='LEASED' AND o.lease_until>now()
      AND o.spec_revision=r.request_spec_revision AND r.withdrawn=false AND (r.input->>'case_scope_revision')::int=c.scope_revision AND r.input->>'candidate_sha'=c.data->>'candidate_sha'`, [work.id, work.worker_id, work.attempts]);
    return r.rowCount === 1;
  }
  /** 在第一次调用 Runtime 之前冻结凭据引用/输入/配置与预算。UNKNOWN 不释放配额。 */
  async reserveRunMapping(work: WorkItem, mapping: RunMapping): Promise<RunMapping> {
    budget(mapping.allocation);
    if (mapping.limits.model_calls !== mapping.allocation.model_calls || mapping.limits.reads !== mapping.allocation.read_calls
      || !HASH.test(mapping.config_digest) || !HASH.test(mapping.frozen_input_digest)) fail("RUN_BUDGET_LIMITS_MISMATCH", 400);
    return this.tx(async c => {
      const lookup = (await c.query<RequestRow>("SELECT * FROM egap_review_v2.requests WHERE id=$1", [work.request_id])).rows[0]; if (!lookup) fail("NOT_FOUND", 404);
      const row = await this.case(c, lookup.case_id, undefined, true);
      const outbox = (await c.query<{ state: string; worker_id: string; attempts: number; live: boolean }>("SELECT state,worker_id,attempts,lease_until>now() AS live FROM egap_review_v2.outbox WHERE id=$1 FOR UPDATE", [work.id])).rows[0];
      if (!outbox || outbox.state !== "LEASED" || outbox.worker_id !== work.worker_id || outbox.attempts !== work.attempts || !outbox.live) fail("WORK_LEASE_LOST");
      if (lookup.withdrawn || lookup.request_spec_revision !== work.request_spec_revision || lookup.input.case_scope_revision !== row.scope_revision) fail("REQUEST_INACTIVE");
      if (mapping.request_id !== work.request_id || mapping.request_spec_revision !== work.request_spec_revision || mapping.frozen_input_digest !== lookup.input_digest || !str(mapping.credential_ref) || !str(mapping.run_id)) fail("RUN_MAPPING_BINDING_ERROR");
      const prior = (await c.query<{ data: RunMapping }>("SELECT data FROM egap_review_v2.run_mappings WHERE request_id=$1 AND spec_revision=$2", [work.request_id, work.request_spec_revision])).rows[0];
      if (prior) {
        const immutable = ({ consumed: _c, settlement: _s, last_runtime_event_seq: _seq, last_runtime_result_digest: _digest, ...rest }: RunMapping): unknown => rest;
        if (canonical(immutable(prior.data)) !== canonical(immutable(mapping))) fail("RUN_MAPPING_CONFLICT"); return prior.data;
      }
      const nextReserved = plus(row.budget_reserved, mapping.allocation);
      if (!within(plus(row.budget_consumed, nextReserved), row.budget_cap)) fail("CUMULATIVE_BUDGET_EXHAUSTED");
      const frozen: RunMapping = { ...mapping, consumed: { ...ZERO }, settlement: "RESERVED" };
      await c.query("INSERT INTO egap_review_v2.run_mappings(request_id,spec_revision,data) VALUES($1,$2,$3)", [work.request_id, work.request_spec_revision, frozen]);
      await c.query("UPDATE egap_review_v2.cases SET budget_reserved=$2 WHERE id=$1", [row.id, nextReserved]);
      await c.query("UPDATE egap_review_v2.requests SET execution_state='RUNNING' WHERE id=$1", [work.request_id]);
      await this.event(c, row.id, "EVIDENCE_RUN_RESERVED", { request_id: work.request_id, request_spec_revision: work.request_spec_revision, run_id: frozen.run_id, allocation: frozen.allocation }); return frozen;
    });
  }
  private responseDigest(body: Record<string, unknown>): string { const { response_sha256: _hash, ...rest } = body; return evidenceReviewDigest(rest); }
  /**
   * Runtime 出站结果的 inbox 去重及 Bundle/Response/Task 更新仅在本库原子提交。
   * 先锁当前 case，再将 verified delta 加到当前 manifest；不以旧 source bundle 覆盖新材料。
   */
  async applyRuntimeResult(effectId: string, result: RuntimeResult): Promise<Record<string, unknown>> {
    budget(result.consumed);
    if (result.runtime_event_seq !== undefined && (!Number.isSafeInteger(result.runtime_event_seq) || result.runtime_event_seq < 0)) fail("INVALID_RUNTIME_EVENT_SEQ", 400);
    if (result.response && result.execution_state !== "FINISHED") fail("RESPONSE_REQUIRES_FINISHED_RUNTIME");
    if (!ID.test(effectId)) fail("INVALID_EFFECT", 400);
    const payloadDigest = evidenceReviewDigest(result);
    return this.tx(async c => {
      // advisory 锁使两个不同 case 的相同 effect_id 也能安全识别冲突。
      await c.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`review-inbox:${effectId}`]);
      const prior = (await c.query<{ payload_digest: string; result: Record<string, unknown> }>("SELECT payload_digest,result FROM egap_review_v2.inbox WHERE effect_id=$1", [effectId])).rows[0];
      if (prior) { if (prior.payload_digest !== payloadDigest) fail("INBOX_EFFECT_CONFLICT"); return prior.result; }
      const lookup = (await c.query<RequestRow>("SELECT * FROM egap_review_v2.requests WHERE id=$1", [result.request_id])).rows[0]; if (!lookup) fail("NOT_FOUND", 404);
      const row = await this.case(c, lookup.case_id, undefined, true);
      const request = (await c.query<RequestRow>("SELECT * FROM egap_review_v2.requests WHERE id=$1 FOR UPDATE", [result.request_id])).rows[0]!;
      const spec = (await c.query<{ input: EvidenceReviewInput; input_digest: string }>("SELECT input,input_digest FROM egap_review_v2.specs WHERE request_id=$1 AND revision=$2", [result.request_id, result.request_spec_revision])).rows[0];
      const mapRow = (await c.query<{ data: RunMapping }>("SELECT data FROM egap_review_v2.run_mappings WHERE request_id=$1 AND spec_revision=$2 FOR UPDATE", [result.request_id, result.request_spec_revision])).rows[0];
      if (!spec || !mapRow || mapRow.data.run_id !== result.run_id || spec.input_digest !== result.input_digest || mapRow.data.frozen_input_digest !== result.input_digest) fail("RUNTIME_RESULT_BINDING_ERROR");
      const mapping = mapRow.data;
      const sequence = result.runtime_event_seq;
      const previousSequence = mapping.last_runtime_event_seq;
      if (sequence !== undefined && sequence === previousSequence && mapping.last_runtime_result_digest !== payloadDigest) fail("RUNTIME_EVENT_CONFLICT");
      const staleSequence = sequence !== undefined && previousSequence !== undefined && sequence <= previousSequence;
      const terminalRegression = mapping.settlement === "SETTLED" && (result.execution_state === "UNKNOWN"
        || (request.request_spec_revision === result.request_spec_revision && request.execution_state === "FINISHED" && result.execution_state !== "FINISHED"));
      // 回流顺序与网络到达顺序可以不同；保留迟到原结果，不回退当前执行态、不重开 QA 接受、不重复结算。
      if (staleSequence || terminalRegression) {
        if (result.response) {
          const response = result.response;
          const identityKeys = ["request_id", "request_spec_revision", "request_spec_sha256", "case_id", "case_scope_revision", "candidate_sha", "historical_run_id", "tenant_id", "task_id", "obligation_id", "source_bundle_id", "source_bundle_sha256"] as const;
          if (response.response_sha256 !== this.responseDigest(response as unknown as Record<string, unknown>) || response.run_id !== result.run_id
            || identityKeys.some(key => response[key] !== spec.input[key]) || response.status !== "PROPOSED" || response.qa_acceptance !== "NOT_EVALUATED") fail("RESPONSE_BINDING_ERROR");
          const priorResponse = (await c.query<{ digest: string; request_id: string }>("SELECT digest,request_id FROM egap_review_v2.responses WHERE id=$1", [response.response_id])).rows[0];
          if (priorResponse && (priorResponse.digest !== response.response_sha256 || priorResponse.request_id !== request.id)) fail("IMMUTABLE_RESPONSE_CONFLICT");
          if (!priorResponse) await c.query("INSERT INTO egap_review_v2.responses(id,request_id,spec_revision,digest,body,bundle_id,bundle_digest,archived,read_receipts) VALUES($1,$2,$3,$4,$5,$6,$7,true,$8)",
            [response.response_id, request.id, result.request_spec_revision, response.response_sha256, response, spec.input.source_bundle_id, spec.input.source_bundle_sha256, JSON.stringify(result.read_receipts ?? [])]);
        }
        const stale = { effect_id: effectId, request_id: request.id, run_id: result.run_id, response_id: result.response?.response_id ?? null, archived: true, ignored_stale_state: true,
          incoming_execution_state: result.execution_state, incoming_runtime_event_seq: sequence ?? null, incoming_observed_cost: result.consumed, execution_state: request.execution_state };
        await c.query("INSERT INTO egap_review_v2.inbox(effect_id,payload_digest,result) VALUES($1,$2,$3)", [effectId, payloadDigest, stale]);
        await this.event(c, row.id, "STALE_RUNTIME_RESULT_ARCHIVED", stale); return stale;
      }
      if (sequence !== undefined) { mapping.last_runtime_event_seq = sequence; mapping.last_runtime_result_digest = payloadDigest; }
      if (!within(result.consumed, mapping.allocation)) fail("RUNTIME_BUDGET_OVER_ALLOCATION");
      if (mapping.settlement === "SETTLED" && canonical(mapping.consumed) !== canonical(result.consumed)) fail("RUNTIME_COST_CONFLICT");
      if (mapping.settlement !== "SETTLED") {
        if (result.execution_state === "UNKNOWN") mapping.settlement = "UNKNOWN";
        else {
          mapping.settlement = "SETTLED"; mapping.consumed = result.consumed;
          await c.query("UPDATE egap_review_v2.cases SET budget_reserved=$2,budget_consumed=$3 WHERE id=$1", [row.id, minus(row.budget_reserved, mapping.allocation), plus(row.budget_consumed, result.consumed)]);
        }
        await c.query("UPDATE egap_review_v2.run_mappings SET data=$3 WHERE request_id=$1 AND spec_revision=$2", [result.request_id, result.request_spec_revision, mapping]);
      }
      if (sequence !== undefined) await c.query("UPDATE egap_review_v2.run_mappings SET data=$3 WHERE request_id=$1 AND spec_revision=$2", [result.request_id, result.request_spec_revision, mapping]);
      const archived = request.withdrawn || request.request_spec_revision !== result.request_spec_revision || row.scope_revision !== spec.input.case_scope_revision || row.data.candidate_sha !== spec.input.candidate_sha;
      // 即便 CLARIFY/UNKNOWN 尚无响应，也保留已读取原件与缺口；按规格存档避免后继覆盖历史。
      const receiptMetadata = (result.read_receipts ?? []).map(receipt => {
        const material = spec.input.materials.find(m => m.material_ref === receipt.material_ref);
        if (!material || receipt.status !== "COMPLETED" || typeof receipt.receipt_ref !== "string" || receipt.source_sha256 !== material.sha256
          || !Number.isSafeInteger(receipt.start_line) || !Number.isSafeInteger(receipt.end_line) || Number(receipt.start_line) < 1 || Number(receipt.end_line) < Number(receipt.start_line)
          || typeof receipt.content !== "string" || digest(receipt.content) !== receipt.fragment_sha256
          || material.content.split("\n").slice(Number(receipt.start_line) - 1, Number(receipt.end_line)).join("\n") !== receipt.content) fail("EXECUTION_RECEIPT_INVALID");
        return { receipt_ref: receipt.receipt_ref, material_ref: receipt.material_ref, start_line: receipt.start_line, end_line: receipt.end_line,
          source_sha256: receipt.source_sha256, fragment_sha256: receipt.fragment_sha256 };
      });
      const executionContext: ExecutionContext = { run_id: result.run_id, request_spec_revision: result.request_spec_revision, execution_state: result.execution_state,
        read_receipts: receiptMetadata, remaining_gaps: [...new Set([...(result.clarification ? [result.clarification] : []), ...(result.response?.items.flatMap(item => item.remaining_gaps) ?? [])])], consumed: result.consumed };
      await c.query("UPDATE egap_review_v2.specs SET execution_context=$3 WHERE request_id=$1 AND revision=$2", [request.id, result.request_spec_revision, executionContext]);
      let currentBundle = (await c.query<BundleRow>("SELECT * FROM egap_review_v2.bundles WHERE id=$1", [row.current_bundle_id])).rows[0]!;
      let fulfillment = "UNASSESSED"; let responseId: string | null = null;
      if (result.response) {
        const response = result.response;
        const body = response as unknown as Record<string, unknown>;
        if (response.response_sha256 !== this.responseDigest(body) || response.request_id !== result.request_id || response.request_spec_revision !== result.request_spec_revision || response.run_id !== result.run_id
          || response.request_spec_sha256 !== spec.input.request_spec_sha256 || response.case_id !== row.id || response.case_scope_revision !== spec.input.case_scope_revision || response.candidate_sha !== spec.input.candidate_sha
          || response.tenant_id !== spec.input.tenant_id || response.historical_run_id !== spec.input.historical_run_id || response.task_id !== spec.input.task_id || response.obligation_id !== spec.input.obligation_id
          || response.source_bundle_id !== spec.input.source_bundle_id || response.source_bundle_sha256 !== spec.input.source_bundle_sha256 || response.status !== "PROPOSED" || response.qa_acceptance !== "NOT_EVALUATED") fail("RESPONSE_BINDING_ERROR");
        if (response.items.length !== spec.input.request_items.length || new Set(response.items.map(i => i.item_id)).size !== response.items.length) fail("RESPONSE_ITEMS_MISMATCH");
        const refs = new Set<string>();
        for (const item of response.items) {
          const expected = spec.input.request_items.find(i => i.item_id === item.item_id); if (!expected) fail("RESPONSE_ITEMS_MISMATCH");
          if (!["PROVIDED", "PARTIAL", "UNAVAILABLE", "CONFLICTING"].includes(item.validated_disposition)) fail("INVALID_RESPONSE_DISPOSITION");
          for (const location of item.supporting_locations) {
            const material = spec.input.materials.find(m => m.material_ref === location.material_ref);
            const receipt = result.read_receipts?.find(r => r.receipt_ref === location.receipt_ref);
            if (!material || !receipt || receipt.material_ref !== material.material_ref || receipt.source_sha256 !== material.sha256 || receipt.status !== "COMPLETED"
              || !Number.isSafeInteger(location.start_line) || !Number.isSafeInteger(location.end_line) || location.start_line < 1 || location.end_line < location.start_line
              || Number(receipt.start_line) > location.start_line || Number(receipt.end_line) < location.end_line || typeof receipt.content !== "string"
              || digest(receipt.content) !== receipt.fragment_sha256
              || material.content.split("\n").slice(Number(receipt.start_line) - 1, Number(receipt.end_line)).join("\n") !== receipt.content) fail("RESPONSE_READ_RECEIPT_INVALID");
          }
          for (const ref of item.evidence_refs) {
            const material = spec.input.materials.find(m => m.material_ref === ref); if (!material) fail("UNREGISTERED_RESPONSE_MATERIAL");
            const { candidate_sha: _sha, source_ref: _source, source_sha256: _sourceHash, ...target } = material.provenance;
            if (canonical(target) !== canonical(expected.target) || material.kind !== expected.kind) fail("RESPONSE_MATERIAL_TARGET_MISMATCH");
            if (!item.supporting_locations.some(l => l.material_ref === ref && item.read_receipt_refs.includes(l.receipt_ref))) fail("RESPONSE_READ_RECEIPT_REQUIRED");
            refs.add(ref);
          }
          if (item.validated_disposition === "PROVIDED" && (!item.evidence_refs.length || item.remaining_gaps.length)) fail("INCOMPLETE_PROVIDED_ITEM");
        }
        const required = response.items.filter(i => spec.input.request_items.find(r => r.item_id === i.item_id)!.required);
        fulfillment = required.every(i => i.validated_disposition === "PROVIDED") ? "COMPLETE" : required.some(i => i.validated_disposition === "CONFLICTING") ? "CONFLICTING"
          : required.every(i => i.validated_disposition === "UNAVAILABLE") ? "UNAVAILABLE" : "PARTIAL";
        const delta = [...refs].map(ref => spec.input.materials.find(m => m.material_ref === ref)!);
        // Worker 不能把未经冻结授权的任意材料借 delta 导入。
        for (const supplied of result.materials ?? []) if (!delta.some(m => canonical(m) === canonical(supplied))) fail("UNAUTHORIZED_MATERIAL_DELTA");
        if (!archived && delta.length) currentBundle = await this.mergeBundle(c, row, currentBundle, delta);
        responseId = response.response_id;
        const old = (await c.query<{ digest: string; request_id: string }>("SELECT digest,request_id FROM egap_review_v2.responses WHERE id=$1", [responseId])).rows[0];
        if (old && (old.digest !== response.response_sha256 || old.request_id !== request.id)) fail("IMMUTABLE_RESPONSE_CONFLICT");
        if (old && request.current_response_id !== responseId && !archived) fail("RESPONSE_ALREADY_SUPERSEDED");
        if (old) {
          // Runtime outbox 的 effect 身份改变也不能把同一响应重新置为待审，更不能撤销 QA 已接受状态。
          const replay = { effect_id: effectId, request_id: request.id, response_id: responseId, archived, bundle_id: currentBundle.id, bundle_digest: currentBundle.digest, fulfillment_state: request.fulfillment_state, execution_state: request.execution_state, duplicate_response: true };
          await c.query("INSERT INTO egap_review_v2.inbox(effect_id,payload_digest,result) VALUES($1,$2,$3)", [effectId, payloadDigest, replay]);
          return replay;
        }
        if (!old) await c.query("INSERT INTO egap_review_v2.responses(id,request_id,spec_revision,digest,body,bundle_id,bundle_digest,archived,read_receipts) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)", [responseId, request.id, result.request_spec_revision, response.response_sha256, body, currentBundle.id, currentBundle.digest, archived, JSON.stringify(result.read_receipts ?? [])]);
        if (!archived) {
          if (request.current_response_id && request.current_response_id !== responseId) await c.query("UPDATE egap_review_v2.responses SET review_state='SUPERSEDED' WHERE id=$1", [request.current_response_id]);
          await c.query("UPDATE egap_review_v2.requests SET current_response_id=$2,review_state='PENDING_REVIEW',fulfillment_state=$3 WHERE id=$1", [request.id, responseId, fulfillment]);
          await c.query("UPDATE egap_review_v2.tasks SET status='RESPONSE_READY',task_revision=task_revision+1 WHERE id=$1", [request.task_id]);
        }
      }
      if (!archived) await c.query("UPDATE egap_review_v2.requests SET execution_state=$2,clarification=$3,execution_context=$4 WHERE id=$1", [request.id, result.execution_state, result.clarification ?? null, executionContext]);
      await c.query("UPDATE egap_review_v2.outbox SET state=$3,lease_until=NULL WHERE request_id=$1 AND spec_revision=$2", [request.id, result.request_spec_revision, result.execution_state === "UNKNOWN" ? "UNKNOWN" : "DONE"]);
      const receipt = { effect_id: effectId, request_id: request.id, response_id: responseId, archived, bundle_id: currentBundle.id, bundle_digest: currentBundle.digest, fulfillment_state: fulfillment, execution_state: result.execution_state };
      await c.query("INSERT INTO egap_review_v2.inbox(effect_id,payload_digest,result) VALUES($1,$2,$3)", [effectId, payloadDigest, receipt]);
      await this.event(c, row.id, archived ? "LATE_RESULT_ARCHIVED" : "EVIDENCE_RUNTIME_RESULT", receipt); return receipt;
    });
  }
  private async mergeBundle(c: PoolClient, row: CaseRow, current: BundleRow, delta: EvidenceMaterial[]): Promise<BundleRow> {
    if (digest(canonical(current.manifest)) !== current.digest) fail("BUNDLE_INTEGRITY_ERROR");
    const manifest = [...current.manifest];
    for (const material of delta) {
      const meta = materialMeta(material);
      if (manifest.some(({ conflict: _conflict, ...m }) => canonical(m) === canonical(meta))) continue;
      // 不同正文仍保留两份不可变来源；显式 conflict 标记让相关任务重新审查。
      const collision = manifest.some(m => m.material_ref === material.material_ref || (object(m.provenance) && m.kind === material.kind && m.provenance.job_id === material.provenance.job_id && m.provenance.phase === material.provenance.phase && m.provenance.ticket_revision === material.provenance.ticket_revision && m.sha256 !== material.sha256));
      manifest.push(collision ? { ...meta, conflict: true } : meta);
    }
    const sha = digest(canonical(manifest)); if (sha === current.digest) return current;
    const next: BundleRow = { id: `${row.id}:b-${randomUUID()}`, case_id: row.id, parent_id: current.id, digest: sha, manifest };
    await c.query("INSERT INTO egap_review_v2.bundles(id,case_id,parent_id,digest,manifest) VALUES($1,$2,$3,$4,$5)", [next.id, row.id, current.id, sha, JSON.stringify(manifest)]);
    await c.query("UPDATE egap_review_v2.cases SET current_bundle_id=$2 WHERE id=$1", [row.id, next.id]);
    // 本切片明确登记两种义务均依赖草稿、最终备注和交付关系；其他未来义务不自动失效。
    await c.query("UPDATE egap_review_v2.tasks SET bound_bundle_id=$2,bound_bundle_digest=$3,task_revision=task_revision+1,status=CASE WHEN status='COMPLETED' THEN 'PENDING_REVIEW' ELSE status END WHERE case_id=$1 AND obligation_id IN ('EVIDENCE_LINKAGE','SEMANTIC_REVIEW')", [row.id, next.id, next.digest]);
    return next;
  }
  /** 可安全重试的派发故障回到 PENDING；副作用是否发生未知时保留 mapping 与预留配额。 */
  async failWork(work: WorkItem, code: string, unknown = false, blocked = false): Promise<void> {
    if (!/^[A-Z0-9_]{1,80}$/.test(code)) fail("INVALID_ERROR_CODE", 400);
    await this.tx(async c => {
      const changed = await c.query("UPDATE egap_review_v2.outbox SET state=$4,last_error=$5,lease_until=CASE WHEN $4='UNKNOWN' THEN now()+interval '30 seconds' ELSE NULL END WHERE id=$1 AND worker_id=$2 AND attempts=$3 AND state IN ('LEASED','RECONCILING') RETURNING request_id,spec_revision", [work.id, work.worker_id, work.attempts, unknown ? "UNKNOWN" : blocked ? "BLOCKED" : "PENDING", code]);
      if (!changed.rowCount) return;
      if (unknown || blocked) await c.query("UPDATE egap_review_v2.requests SET execution_state=$3,clarification=$4 WHERE id=$1 AND request_spec_revision=$2 AND withdrawn=false", [work.request_id, work.request_spec_revision, unknown ? "UNKNOWN" : "BLOCKED", blocked ? code : null]);
    });
  }
}
