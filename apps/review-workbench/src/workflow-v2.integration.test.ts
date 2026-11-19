import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test, { type TestContext } from "node:test";
import { evidenceReviewDigest, type EvidenceResponse, type EvidenceTarget } from "@egap/contracts";
import { ReviewStore, digest } from "./store.js";
import { WorkflowStore, ITEM_TEMPLATES, REVIEW_POLICY } from "./workflow-store.js";
import { ReviewError, type Actor, type ImportCase } from "./types.js";
import type { EvidenceItem, EvidenceMaterial, RequestSnapshot, RunMapping, RuntimeResult, WorkflowBasis, WorkItem } from "./workflow-types.js";
const url = process.env["EGAP_REVIEW_TEST_DATABASE_URL"];
const pgTest = (name: string, fn: (t: TestContext) => Promise<void>): void => { test(name, { skip: !url, timeout: 25_000 }, fn); };
const errorCode = (code: string) => (e: unknown) => e instanceof ReviewError && e.code === code;

/** 协议反例专用隔离对象；从不把这里的合成材料作为真实历史实验报告。 */
async function setup(t: TestContext) {
  const legacy = new ReviewStore(url!); const store = new WorkflowStore(legacy.pool); await store.migrate();
  const id = `v2-test-${randomUUID()}`, tenant = id;
  const actor = (id: string, role: Actor["role"]): Actor => ({ id, role, tenant_id: tenant, display_name: id });
  const qa = actor("local-qa", "QA"), peer = actor("local-qa-peer", "QA"), owner = actor("local-business", "BUSINESS");
  const input: ImportCase = { id, tenant_id: tenant, title: "v2 事务协议验收", candidate_sha: "a".repeat(40), baseline_sha: null, execution_mode: "TEST_ONLY", source_ref: "fixture", source_sha256: "b".repeat(64), machine_result: {}, limitations: ["TEST_ONLY"], evidence: [], obligations: [] };
  const targets: EvidenceTarget[] = ["before", "after"].map((phase, index) => ({ tenant_id: tenant, historical_run_id: `${id}:history`, job_id: `${id}:job${index}`, ticket_id: "ticket:1", ticket_revision: index + 1, phase }));
  const materials: EvidenceMaterial[] = targets.flatMap((target, index) => ITEM_TEMPLATES.slice(0, 3).map(template => {
    const content = JSON.stringify({ ...target, kind: template.kind, text: "隔离测试原件" });
    return { material_ref: `${id}:${index}:${template.kind}`, kind: template.kind, title: template.title, content, sha256: digest(content), provenance: { ...target, candidate_sha: input.candidate_sha, source_ref: `fixture:${index}:${template.kind}`, source_sha256: digest(content) } };
  }));
  await store.seedCase({ legacy: input, archive: { historical_run_id: targets[0]!.historical_run_id, targets, materials }, actors: [qa, peer, owner], owner_id: owner.id, qa_assignee_id: qa.id });
  t.after(async () => {
    await legacy.pool.query("DELETE FROM egap_review_v2.inbox WHERE result->>'request_id' IN (SELECT id FROM egap_review_v2.requests WHERE case_id=$1)", [id]);
    for (const table of ["run_mappings", "outbox", "specs", "responses"]) await legacy.pool.query(`DELETE FROM egap_review_v2.${table} WHERE request_id IN (SELECT id FROM egap_review_v2.requests WHERE case_id=$1)`, [id]);
    for (const table of ["requests", "commands", "events", "tasks", "bundles"]) await legacy.pool.query(`DELETE FROM egap_review_v2.${table} WHERE case_id=$1`, [id]);
    await legacy.pool.query("DELETE FROM egap_review_v2.cases WHERE id=$1", [id]); await legacy.close();
  });
  const tasks = (await store.listTasks(qa)).tasks;
  const evidenceTask = String(tasks.find(x => x.obligation_id === "EVIDENCE_LINKAGE")!.id);
  const semanticTask = String(tasks.find(x => x.obligation_id === "SEMANTIC_REVIEW")!.id);
  const basis = async (taskId = evidenceTask, who = qa): Promise<WorkflowBasis> => {
    const detail = await store.getTask(taskId, who); const task = detail.task as Record<string, unknown>;
    return { idempotency_key: randomUUID(), expected_task_revision: Number(task.task_revision), expected_assignment_revision: Number(task.assignment_revision), expected_case_scope_revision: Number(task.case_scope_revision), reviewed_bundle_id: String(task.bound_bundle_id), reviewed_bundle_digest: String(task.bound_bundle_digest), candidate_sha: String(task.candidate_sha) };
  };
  const items = (target = targets[0]!, raw = false): EvidenceItem[] => ITEM_TEMPLATES.slice(0, raw ? 4 : 3).map((kind, i) => ({ item_id: `E${i + 1}`, kind: kind.kind, target, required: true, acceptance_criteria: kind.acceptance_criteria }));
  const request = async (taskId = evidenceTask, requestItems = items()) => {
    const d = await store.getTask(taskId, qa); if (!(d.task as Record<string, unknown>).claimed) await store.claim(taskId, qa, await basis(taskId));
    const r = await store.requestEvidence(taskId, qa, { ...await basis(taskId), goal: "核对历史原件的来源与交付关联", items: requestItems }); return String(r.request_id);
  };
  const work = async (requestId: string, allocation = { model_calls: 6, read_calls: 8 }): Promise<{ work: WorkItem; snapshot: RequestSnapshot; mapping: RunMapping }> => {
    const w = await store.claimWork(`worker-${randomUUID()}`); assert(w); assert.equal(w.request_id, requestId);
    const snapshot = await store.loadRequest(requestId);
    const mapping: RunMapping = { request_id: requestId, request_spec_revision: snapshot.request_spec_revision, run_id: randomUUID(), frozen_input_digest: snapshot.input_digest, execution_profile: "LOCAL_EVIDENCE_REVIEW", config_digest: "c".repeat(64), limits: { model_calls: allocation.model_calls, reads: allocation.read_calls }, credential_ref: `local-vault:${randomUUID()}`, allocation, consumed: { model_calls: 0, read_calls: 0 }, settlement: "RESERVED" };
    await store.reserveRunMapping(w, mapping); return { work: w, snapshot, mapping };
  };
  return { legacy, store, id, qa, peer, owner, targets, materials, evidenceTask, semanticTask, basis, items, request, work };
}
function result(snapshot: RequestSnapshot, mapping: RunMapping): RuntimeResult {
  const b = snapshot.input; const receipts: Record<string, unknown>[] = [];
  const items = b.request_items.map(item => {
    const material = b.materials.find(m => m.kind === item.kind && m.provenance.job_id === item.target.job_id);
    const receiptRef = `read:${randomUUID()}`;
    if (material) receipts.push({ receipt_ref: receiptRef, status: "COMPLETED", material_ref: material.material_ref, start_line: 1, end_line: 1, source_sha256: material.sha256, fragment_sha256: digest(material.content), content: material.content });
    return { item_id: item.item_id, proposed_disposition: material ? "PROVIDED" as const : "UNAVAILABLE" as const, validated_disposition: material ? "PROVIDED" as const : "UNAVAILABLE" as const,
      association_status: material ? "BOUND" as const : "NO_SUPPORTING_MATERIAL" as const, semantic_acceptance: "NOT_EVALUATED" as const,
      evidence_refs: material ? [material.material_ref] : [], read_receipt_refs: material ? [receiptRef] : [], supporting_locations: material ? [{ material_ref: material.material_ref, receipt_ref: receiptRef, start_line: 1, end_line: 1 }] : [],
      remaining_gaps: material ? [] : ["未采集模型原始响应"], unavailable_reason: material ? null : "NOT_CAPTURED_IN_PINNED_ARCHIVE" as const, explanation: "仅核对来源关联" };
  });
  const body: Omit<EvidenceResponse, "response_sha256"> = { schema_version: 2, response_id: randomUUID(), request_id: b.request_id, request_spec_revision: b.request_spec_revision, request_spec_sha256: b.request_spec_sha256, run_id: mapping.run_id, operation_id: randomUUID(), candidate_sha: b.candidate_sha, historical_run_id: b.historical_run_id, tenant_id: b.tenant_id, case_id: b.case_id, case_scope_revision: b.case_scope_revision, task_id: b.task_id, obligation_id: b.obligation_id, source_bundle_id: b.source_bundle_id, source_bundle_sha256: b.source_bundle_sha256, status: "PROPOSED", qa_acceptance: "NOT_EVALUATED", items, summary: "逐项来源核对；语义未评价" };
  return { request_id: b.request_id, request_spec_revision: b.request_spec_revision, run_id: mapping.run_id, input_digest: snapshot.input_digest, execution_state: "FINISHED", response: { ...body, response_sha256: evidenceReviewDigest(body) }, consumed: { model_calls: 2, read_calls: receipts.length }, read_receipts: receipts };
}
const accept = (base: WorkflowBasis, r: RuntimeResult) => ({ ...base, request_spec_revision: r.request_spec_revision, response_id: r.response!.response_id, response_digest: r.response!.response_sha256, review_policy_version: REVIEW_POLICY });

pgTest("v2 PG：独立任务并行接手、同任务竞争与角色资格", async t => {
  const x = await setup(t); const a = await x.basis(), b = await x.basis(x.semanticTask);
  await Promise.all([x.store.claim(x.evidenceTask, x.qa, a), x.store.claim(x.semanticTask, x.qa, b)]);
  assert.equal((await x.store.getTask(x.evidenceTask, x.qa)).task && (await x.store.listTasks(x.qa)).tasks.length, 2);
  await assert.rejects(x.store.requestEvidence(x.evidenceTask, x.peer, { ...await x.basis(), goal: "越权请求", items: x.items() }), errorCode("NOT_TASK_ASSIGNEE"));
  const unsupported = x.items(); unsupported[0]!.acceptance_criteria = ["所有退款已获批准"];
  await assert.rejects(x.store.requestEvidence(x.evidenceTask, x.qa, { ...await x.basis(), goal: "未经实现的业务门禁", items: unsupported }), errorCode("UNSUPPORTED_ACCEPTANCE_CRITERIA"));
  await assert.rejects(x.store.reassign(x.evidenceTask, x.qa, { ...await x.basis(), assignee_id: x.owner.id }), errorCode("INELIGIBLE_ASSIGNEE"));
  const transfer = { ...await x.basis(), assignee_id: x.peer.id };
  const races = await Promise.allSettled([x.store.reassign(x.evidenceTask, x.qa, transfer), x.store.reassign(x.evidenceTask, x.qa, { ...transfer, idempotency_key: randomUUID() })]);
  assert.equal(races.filter(r => r.status === "fulfilled").length, 1);
});
pgTest("v2 PG：转派不断取证，旧负责人不能接受，新负责人接手后接受精确响应", async t => {
  const x = await setup(t); const req = await x.request(); const { snapshot, mapping } = await x.work(req); const r = result(snapshot, mapping);
  await x.store.reassign(x.evidenceTask, x.qa, { ...await x.basis(), assignee_id: x.peer.id });
  await x.store.applyRuntimeResult(randomUUID(), r);
  await assert.rejects(x.store.acceptResponse(req, x.qa, accept(await x.basis(), r)), errorCode("NOT_TASK_ASSIGNEE"));
  await x.store.claim(x.evidenceTask, x.peer, await x.basis(x.evidenceTask, x.peer));
  const command = accept(await x.basis(x.evidenceTask, x.peer), r);
  const receipt = await x.store.acceptResponse(req, x.peer, command);
  assert.equal(receipt.fulfillment_state, "COMPLETE"); assert.equal(receipt.status, "WAITING_EVIDENCE"); // 第二个目标尚未取得。
  assert.deepEqual(await x.store.acceptResponse(req, x.peer, command), receipt);
});
pgTest("v2 PG：原件缺口可接受但不能关闭义务，原件全文与回执持久化", async t => {
  const x = await setup(t); const req = await x.request(x.evidenceTask, x.items(x.targets[0], true)); const { snapshot, mapping } = await x.work(req); const r = result(snapshot, mapping);
  await x.store.applyRuntimeResult(randomUUID(), r); const response = await x.store.acceptResponse(req, x.qa, accept(await x.basis(), r));
  assert.equal(response.fulfillment_state, "PARTIAL"); assert.equal(response.status, "WAITING_EVIDENCE");
  const detail = await x.store.getTask(x.evidenceTask, x.qa); const rows = detail.requests as Record<string, unknown>[];
  assert.equal(((rows[0]!.current_response as Record<string, unknown>).read_receipts as unknown[]).length, 3);
  assert.equal((await x.store.getMaterial(x.id, x.materials[0]!.material_ref, x.qa)).content, x.materials[0]!.content);
});
pgTest("v2 PG：inbox重放、跨候选及跨Job引用拒绝，旧响应指针不能接受", async t => {
  const x = await setup(t); const req = await x.request(); const { snapshot, mapping } = await x.work(req); const r = result(snapshot, mapping);
  const corrupt = structuredClone(r); corrupt.response!.candidate_sha = "d".repeat(40); const { response_sha256: _hash, ...rest } = corrupt.response!; corrupt.response!.response_sha256 = evidenceReviewDigest(rest);
  await assert.rejects(x.store.applyRuntimeResult(randomUUID(), corrupt), errorCode("RESPONSE_BINDING_ERROR"));
  const wrong = structuredClone(r); wrong.response!.items[0]!.evidence_refs = [x.materials[3]!.material_ref]; const { response_sha256: _h, ...wrongRest } = wrong.response!; wrong.response!.response_sha256 = evidenceReviewDigest(wrongRest);
  await assert.rejects(x.store.applyRuntimeResult(randomUUID(), wrong), errorCode("RESPONSE_MATERIAL_TARGET_MISMATCH"));
  const effect = randomUUID(); const applied = await x.store.applyRuntimeResult(effect, r); assert.deepEqual(await x.store.applyRuntimeResult(effect, r), applied);
  const old = accept(await x.basis(), r); const newer = result(snapshot, mapping); await x.store.applyRuntimeResult(randomUUID(), newer);
  await assert.rejects(x.store.acceptResponse(req, x.qa, old), errorCode("STALE_TASK_REVISION"));
  await assert.rejects(x.store.acceptResponse(req, x.qa, accept(await x.basis(), r)), errorCode("STALE_RESPONSE_POINTER"));
});
pgTest("v2 PG：outbox lease恢复复用冻结映射，过期Worker不能派发", async t => {
  const x = await setup(t); const req = await x.request(); const { work, mapping } = await x.work(req);
  assert.equal(await x.store.isDispatchable(work), true); await x.legacy.pool.query("UPDATE egap_review_v2.outbox SET lease_until=now()-interval '1 second' WHERE id=$1", [work.id]);
  const next = await x.store.claimWork("recovery-worker"); assert(next); assert.equal(next.request_id, req); assert.equal(await x.store.isDispatchable(work), false); assert.equal(await x.store.renewWork(work), false);
  assert.deepEqual(await x.store.reserveRunMapping(next, mapping), mapping);
  await assert.rejects(x.store.reserveRunMapping(next, { ...mapping, credential_ref: "changed-token" }), errorCode("RUN_MAPPING_CONFLICT"));
});
pgTest("v2 PG：并发补入材料合并当前包且全部目标完成后才闭合", async t => {
  const x = await setup(t); const first = await x.request(x.evidenceTask, x.items(x.targets[0])); const a = await x.work(first);
  const second = await x.request(x.evidenceTask, x.items(x.targets[1])); const b = await x.work(second);
  const ra = result(a.snapshot, a.mapping), rb = result(b.snapshot, b.mapping);
  await Promise.all([x.store.applyRuntimeResult(randomUUID(), ra), x.store.applyRuntimeResult(randomUUID(), rb)]);
  const detail = await x.store.getTask(x.evidenceTask, x.qa); assert.equal(((detail.bundle as Record<string, unknown>).manifest as unknown[]).length, 6);
  await x.store.acceptResponse(first, x.qa, accept(await x.basis(), ra));
  const final = await x.store.acceptResponse(second, x.qa, accept(await x.basis(), rb)); assert.equal(final.status, "COMPLETED");
  assert.equal((await x.store.getTask(x.semanticTask, x.qa)).task && (await x.store.listTasks(x.qa)).tasks.find(r => r.obligation_id === "SEMANTIC_REVIEW")!.status, "ASSIGNED");
});
pgTest("v2 PG：撤回后迟到结果只归档，UNKNOWN预算保留且后继规格不能重置预算", async t => {
  const x = await setup(t); const req = await x.request(); const a = await x.work(req, { model_calls: 20, read_calls: 8 });
  await x.store.applyRuntimeResult(randomUUID(), { request_id: req, request_spec_revision: 1, run_id: a.mapping.run_id, input_digest: a.snapshot.input_digest, execution_state: "BLOCKED", consumed: { model_calls: 20, read_calls: 0 }, clarification: "请说明目标阶段" });
  await x.store.clarify(req, x.qa, { ...await x.basis(), answer: "沿用 before 阶段与原件目标" });
  const w = await x.store.claimWork("successor-worker"); assert(w); const snapshot = await x.store.loadRequest(req); assert.equal(snapshot.request_spec_revision, 2);
  await assert.rejects(x.store.reserveRunMapping(w, { ...a.mapping, request_spec_revision: 2, run_id: randomUUID(), frozen_input_digest: snapshot.input_digest, allocation: { model_calls: 6, read_calls: 8 }, limits: { model_calls: 6, reads: 8 }, consumed: { model_calls: 0, read_calls: 0 } }), errorCode("CUMULATIVE_BUDGET_EXHAUSTED"));
  await x.store.withdraw(req, x.qa, { ...await x.basis(), reason: "结束本次调查" });
  assert.equal(await x.store.isDispatchable(w), false);
  const lateResult = result(a.snapshot, a.mapping); lateResult.consumed = { model_calls: 20, read_calls: 0 };
  const late = await x.store.applyRuntimeResult(randomUUID(), lateResult);
  assert.equal(late.archived, true);
});
pgTest("v2 PG：UNKNOWN保留预留额度，重放不重复占用；只读接口不泄漏其他租户", async t => {
  const x = await setup(t); const req = await x.request(); const a = await x.work(req);
  const unknown: RuntimeResult = { request_id: req, request_spec_revision: 1, run_id: a.mapping.run_id, input_digest: a.snapshot.input_digest, execution_state: "UNKNOWN", consumed: { model_calls: 1, read_calls: 0 } };
  const premature = result(a.snapshot, a.mapping); premature.execution_state = "UNKNOWN";
  await assert.rejects(x.store.applyRuntimeResult(randomUUID(), premature), errorCode("RESPONSE_REQUIRES_FINISHED_RUNTIME"));
  const effect = randomUUID(); await x.store.applyRuntimeResult(effect, unknown); await x.store.applyRuntimeResult(effect, unknown);
  const detail = await x.store.getTask(x.evidenceTask, x.qa); assert.deepEqual((detail.case as Record<string, unknown>).budget_reserved, a.mapping.allocation); assert.equal(await x.store.claimWork("unknown-does-not-retry"), null);
  await assert.rejects(x.store.getTask(x.evidenceTask, { ...x.qa, tenant_id: "other" }), errorCode("NOT_FOUND"));
  assert.deepEqual(await x.store.listTasks({ ...x.qa, tenant_id: "other" }), { tasks: [] });
});

pgTest("v2 PG：相同响应不同effect不重开已接受审查，旧claim故障回报不能污染新claim", async t => {
  const x = await setup(t); const req = await x.request(); const a = await x.work(req); const r = result(a.snapshot, a.mapping);
  await x.store.applyRuntimeResult(randomUUID(), r); await x.store.acceptResponse(req, x.qa, accept(await x.basis(), r));
  const before = await x.store.getTask(x.evidenceTask, x.qa); const replay = await x.store.applyRuntimeResult(randomUUID(), r); assert.equal(replay.duplicate_response, true);
  const after = await x.store.getTask(x.evidenceTask, x.qa); assert.deepEqual(after.task, before.task); assert.equal((after.requests as Record<string, unknown>[])[0]!.review_state, "ACCEPTED");
  const second = await x.request(x.semanticTask); const b = await x.work(second); await x.legacy.pool.query("UPDATE egap_review_v2.outbox SET lease_until=now()-interval '1 second' WHERE id=$1", [b.work.id]);
  const newer = await x.store.claimWork(b.work.worker_id); assert(newer); await x.store.failWork(b.work, "OLD_CLAIM_ERROR", true);
  assert.equal(await x.store.isDispatchable(newer), true); assert.equal((await x.store.loadRequest(second)).execution_state, "RUNNING");
});

pgTest("v2 PG：UNKNOWN仅只读对账，未决冷却不重置预算，终态同Run可恢复", async t => {
  const x = await setup(t); const req = await x.request(); const a = await x.work(req);
  await x.store.applyRuntimeResult(randomUUID(), { request_id: req, request_spec_revision: 1, run_id: a.mapping.run_id, input_digest: a.snapshot.input_digest, execution_state: "UNKNOWN", consumed: { model_calls: 1, read_calls: 0 } });
  const reconciliation = await x.store.claimReconciliationWork("read-only-recovery"); assert(reconciliation); assert.equal(await x.store.isDispatchable(reconciliation), false); assert.equal(await x.store.renewWork(reconciliation), true);
  await x.store.failWork(reconciliation, "STILL_UNKNOWN", true); assert.equal(await x.store.claimReconciliationWork("no-hot-loop"), null);
  assert.deepEqual(((await x.store.getTask(x.evidenceTask, x.qa)).case as Record<string, unknown>).budget_reserved, a.mapping.allocation);
  await x.legacy.pool.query("UPDATE egap_review_v2.outbox SET lease_until=now()-interval '1 second' WHERE id=$1", [reconciliation.id]);
  const recovered = await x.store.claimReconciliationWork("recovery-after-cooldown"); assert(recovered);
  await x.store.applyRuntimeResult(randomUUID(), result(a.snapshot, a.mapping));
  assert.equal((await x.store.loadRequest(req)).execution_state, "FINISHED");
  assert.deepEqual(((await x.store.getTask(x.evidenceTask, x.qa)).case as Record<string, unknown>).budget_reserved, { model_calls: 0, read_calls: 0 });
  assert.equal((await x.store.loadRequest(req)).mapping!.run_id, a.mapping.run_id);
  const completed = await x.store.loadRequest(req);
  const lateUnknown = await x.store.applyRuntimeResult(randomUUID(), { request_id: req, request_spec_revision: 1, run_id: a.mapping.run_id, input_digest: a.snapshot.input_digest, execution_state: "UNKNOWN", consumed: { model_calls: 1, read_calls: 0 } });
  assert.equal(lateUnknown.ignored_stale_state, true);
  assert.equal((await x.store.loadRequest(req)).execution_state, "FINISHED");
  assert.deepEqual((await x.store.loadRequest(req)).execution_context, completed.execution_context);
});

pgTest("v2 PG：澄清后继保留旧Run读取/缺口/成本，但不继承当前Run回执权限", async t => {
  const x = await setup(t); const req = await x.request(); const a = await x.work(req); const completedReads = result(a.snapshot, a.mapping).read_receipts!;
  assert.equal(a.snapshot.input.continuation, null);
  await x.store.applyRuntimeResult(randomUUID(), { request_id: req, request_spec_revision: 1, run_id: a.mapping.run_id, input_digest: a.snapshot.input_digest,
    execution_state: "BLOCKED", consumed: { model_calls: 2, read_calls: completedReads.length }, read_receipts: completedReads, clarification: "最终备注关联已采集，但需要确认目标阶段是否 before" });
  const before = await x.store.loadRequest(req);
  assert.equal(before.execution_context.read_receipts!.length, 3); assert.equal(before.execution_context.run_id, a.mapping.run_id);
  assert.equal(before.execution_context.read_receipts![0]!.content, undefined); // 历史上下文只保存绑定元数据，不伪装成新Run读取。
  const answer = "确认 before 阶段；仍需按当前授权目录读取后提交";
  await x.store.clarify(req, x.qa, { ...await x.basis(), answer });
  const successor = await x.store.loadRequest(req);
  assert.deepEqual(successor.input.continuation, { predecessor_run_id: a.mapping.run_id, predecessor_request_spec_revision: 1,
    read_receipt_refs: completedReads.map(receipt => receipt.receipt_ref), remaining_gaps: before.execution_context.remaining_gaps, human_answer: answer });
  assert.deepEqual(successor.execution_context, {}); assert.equal(successor.mapping, null);
  const historical = await x.store.loadRequest(req, 1);
  assert.deepEqual(historical.execution_context, before.execution_context); assert.equal(historical.input_digest, a.snapshot.input_digest);
  assert.deepEqual(historical.mapping!.consumed, { model_calls: 2, read_calls: 3 });
  const next = await x.work(req);
  assert.notEqual(next.mapping.run_id, a.mapping.run_id);
  const detail = await x.store.getTask(x.evidenceTask, x.qa);
  assert.deepEqual((detail.case as Record<string, unknown>).budget_consumed, { model_calls: 2, read_calls: 3 });
  assert.deepEqual((detail.case as Record<string, unknown>).budget_reserved, next.mapping.allocation);
});

pgTest("v2 PG：同租户不同成员case互不泄漏，也不使整个任务列表404", async t => {
  const x = await setup(t); const id = `same-tenant-${randomUUID()}`;
  const qa = { ...x.qa, id: `isolated-${randomUUID()}` }, owner = { ...x.owner, id: `owner-${randomUUID()}` };
  const legacy: ImportCase = { id, tenant_id: x.qa.tenant_id, title: "同租户独立成员测试", candidate_sha: "a".repeat(40), baseline_sha: null, execution_mode: "TEST_ONLY", source_ref: "test", source_sha256: "b".repeat(64), machine_result: {}, limitations: ["TEST_ONLY"], evidence: [], obligations: [] };
  await x.store.seedCase({ legacy, archive: { historical_run_id: x.targets[0]!.historical_run_id, targets: x.targets, materials: x.materials }, actors: [qa, owner], owner_id: owner.id, qa_assignee_id: qa.id });
  assert.deepEqual((await x.store.listTasks(x.qa)).tasks.map(task => task.case_id), [x.id, x.id]);
  assert.deepEqual((await x.store.listTasks(qa)).tasks.map(task => task.case_id), [id, id]);
  assert.deepEqual(await x.store.listTasks({ ...qa, id: "not-a-member" }), { tasks: [] });
  for (const table of ["events", "tasks", "bundles"]) await x.legacy.pool.query(`DELETE FROM egap_review_v2.${table} WHERE case_id=$1`, [id]);
  await x.legacy.pool.query("DELETE FROM egap_review_v2.cases WHERE id=$1", [id]);
});

pgTest("v2 PG：Run事件序单调，迟到响应只归档，已接受终态不被回退", async t => {
  const x = await setup(t); const req = await x.request(); const a = await x.work(req);
  const current = result(a.snapshot, a.mapping); current.runtime_event_seq = 20;
  const effect = randomUUID(); const applied = await x.store.applyRuntimeResult(effect, current);
  await x.store.acceptResponse(req, x.qa, accept(await x.basis(), current));
  const before = await x.store.getTask(x.evidenceTask, x.qa);
  const earlier = result(a.snapshot, a.mapping); earlier.runtime_event_seq = 10; earlier.consumed = { model_calls: 1, read_calls: 0 };
  const archived = await x.store.applyRuntimeResult(randomUUID(), earlier); assert.equal(archived.archived, true);
  const after = await x.store.getTask(x.evidenceTask, x.qa);
  assert.deepEqual(after.task, before.task);
  const request = (after.requests as Record<string, unknown>[])[0]!;
  assert.equal(request.current_response_id, current.response!.response_id); assert.equal(request.review_state, "ACCEPTED");
  assert.equal((request.responses as Record<string, unknown>[]).find(r => r.id === earlier.response!.response_id)!.archived, true);
  assert.equal((await x.store.loadRequest(req)).mapping!.last_runtime_event_seq, 20);
  assert.deepEqual(await x.store.applyRuntimeResult(effect, current), applied);
  await assert.rejects(x.store.applyRuntimeResult(randomUUID(), { ...earlier, runtime_event_seq: 20 }), errorCode("RUNTIME_EVENT_CONFLICT"));
  await x.store.applyRuntimeResult(randomUUID(), { request_id: req, request_spec_revision: 1, run_id: a.mapping.run_id, input_digest: a.snapshot.input_digest,
    execution_state: "BLOCKED", runtime_event_seq: 21, consumed: current.consumed, clarification: "迟到的不完整投影" });
  assert.equal((await x.store.loadRequest(req)).execution_state, "FINISHED");
  assert.deepEqual((await x.store.getTask(x.evidenceTask, x.qa)).task, before.task);
});
