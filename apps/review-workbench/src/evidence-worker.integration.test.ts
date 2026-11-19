import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import type { Pool } from "pg";
import { evidenceReviewDigest, type EvidenceResponse, type EvidenceReviewInput, type EvidenceTarget } from "@egap/contracts";
import type { Limits, Runtime } from "@egap/verify-runtime";
import { assertLocalDatabase } from "./config.js";
import { EvidenceWorker, type GraphLaunch, type GraphRunner } from "./evidence-worker.js";
import { RegisteredArchive } from "./evidence-mcp.js";
import { ReviewStore, digest } from "./store.js";
import { ITEM_TEMPLATES, WorkflowStore } from "./workflow-store.js";
import type { Actor, ImportCase } from "./types.js";
import type { EvidenceMaterial, RuntimeResult, WorkItem, WorkflowBasis } from "./workflow-types.js";

const url = process.env["EGAP_REVIEW_TEST_DATABASE_URL"];
const pgTest = (name: string, fn: (t: TestContext) => Promise<void>): void => { test(name, { skip: !url, timeout: 25_000 }, fn); };

/**
 * 仅给本文件的队列认领增加 request_id 限制，避免并行测试消费别人的 Case。
 * reserveRunMapping / failWork / applyRuntimeResult 与预算、Bundle、inbox 均运行真实 WorkflowStore。
 * 这不是队列调度或模型正确性测试；队列协议另由 workflow-v2.integration.test.ts 覆盖。
 */
class IsolatedWorkflowStore extends WorkflowStore {
  failNextApplications = 0;
  applicationAttempts = 0;
  constructor(pool: Pool, readonly requestId: string) { super(pool); }
  private async claimIsolated(workerId: string, leaseMs: number, reconcile: boolean): Promise<WorkItem | null> {
    const c = await this.pool.connect();
    try {
      await c.query("BEGIN");
      const predicate = reconcile
        ? "o.state IN ('UNKNOWN','RECONCILING') AND (o.lease_until IS NULL OR o.lease_until<now())"
        : "(o.state='PENDING' OR (o.state='LEASED' AND o.lease_until<now())) AND r.withdrawn=false AND o.spec_revision=r.request_spec_revision";
      const row = (await c.query<{ id: string; request_id: string; spec_revision: number; attempts: number }>(
        `SELECT o.* FROM egap_review_v2.outbox o JOIN egap_review_v2.requests r ON r.id=o.request_id WHERE o.request_id=$1 AND ${predicate} ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT 1`, [this.requestId])).rows[0];
      if (!row) { await c.query("COMMIT"); return null; }
      await c.query("UPDATE egap_review_v2.outbox SET state=$2,worker_id=$3,lease_until=now()+($4::int*interval '1 millisecond'),attempts=attempts+1 WHERE id=$1", [row.id, reconcile ? "RECONCILING" : "LEASED", workerId, leaseMs]);
      await c.query("COMMIT");
      return { id: row.id, request_id: row.request_id, request_spec_revision: row.spec_revision, worker_id: workerId, attempts: row.attempts + 1 };
    } catch (error) { await c.query("ROLLBACK"); throw error; } finally { c.release(); }
  }
  override claimWork(workerId: string, leaseMs = 30_000): Promise<WorkItem | null> { return this.claimIsolated(workerId, leaseMs, false); }
  override claimReconciliationWork(workerId: string, leaseMs = 30_000): Promise<WorkItem | null> { return this.claimIsolated(workerId, leaseMs, true); }
  override async applyRuntimeResult(effectId: string, result: RuntimeResult): Promise<Record<string, unknown>> {
    this.applicationAttempts += 1;
    if (this.failNextApplications > 0) { this.failNextApplications -= 1; throw new Error("INJECTED_INBOX_OUTAGE"); }
    return super.applyRuntimeResult(effectId, result);
  }
}

/**
 * 窄 Runtime 替身仅提供 Worker 消费的持久化投影和 accept 身份校验。
 * 不执行 LLM、Python Graph 或 MCP；因此本文件的通过只证明 Worker/PG 回流与恢复。
 */
class RuntimeProjectionDouble {
  readonly configDigest = "c".repeat(64);
  readonly accepted: { input: EvidenceReviewInput; token: string; limits: Limits; run_id: string }[] = [];
  readonly projections = new Map<string, Record<string, unknown>>();
  reconciliationReads = 0;
  async acceptRun(input: EvidenceReviewInput, token: string, limits: Limits, runId: string): Promise<void> {
    const prior = this.accepted.find(a => a.run_id === runId);
    const next = { input: structuredClone(input), token, limits: structuredClone(limits), run_id: runId };
    if (prior) assert.deepEqual(next, prior, "重启时必须复用原 input/token/limits/run 身份");
    this.accepted.push(next);
    if (!this.projections.has(runId)) this.projections.set(runId, { status: "ACTIVE", event_seq: 1, operations: [], model_calls: [], model_calls_used: 0, reads_used: 0, read_receipts: [] });
  }
  async projection(runId: string): Promise<Record<string, unknown>> { const p = this.projections.get(runId); assert(p); return structuredClone(p); }
  async reconcileProjection(runId: string): Promise<Record<string, unknown>> { this.reconciliationReads += 1; return this.projection(runId); }
  terminal(runId: string): void {
    const input = this.accepted.find(a => a.run_id === runId)!.input;
    const receipts: Record<string, unknown>[] = [];
    const items = input.request_items.map(item => {
      const material = input.materials.find(m => m.kind === item.kind && m.provenance.job_id === item.target.job_id)!;
      const receiptRef = `read:${randomUUID()}`;
      receipts.push({ receipt_ref: receiptRef, status: "COMPLETED", material_ref: material.material_ref, source_sha256: material.sha256, fragment_sha256: digest(material.content), start_line: 1, end_line: 1, content: material.content });
      return { item_id: item.item_id, proposed_disposition: "PROVIDED" as const, validated_disposition: "PROVIDED" as const, association_status: "BOUND" as const, semantic_acceptance: "NOT_EVALUATED" as const,
        evidence_refs: [material.material_ref], read_receipt_refs: [receiptRef], supporting_locations: [{ material_ref: material.material_ref, receipt_ref: receiptRef, start_line: 1, end_line: 1 }], remaining_gaps: [], unavailable_reason: null, explanation: "仅供 Worker 事务测试的合成关联响应" };
    });
    const responseBody: Omit<EvidenceResponse, "response_sha256"> = { schema_version: 2, response_id: randomUUID(), request_id: input.request_id, request_spec_revision: input.request_spec_revision, request_spec_sha256: input.request_spec_sha256,
      run_id: runId, operation_id: randomUUID(), candidate_sha: input.candidate_sha, historical_run_id: input.historical_run_id, tenant_id: input.tenant_id, case_id: input.case_id, case_scope_revision: input.case_scope_revision,
      task_id: input.task_id, obligation_id: input.obligation_id, source_bundle_id: input.source_bundle_id, source_bundle_sha256: input.source_bundle_sha256, status: "PROPOSED", qa_acceptance: "NOT_EVALUATED", items, summary: "TEST_ONLY：只验证工作流恢复，不代表真实模型通过" };
    this.projections.set(runId, { status: "PLANNED", stop_reason: "EVIDENCE_RESPONSE_PROPOSED", event_seq: 8, model_calls: [{ status: "COMPLETED" }], model_calls_used: 2, reads_used: receipts.length, read_receipts: receipts,
      operations: [{ status: "COMPLETED", result: { evidence_response: { ...responseBody, response_sha256: evidenceReviewDigest(responseBody) } } }] });
  }
  unknown(runId: string): void { this.projections.set(runId, { status: "BLOCKED", stop_reason: "MODEL_RESULT_UNKNOWN", event_seq: 4, model_calls: [{ status: "UNKNOWN" }], model_calls_used: 1, reads_used: 0, read_receipts: [], operations: [] }); }
}

async function fixture(t: TestContext) {
  assertLocalDatabase(url!);
  const legacy = new ReviewStore(url!); const setupStore = new WorkflowStore(legacy.pool); await setupStore.migrate();
  const id = `worker-test-${randomUUID()}`;
  const privateRoot = await mkdtemp(join(tmpdir(), "egap-evidence-worker-test-"));
  const qa: Actor = { id: "qa-test", display_name: "隔离测试 QA", role: "QA", tenant_id: id };
  const owner: Actor = { id: "owner-test", display_name: "隔离测试负责人", role: "BUSINESS", tenant_id: id };
  const target: EvidenceTarget = { tenant_id: id, historical_run_id: `${id}:history`, job_id: `${id}:job`, ticket_id: "test-ticket", ticket_revision: 1, phase: "before" };
  const imported: ImportCase = { id, tenant_id: id, title: "Worker 回流恢复隔离测试", candidate_sha: "a".repeat(40), baseline_sha: null, execution_mode: "TEST_ONLY", source_ref: "worker-recovery-fixture", source_sha256: "b".repeat(64), machine_result: {}, limitations: ["TEST_ONLY_NO_LLM"], evidence: [], obligations: [] };
  const materials: EvidenceMaterial[] = ITEM_TEMPLATES.slice(0, 3).map(template => {
    const content = JSON.stringify({ kind: template.kind, text: "隔离测试原件", ...target });
    return { material_ref: `${id}:${template.kind}`, kind: template.kind, title: template.title, content, sha256: digest(content), provenance: { ...target, candidate_sha: imported.candidate_sha, source_ref: `fixture:${template.kind}`, source_sha256: digest(content) } };
  });
  t.after(async () => {
    try {
      await legacy.pool.query("DELETE FROM egap_review_v2.inbox WHERE result->>'request_id' IN (SELECT id FROM egap_review_v2.requests WHERE case_id=$1)", [id]);
      for (const table of ["run_mappings", "outbox", "specs", "responses"]) await legacy.pool.query(`DELETE FROM egap_review_v2.${table} WHERE request_id IN (SELECT id FROM egap_review_v2.requests WHERE case_id=$1)`, [id]);
      for (const table of ["requests", "commands", "events", "tasks", "bundles"]) await legacy.pool.query(`DELETE FROM egap_review_v2.${table} WHERE case_id=$1`, [id]);
      await legacy.pool.query("DELETE FROM egap_review_v2.cases WHERE id=$1", [id]);
    } finally { await legacy.close(); await rm(privateRoot, { recursive: true, force: true }); }
  });
  await setupStore.seedCase({ legacy: imported, archive: { historical_run_id: target.historical_run_id, targets: [target], materials }, actors: [qa, owner], owner_id: owner.id, qa_assignee_id: qa.id });
  const taskId = String((await setupStore.listTasks(qa)).tasks.find(task => task.obligation_id === "EVIDENCE_LINKAGE")!.id);
  const basis = async (): Promise<WorkflowBasis> => {
    const task = (await setupStore.getTask(taskId, qa)).task as Record<string, unknown>;
    return { idempotency_key: randomUUID(), expected_task_revision: Number(task.task_revision), expected_assignment_revision: Number(task.assignment_revision), expected_case_scope_revision: Number(task.case_scope_revision), reviewed_bundle_id: String(task.bound_bundle_id), reviewed_bundle_digest: String(task.bound_bundle_digest), candidate_sha: String(task.candidate_sha) };
  };
  await setupStore.claim(taskId, qa, await basis());
  const requested = await setupStore.requestEvidence(taskId, qa, { ...await basis(), goal: "TEST_ONLY：核对 Worker 回流的三种归档原件", items: ITEM_TEMPLATES.slice(0, 3).map((template, i) => ({ item_id: `E${i + 1}`, kind: template.kind, target, required: true, acceptance_criteria: template.acceptance_criteria })) });
  const requestId = String(requested.request_id); const store = new IsolatedWorkflowStore(legacy.pool, requestId); const runtime = new RuntimeProjectionDouble();
  const worker = (graph: GraphRunner) => new EvidenceWorker(store, runtime as unknown as Runtime, new RegisteredArchive(), "http://127.0.0.1:1", `worker-test-${randomUUID()}`, graph, privateRoot);
  const expireLease = async () => { await legacy.pool.query("UPDATE egap_review_v2.outbox SET lease_until=now()-interval '1 second' WHERE request_id=$1", [requestId]); };
  const counts = async () => (await legacy.pool.query<{ mappings: number; responses: number; inbox: number }>(`SELECT
    (SELECT count(*)::int FROM egap_review_v2.run_mappings WHERE request_id=$1) AS mappings,
    (SELECT count(*)::int FROM egap_review_v2.responses WHERE request_id=$1) AS responses,
    (SELECT count(*)::int FROM egap_review_v2.inbox WHERE result->>'request_id'=$1) AS inbox`, [requestId])).rows[0]!;
  const budget = async () => (await store.getTask(taskId, qa)).case as Record<string, unknown>;
  return { legacy, store, runtime, worker, requestId, privateRoot, expireLease, counts, budget };
}

pgTest("Worker + PG：过期 lease 重启复用 run/token/冻结映射，终态重放不重复 Graph 或 inbox", async t => {
  const x = await fixture(t); let graphCalls = 0;
  let announceGraph!: (launch: GraphLaunch) => void; let releaseGraph!: () => void;
  const entered = new Promise<GraphLaunch>(resolve => { announceGraph = resolve; });
  const hold = new Promise<void>(resolve => { releaseGraph = resolve; });
  const first = x.worker(async launch => { graphCalls += 1; announceGraph(launch); await hold; });
  const pending = first.runOnce();
  try {
    const launch = await entered;
    const mapping = (await x.store.loadRequest(x.requestId)).mapping!;
    assert.equal(mapping.run_id, launch.run_id);
    assert.equal((await readFile(launch.token_file, "utf8")).trim(), x.runtime.accepted[0]!.token);
    const credentialBefore = await readFile(mapping.credential_ref, "utf8");
    x.runtime.terminal(launch.run_id); await x.expireLease();
    const restarted = x.worker(async () => { graphCalls += 1; throw new Error("TERMINAL_GRAPH_MUST_NOT_RUN"); });
    const result = await restarted.runOnce();
    assert.equal(result?.execution_state, "FINISHED"); assert.equal(graphCalls, 1);
    assert.equal(x.runtime.accepted.length, 2); assert.deepEqual(x.runtime.accepted[1], x.runtime.accepted[0]);
    assert.equal(await readFile(mapping.credential_ref, "utf8"), credentialBefore);
    assert.equal((await readdir(x.privateRoot)).filter(name => name.endsWith("credential.private.json")).length, 1);
    releaseGraph(); await pending;
    assert.deepEqual(await x.counts(), { mappings: 1, responses: 1, inbox: 1 });
    assert.deepEqual((await x.budget()).budget_consumed, { model_calls: 2, read_calls: 3 });
    assert.deepEqual((await x.budget()).budget_reserved, { model_calls: 0, read_calls: 0 });
  } finally { releaseGraph(); await pending; }
});

pgTest("Worker + PG：首次 inbox 故障进入 UNKNOWN，同 Run 只读对账恢复且不再次派发", async t => {
  const x = await fixture(t); let graphCalls = 0; x.store.failNextApplications = 1;
  const worker = x.worker(async launch => { graphCalls += 1; x.runtime.terminal(launch.run_id); });
  const failed = await worker.runOnce(); assert.equal(failed?.execution_state, "UNKNOWN");
  const original = (await x.store.loadRequest(x.requestId)).mapping!;
  assert.deepEqual(await x.counts(), { mappings: 1, responses: 0, inbox: 0 });
  assert.deepEqual((await x.budget()).budget_reserved, { model_calls: 6, read_calls: 4 });
  assert.equal(await worker.reconcileOnce(), null, "UNKNOWN 的冷却窗口不能被忙循环绕过");
  await x.expireLease(); const recovered = await worker.reconcileOnce();
  assert.equal(recovered?.reconciliation, "RECOVERED_WITHOUT_REDISPATCH"); assert.equal(recovered?.execution_state, "FINISHED");
  assert.equal(graphCalls, 1); assert.equal(x.runtime.accepted.length, 1); assert.equal(x.runtime.reconciliationReads, 1);
  assert.equal((await x.store.loadRequest(x.requestId)).mapping!.run_id, original.run_id);
  assert.deepEqual(await x.counts(), { mappings: 1, responses: 1, inbox: 1 });
  assert.deepEqual((await x.budget()).budget_reserved, { model_calls: 0, read_calls: 0 });
  assert.deepEqual((await x.budget()).budget_consumed, { model_calls: 2, read_calls: 3 });
  assert.equal(await worker.runOnce(), null); assert.equal(await worker.reconcileOnce(), null);
});

pgTest("Worker + PG：Runtime 仍未决时只读对账保留 UNKNOWN、原映射和全部预留预算", async t => {
  const x = await fixture(t); let graphCalls = 0;
  const worker = x.worker(async launch => { graphCalls += 1; x.runtime.unknown(launch.run_id); });
  assert.equal((await worker.runOnce())?.execution_state, "UNKNOWN");
  const before = await x.store.loadRequest(x.requestId); const applications = x.store.applicationAttempts;
  const result = await worker.reconcileOnce();
  assert.equal(result?.reconciliation, "NO_REDISPATCH"); assert.equal(result?.execution_state, "UNKNOWN");
  assert.equal(x.store.applicationAttempts, applications, "未决投影不能制造新完成结果");
  assert.equal(x.runtime.accepted.length, 1); assert.equal(graphCalls, 1); assert.equal(x.runtime.reconciliationReads, 1);
  const after = await x.store.loadRequest(x.requestId); assert.deepEqual(after.mapping, before.mapping); assert.equal(after.execution_state, "UNKNOWN");
  assert.deepEqual((await x.budget()).budget_reserved, { model_calls: 6, read_calls: 4 });
  assert.deepEqual((await x.budget()).budget_consumed, { model_calls: 0, read_calls: 0 });
  assert.deepEqual(await x.counts(), { mappings: 1, responses: 0, inbox: 1 });
  assert.equal(await worker.reconcileOnce(), null); assert.equal(await worker.runOnce(), null);
});
