import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { test, type TestContext } from "node:test";
import type { PlanDraft, ReviewRequest } from "@egap/contracts";
import { Runtime } from "./runtime.js";
import { capabilityContract } from "./capability-authority.js";
import { sha, type Json, type Limits, type ReleaseBundle } from "./types.js";

const databaseUrl = process.env["EGAP_VERIFY_TEST_DATABASE_URL"];
const pgTest = (name: string, fn: (t: TestContext) => Promise<void>) => test(name, { skip: !databaseUrl, timeout: 20_000 }, fn);
type ReviewBundle = ReleaseBundle & { review_request: ReviewRequest };
type Reply = Json | Promise<Json> | ((context: Json) => Json);

/**
 * 真实 PostgreSQL + 固定 Provider 响应，用来检验编排/权限/恢复协议。
 * 源码包含显式条件变化，但不执行候选，也不把 stub 的实验设计当成模型能力证据。
 */
function bundle(): ReviewBundle {
  const path = (capabilityContract["entrypoint"] as { path: string }).path;
  const baseline = readFileSync(new URL("../../../" + path, import.meta.url), "utf8");
  const candidate = baseline.replace('instant(acceptance["accepted_at"]) > renewed', 'instant(acceptance["accepted_at"]) > claimed');
  assert.notEqual(candidate, baseline, "源代码变化不能使协议夹具静默成为无变化任务");
  const diff = `--- a/${path}\n+++ b/${path}\n@@ -270 +270 @@\n-            or instant(acceptance["accepted_at"]) > renewed:\n+            or instant(acceptance["accepted_at"]) > claimed:\n`;
  const rule = "本地校准：合同应在续费前接受；保留已确认付款及客户绑定的前提。";
  const goal = "核对合同接受时间上界的变化及必须保留的资格语义";
  return {
    schema_version: 2, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING", input_provenance: "CALIBRATION_SYNTHETIC",
    goal, baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40), catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40),
    requirement_refs: ["R4"], changes: [{ path, diff_material_ref: "diff:commercial.py" }],
    materials: [
      { material_ref: "rule:R4", kind: "REGISTERED_RULE_EXCERPT", title: "本地规则", git_commit: "d".repeat(40), path: "business-source-pack/v1/policies/rules.md", content: rule, sha256: sha(rule) },
      { material_ref: "baseline:commercial.py", kind: "BASELINE_SOURCE", title: "基线", git_commit: "a".repeat(40), path, content: baseline, sha256: sha(baseline) },
      { material_ref: "candidate:commercial.py", kind: "CANDIDATE_SOURCE", title: "合成候选", git_commit: "b".repeat(40), path, content: candidate, sha256: sha(candidate) },
      { material_ref: "diff:commercial.py", kind: "UNTRUSTED_DIFF", title: "真实条件差异", git_commit: "b".repeat(40), path, content: diff, sha256: sha(diff) },
    ],
    catalog: { status: "DRAFT", dispatch_authorized: false,
      requirements: [{ id: "R4", material_ref: "rule:R4", source_ref: "rules" }],
      sources: [{ id: "sut_runtime", kind: "PRODUCT_EXECUTION" }, { id: "rules", kind: "BUSINESS_RULE" }],
      capabilities: [{ id: "probe.commercial_decision", requirement_refs: ["R4"], dispatch_enabled: false }],
      scenarios: [{ id: "commercial_unit", stages: [{ stage_ref: "commercial_unit.evaluate" }] }] },
    planning_contracts: [capabilityContract], gaps: [], current_candidate_coverage: [], evaluation_status: "NOT_RUN",
    review_request: { schema_version: 1, request_id: "review:" + randomUUID(), revision: 1, requester_ref: "operator:integration", owner_ref: "owner:local",
      goal, non_goals: ["不执行业务、不作发布决定"], baseline: { commit: "a".repeat(40), reason: "协议夹具的准确比较基线", source_ref: "git:baseline" },
      candidate: { commit: "b".repeat(40), source_ref: "git:candidate" },
      requirements: [{ id: "R4", kind: "PRESERVE", statement: "保留合同接受时点语义", material_ref: "rule:R4", authority: "LOCAL_CALIBRATION", status: "CONFIRMED_LOCAL", owner_ref: "owner:local" }],
      known_gaps: [], predecessor_run_id: null },
  };
}

function decision(action_kind: string, payload: Json): Json {
  return { action_kind, payload, requirement_refs: ["R4"], hypothesis_refs: ["H:acceptance"], evidence_refs: ["diff:commercial.py"],
    expected_information: "核对接受时间关系所需的证据或可执行计划。", rationale: "依据当前证据缺口选择下一项工作。", hypothesis_updates: [],
    decision_frame: { unresolved_questions: [{ id: "q:acceptance", question: "新上界是否改变晚接受合同的资格？", why_unresolved: "当前只有代码和规则，尚未执行候选。", evidence_refs: ["diff:commercial.py"] }],
      selected_question_id: "q:acceptance", outcome_branches: [
        { observation: "证据支持保留边界", implication: "可按要求形成因素对照实验", next_action: "PLAN_DRAFT" },
        { observation: "规则存在冲突或证据缺失", implication: "先补材料或请求责任人澄清", next_action: "CLARIFY" },
      ] } };
}
function direction(): Json {
  return { ...decision("UPDATE_DIRECTION", { focus: "先核对合同接受时间条件及保留义务" }),
    hypothesis_updates: [{ id: "H:acceptance", statement: "上界改成申请时间可能允许续费后才接受的合同。", status: "OPEN", evidence_refs: ["diff:commercial.py"] }] };
}
const readRule = () => decision("READ", { material_ref: "rule:R4", start_line: 1, end_line: 1 });
const partial = () => decision("PLAN_DRAFT", { draft: { objective: "比较晚接受合同的资格" } });
function complete(): Json {
  const draft: PlanDraft = { objective: "用续费后、申请前才接受合同区分变化影响", requirement_ref: "R4", diff_ref: "diff:commercial.py", changed_fields: ["accepted_at"], claim: "MAY_CHANGE",
    inputs: [{ name: "commercial_source.contracts.0.acceptance.accepted_at", value: "2026-11-05T00:00:00Z" }],
    control_inputs: [{ name: "commercial_source.contracts.0.acceptance.accepted_at", value: "2026-11-01T00:00:00Z" }],
    observation_field: "decision.eligibility", operator: "EQUALS", expected_value: "NEEDS_MORE_EVIDENCE", expected_baseline: "NEEDS_MORE_EVIDENCE", expected_candidate: "ELIGIBLE",
    control_relation: "DIFFERENT", control_reason: "只改变接受时间，其余 fixture 前提相同。", change_reason: "上界由续费改为申请时点，可能放宽两者之间接受的合同。" };
  return { ...decision("PLAN_DRAFT", { draft }), evidence_refs: ["diff:commercial.py", "rule:R4"] };
}
function accept(context: Json): Json {
  const draft = (context["review_cycle"] as Json)["draft"] as Json;
  return decision("ACCEPT_PLAN", { draft_ref: draft["ref"], draft_digest: draft["digest"] });
}

/** Stub 可以故意越过生成语法；真正的协议和动作校验仍必须拒绝无效响应。 */
async function harness(t: TestContext, responses: Reply[], input = bundle(), limits: Limits = { model_calls: 7, reads: 3, no_progress: 3, plan_repairs: 1 }) {
  const requests: Json[] = [];
  const server = createServer(async (req, res) => {
    res.setHeader("content-type", "application/json");
    if (req.url === "/api/tags") { res.end(JSON.stringify({ models: [{ name: "review-cycle-stub", digest: "e".repeat(64) }] })); return; }
    let raw = ""; for await (const chunk of req) raw += String(chunk);
    const request = JSON.parse(raw) as Json; const index = requests.length; requests.push(request);
    const context = JSON.parse(((request["messages"] as Json[])[1]!["content"] as string))["context_view"] as Json;
    const response = responses[index];
    const body = typeof response === "function" ? response(context) : await (response ?? { action_kind: "UNEXPECTED_EXTRA_CALL" });
    res.end(JSON.stringify({ model: "review-cycle-stub", done: true, message: { content: JSON.stringify(body) }, prompt_eval_count: 100 + index, eval_count: 40 + index }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const config = { databaseUrl: databaseUrl!, plannerPrompt: "unused-v1-prompt", model: {
    endpoint: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, name: "review-cycle-stub", digest: "e".repeat(64), timeoutMs: 3000, maxOutputTokens: 2000 } };
  const opened = new Set<Runtime>();
  const open = async () => { const runtime = new Runtime(config); opened.add(runtime); await runtime.store.migrate(); return runtime; };
  const close = async (runtime: Runtime) => { if (opened.delete(runtime)) await runtime.close(); };
  t.after(async () => { for (const runtime of opened) await close(runtime); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); });
  const runtime = await open(); const token = randomUUID() + randomUUID(); const runId = await runtime.acceptRun(input, token, limits);
  return { runtime, runId, token, input, requests, open, close };
}
const command = (projection: Json) => ({ command_id: projection["next_command_id"] as string, based_on_decision_revision: projection["decision_basis_revision"] as number });
async function step(runtime: Runtime, runId: string) {
  const call = await runtime.decide(runId, command(await runtime.projection(runId)));
  assert.equal(call["status"], "COMPLETED");
  const operation = await runtime.submit(runId, call["proposal"]);
  return { call, operation, result: operation["result"] as Json };
}

pgTest("Review循环：影响→读取→部分草案→修正→主图接受，始终没有执行或覆盖", async t => {
  // 三次探索 + 一次修正 + 一次主图收口正好用尽总额，不能借用或重复返还任何池。
  const h = await harness(t, [direction(), readRule(), partial(), complete(), accept], bundle(),
    { model_calls: 5, reads: 3, no_progress: 3, plan_repairs: 1 });
  assert.equal((await step(h.runtime, h.runId)).result["status"], "DIRECTION_UPDATED");
  await step(h.runtime, h.runId);
  const firstDraft = await step(h.runtime, h.runId);
  assert.equal(firstDraft.result["status"], "NEEDS_REVISION");
  assert.ok((firstDraft.result["issues"] as Json[]).some(issue => issue["path"] === "/payload/draft/control_inputs"));
  assert.ok((firstDraft.result["issues"] as Json[]).some(issue => issue["path"] === "/payload/draft/expected_value"));
  const bound = await step(h.runtime, h.runId);
  assert.equal(bound.result["status"], "BOUND_PLAN");
  assert.equal(bound.result["semantic_correctness"], "NOT_ESTABLISHED");
  const beforeAccept = await h.runtime.projection(h.runId);
  assert.equal(beforeAccept["status"], "ACTIVE", "Planner的返回必须让主图重新决定下一步");
  assert.equal((beforeAccept["review_repair"] as Json)["attempts_used"], 1);
  const accepted = await step(h.runtime, h.runId);
  assert.equal((accepted.call["proposal"] as Json)["context_view_ref"], beforeAccept["context_view_ref"],
    "主图收口额度预留不能改变同次决策依据，导致合法接受自我过期");
  assert.equal(accepted.result["status"], "PLAN_ACCEPTED_FOR_REVIEW");
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "PLANNED"); assert.equal(final["stop_reason"], "SCOPED_PLAN_ACCEPTED");
  assert.deepEqual(final["current_candidate_coverage"], []); assert.equal((final["final_eligibility"] as Json)["eligible"], false);
  assert.equal(final["model_calls_used"], 5);
  assert.equal(final["review_close_calls_reserved"], 1);
  assert.equal((final["review_cycle"] as Json)["closure_calls_used"], 1);
  assert.equal((final["review_cycle"] as Json)["decisions_completed"], 5);
  const rows = (await h.runtime.store.pool.query("SELECT proposal,raw_response,usage FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at,id", [h.runId])).rows;
  assert.deepEqual(rows.map(row => row.proposal.action_kind), ["UPDATE_DIRECTION", "READ", "PLAN_DRAFT", "PLAN_DRAFT", "ACCEPT_PLAN"]);
  assert.ok(rows.every(row => row.raw_response && row.usage));
  assert.equal(rows.reduce((sum, row) => sum + row.usage.prompt_tokens, 0), 510);
  const context = JSON.parse(((h.requests[4]!["messages"] as Json[])[1]!["content"] as string))["context_view"] as Json;
  assert.equal((context["review_request"] as Json)["request_id"], h.input.review_request.request_id);
  assert.equal((context["review_cycle"] as Json)["draft"] !== undefined, true);
  assert.equal((context["allowed_actions"] as string[]).includes("READ"), false);
  assert.equal((context["allowed_actions"] as string[]).includes("UPDATE_DIRECTION"), false);
  assert.equal((context["allowed_actions"] as string[]).includes("ACCEPT_PLAN"), true);
  assert.equal(JSON.parse(((h.requests[4]!["messages"] as Json[])[1]!["content"] as string))["execution_limits"]["decision_phase"], "REVIEW_CLOSE");
});

pgTest("Review缺项：冲突要求不能规划，CLARIFY可登记且不发送外部消息", async t => {
  const input = bundle(); input.review_request = { ...input.review_request, requirements: [{ ...input.review_request.requirements[0]!, status: "CONFLICT" }] };
  const h = await harness(t, [direction(), partial(), decision("CLARIFY", { question: "请Owner确认续费与申请两个时点中哪一个是上界。" })], input);
  await step(h.runtime, h.runId);
  const rejected = await step(h.runtime, h.runId);
  assert.equal(rejected.operation["status"], "REJECTED"); assert.equal(rejected.result["error_code"], "REVIEW_ACTION_NOT_AVAILABLE");
  const clarified = await step(h.runtime, h.runId);
  assert.equal(clarified.result["external_message_sent"], false);
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "BLOCKED"); assert.equal(final["stop_reason"], "CLARIFICATION_REQUIRED");
  assert.equal((final["review_cycle"] as Json)["draft"], undefined);
});

pgTest("Review守门：未决问题错引、未读证据和未读适用规则均不能绕过", async t => {
  const wrongQuestion = readRule(); (wrongQuestion["decision_frame"] as Json)["selected_question_id"] = "q:missing";
  const unread = readRule(); ((unread["decision_frame"] as Json)["unresolved_questions"] as Json[])[0]!["evidence_refs"] = ["candidate:commercial.py"];
  const noRule = complete(); noRule["evidence_refs"] = ["diff:commercial.py"];
  const h = await harness(t, [direction(), wrongQuestion, unread, noRule]);
  await step(h.runtime, h.runId);
  for (const expected of ["UNRESOLVED_QUESTION_REFERENCE_INVALID", "EVIDENCE_NOT_OBSERVED", "APPLICABLE_RULE_READ_REQUIRED"]) {
    const rejected = await step(h.runtime, h.runId);
    assert.equal(rejected.operation["status"], "REJECTED"); assert.equal(rejected.result["error_code"], expected);
  }
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["reads_used"], 0); assert.deepEqual(final["read_receipts"], []);
  assert.equal((final["review_cycle"] as Json)["draft"], undefined);
});

pgTest("Review输入身份：同revision内容冲突拒绝；新revision明确链接前驱且不继承进度", async t => {
  const h = await harness(t, [direction(), readRule()]);
  await step(h.runtime, h.runId); await step(h.runtime, h.runId);
  const changed = structuredClone(h.input); changed.review_request = { ...changed.review_request, non_goals: ["新的范围声明"] };
  await assert.rejects(h.runtime.acceptRun(changed, h.token), { message: "REVIEW_REQUEST_REVISION_CONFLICT" });
  const changedMaterial = structuredClone(h.input);
  changedMaterial.materials[0]!.content += " 新的规则文字不能沿用原revision。";
  changedMaterial.materials[0]!.sha256 = sha(changedMaterial.materials[0]!.content);
  await assert.rejects(h.runtime.acceptRun(changedMaterial, h.token), { message: "REVIEW_REQUEST_REVISION_CONFLICT" });
  const next = structuredClone(changed); next.review_request = { ...next.review_request, revision: 2, predecessor_run_id: h.runId };
  const nextId = await h.runtime.acceptRun(next, h.token);
  const projection = await h.runtime.projection(nextId);
  assert.equal((projection["task_identity"] as Json)["predecessor_run_id"], h.runId);
  assert.deepEqual(projection["read_receipts"], []); assert.deepEqual(projection["hypotheses"], []);
  assert.deepEqual((projection["review_cycle"] as Json)["directions"], []);
  const wrongParent = structuredClone(next); wrongParent.review_request = { ...wrongParent.review_request, revision: 3, predecessor_run_id: randomUUID() };
  await assert.rejects(h.runtime.acceptRun(wrongParent, h.token), { message: "REVIEW_PREDECESSOR_MISMATCH" });
});

pgTest("Review新证据：追加READ后不能接受旧草案，必须重新编译", async t => {
  const h = await harness(t, [direction(), readRule(), complete(), decision("READ", { material_ref: "candidate:commercial.py", start_line: 1, end_line: 2 }), accept]);
  await step(h.runtime, h.runId); await step(h.runtime, h.runId);
  assert.equal((await step(h.runtime, h.runId)).result["status"], "BOUND_PLAN");
  await step(h.runtime, h.runId);
  const rejected = await step(h.runtime, h.runId);
  assert.equal(rejected.operation["status"], "REJECTED"); assert.equal(rejected.result["error_code"], "PLAN_BASIS_STALE");
  const final = await h.runtime.projection(h.runId);
  assert.equal((final["review_cycle"] as Json)["accepted_plan_ref"], undefined);
  assert.deepEqual(final["current_candidate_coverage"], []);
});

pgTest("Review恢复：草案跨Runtime重启保留；重复决策/并发提交不重复计费或登记", async t => {
  const h = await harness(t, [direction(), readRule(), complete(), accept]);
  await step(h.runtime, h.runId); await step(h.runtime, h.runId);
  const drafted = await step(h.runtime, h.runId);
  const original = (await h.runtime.store.pool.query("SELECT proposal,result FROM verify_runtime.operations WHERE id=$1", [drafted.operation["operation_id"]])).rows[0];
  const before = await h.runtime.projection(h.runId); const cmd = command(before);
  await h.close(h.runtime); const resumed = await h.open();
  assert.deepEqual((await resumed.projection(h.runId))["review_cycle"], before["review_cycle"]);
  const call = await resumed.decide(h.runId, cmd);
  assert.deepEqual(await resumed.decide(h.runId, cmd), call);
  const admissions = await Promise.all([resumed.submit(h.runId, call["proposal"]), resumed.submit(h.runId, call["proposal"])]);
  assert.deepEqual(admissions[0], admissions[1]);
  const final = await resumed.projection(h.runId);
  assert.equal(final["status"], "PLANNED"); assert.equal(final["model_calls_used"], 4); assert.equal(h.requests.length, 4);
  assert.equal((await resumed.store.pool.query("SELECT count(*)::int AS n FROM verify_runtime.operations WHERE run_id=$1", [h.runId])).rows[0].n, 4);
  assert.deepEqual((await resumed.store.pool.query("SELECT proposal,result FROM verify_runtime.operations WHERE id=$1", [drafted.operation["operation_id"]])).rows[0], original);
});

pgTest("Review修正预算：重复不完整草案耗尽一次修正，原提案与成本保留", async t => {
  const h = await harness(t, [direction(), readRule(), partial(), partial()], bundle(), { model_calls: 5, reads: 1, no_progress: 1, plan_repairs: 1 });
  await step(h.runtime, h.runId); await step(h.runtime, h.runId);
  const first = await step(h.runtime, h.runId);
  assert.equal((await h.runtime.projection(h.runId))["status"], "ACTIVE", "有限修正不应被通用无进展限制抢走");
  const second = await step(h.runtime, h.runId);
  assert.notEqual(first.operation["operation_id"], second.operation["operation_id"]);
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "STOPPED"); assert.equal(final["stop_reason"], "CHECK_REPAIR_LIMIT");
  assert.equal((final["review_repair"] as Json)["attempts_used"], 1);
  assert.equal((final["review_repair"] as Json)["status"], "EXHAUSTED");
  assert.equal(final["model_calls_used"], 4);
  await assert.rejects(h.runtime.decide(h.runId, command(final)), { message: "RUN_NOT_ACTIVE" });
  assert.equal(h.requests.length, 4);
  assert.equal((await h.runtime.store.pool.query("SELECT count(*)::int AS n FROM verify_runtime.operations WHERE run_id=$1 AND proposal->>'action_kind'='PLAN_DRAFT'", [h.runId])).rows[0].n, 2);
});

pgTest("Review主图收口池：探索额度耗尽后可接受已绑定计划，未使用的修正额度不被挪用", async t => {
  const h = await harness(t, [direction(), readRule(), complete(), accept], bundle(),
    { model_calls: 5, reads: 2, no_progress: 3, plan_repairs: 1 });
  await step(h.runtime, h.runId); await step(h.runtime, h.runId);
  assert.equal((await step(h.runtime, h.runId)).result["status"], "BOUND_PLAN");
  const before = await h.runtime.projection(h.runId);
  assert.equal(before["model_calls_used"], 3);
  const closed = await step(h.runtime, h.runId);
  assert.equal(closed.result["status"], "PLAN_ACCEPTED_FOR_REVIEW");
  assert.equal((closed.call["proposal"] as Json)["context_view_ref"], before["context_view_ref"]);
  const context = JSON.parse(((h.requests[3]!["messages"] as Json[])[1]!["content"] as string))["context_view"] as Json;
  assert.ok((context["allowed_actions"] as string[]).includes("ACCEPT_PLAN"));
  assert.equal((context["allowed_actions"] as string[]).includes("READ"), false);
  assert.equal((context["allowed_actions"] as string[]).includes("UPDATE_DIRECTION"), false);
  assert.equal((context["allowed_actions"] as string[]).includes("PLAN_DRAFT"), false);
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "PLANNED"); assert.equal(final["model_calls_used"], 4);
  assert.equal(final["review_repair"], undefined, "没有缺项就不能建立修正消费记录");
  assert.equal(final["review_close_calls_reserved"], 1);
  const events = (await h.runtime.store.pool.query("SELECT payload FROM verify_runtime.events WHERE run_id=$1 AND kind='MODEL_RESERVED' ORDER BY seq", [h.runId])).rows;
  assert.deepEqual(events.map(row => row.payload.budget_pool), ["DISCOVERY", "DISCOVERY", "DISCOVERY", "REVIEW_CLOSE"]);
  assert.equal(h.requests.length, 4);
});

pgTest("Review收口准入：Provider绕过生成限制也不能用收口调用继续READ或更新方向", async t => {
  for (const forbidden of [decision("READ", { material_ref: "candidate:commercial.py", start_line: 1, end_line: 2 }), direction()]) {
    const h = await harness(t, [direction(), readRule(), complete(), forbidden], bundle(),
      { model_calls: 5, reads: 2, no_progress: 3, plan_repairs: 1 });
    await step(h.runtime, h.runId); await step(h.runtime, h.runId); await step(h.runtime, h.runId);
    const rejected = await step(h.runtime, h.runId);
    assert.equal(rejected.operation["status"], "REJECTED");
    assert.equal(rejected.result["error_code"], "REVIEW_ACTION_NOT_AVAILABLE");
    const final = await h.runtime.projection(h.runId);
    assert.equal(final["status"], "STOPPED"); assert.equal(final["stop_reason"], "REVIEW_CLOSE_EXHAUSTED");
    assert.equal(final["reads_used"], 1);
    assert.equal(((final["review_cycle"] as Json)["directions"] as Json[]).length, 1);
    assert.equal(final["review_close_calls_reserved"], 1);
    assert.equal(h.requests.length, 4, "非法Provider输出仍计一次收口成本，不能挪用修正池重选");
  }
});

pgTest("Review无进展：没有新观察不能重复更新方向；新READ后允许重新判断", async t => {
  const updated = direction(); updated["hypothesis_updates"] = [{ id: "H:acceptance", statement: "读到规则后，上界变化的风险仍需独立实验判定。", status: "OPEN", evidence_refs: ["diff:commercial.py", "rule:R4"] }];
  const h = await harness(t, [direction(), direction(), readRule(), updated]);
  await step(h.runtime, h.runId);
  const repeated = await step(h.runtime, h.runId);
  assert.equal(repeated.operation["status"], "REJECTED");
  const afterRepeat = await h.runtime.projection(h.runId);
  assert.equal(((afterRepeat["review_cycle"] as Json)["directions"] as Json[]).length, 1);
  assert.equal(afterRepeat["no_progress"], 1);
  const closedActions = JSON.parse(((h.requests[1]!["messages"] as Json[])[1]!["content"] as string))["context_view"]["allowed_actions"] as string[];
  assert.equal(closedActions.includes("UPDATE_DIRECTION"), false);
  await step(h.runtime, h.runId);
  const changed = await step(h.runtime, h.runId);
  assert.equal(changed.result["status"], "DIRECTION_UPDATED");
  const final = await h.runtime.projection(h.runId);
  assert.equal(((final["review_cycle"] as Json)["directions"] as Json[]).length, 2);
  assert.equal(final["no_progress"], 0);
});

pgTest("Review收口失败：非法响应仍记账，剩余修正额度不能用于重新推理", async t => {
  const h = await harness(t, [direction(), readRule(), complete(), { action_kind: "PASS" }], bundle(),
    { model_calls: 5, reads: 2, no_progress: 3, plan_repairs: 1 });
  await step(h.runtime, h.runId); await step(h.runtime, h.runId); await step(h.runtime, h.runId);
  const cmd = command(await h.runtime.projection(h.runId));
  const invalid = await h.runtime.decide(h.runId, cmd);
  assert.equal(invalid["status"], "INVALID");
  assert.deepEqual(await h.runtime.decide(h.runId, cmd), invalid, "原命令回读已有失败，不能重新派发");
  const after = await h.runtime.projection(h.runId);
  assert.equal(after["model_calls_used"], 4);
  assert.equal(after["review_close_calls_reserved"], 1);
  assert.equal(after["stop_reason"], "REVIEW_CLOSE_EXHAUSTED");
  const calls = (await h.runtime.store.pool.query("SELECT status,raw_response,usage FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at,id", [h.runId])).rows;
  assert.equal(calls[3].status, "INVALID"); assert.ok(calls[3].raw_response); assert.equal(calls[3].usage.output_tokens, 43);
  // 允许Runtime在本次或下次准入时关闭Run；无论何时关闭，都不能消耗修正池再请求Provider。
  try { await h.runtime.decide(h.runId, command(after)); } catch (error) { assert.equal((error as Error).message, "RUN_NOT_ACTIVE"); }
  assert.equal(h.requests.length, 4);
  assert.equal((await h.runtime.projection(h.runId))["status"], "STOPPED");
});

pgTest("Review收口UNKNOWN：请求已发出仍占额度，重启只回读原调用而不自动补发", async t => {
  let release!: (value: Json) => void;
  const waiting = new Promise<Json>(resolve => { release = resolve; });
  const h = await harness(t, [direction(), readRule(), complete(), waiting], bundle(),
    { model_calls: 5, reads: 2, no_progress: 3, plan_repairs: 1 });
  await step(h.runtime, h.runId); await step(h.runtime, h.runId); await step(h.runtime, h.runId);
  const cmd = command(await h.runtime.projection(h.runId));
  const unknown = await h.runtime.decide(h.runId, cmd);
  assert.equal(unknown["status"], "UNKNOWN");
  release({ action_kind: "STOP", payload: { reason: "late response is not a new decision" } });
  await h.close(h.runtime); const resumed = await h.open();
  assert.deepEqual(await resumed.decide(h.runId, cmd), unknown);
  const final = await resumed.projection(h.runId);
  assert.equal(final["status"], "BLOCKED"); assert.equal(final["model_calls_used"], 4);
  assert.equal(final["stop_reason"], "MODEL_EXECUTION_UNCERTAIN");
  assert.equal(final["review_close_calls_reserved"], 1);
  assert.equal((final["review_cycle"] as Json)["closure_calls_used"] ?? 0, 0, "UNKNOWN不是完成的决策，但已发出的收口预留不能返还");
  assert.equal((final["review_cycle"] as Json)["accepted_plan_ref"], undefined);
  await assert.rejects(resumed.decide(h.runId, { command_id: "decision:forbidden-retry", based_on_decision_revision: final["decision_basis_revision"] as number }), { message: "RUN_NOT_ACTIVE" });
  assert.equal(h.requests.length, 4);
  const calls = (await resumed.store.pool.query("SELECT status FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at,id", [h.runId])).rows;
  assert.equal(calls[3].status, "UNKNOWN"); assert.equal(calls.length, 4);
});
