import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { test, type TestContext } from "node:test";
import { Runtime } from "./runtime.js";
import { capabilityContract } from "./capability-authority.js";
import { sha, type Json, type Limits, type ReleaseBundle } from "./types.js";

const databaseUrl = process.env["EGAP_VERIFY_TEST_DATABASE_URL"];
const pgTest = (name: string, fn: (t: TestContext) => Promise<void>): void => {
  test(name, { skip: !databaseUrl, timeout: 20_000 }, fn);
};

/** 协议夹具与真实 PG；本文件的 Provider 是 stub，不能当成真模型规划质量证据。 */
function bundle(): ReleaseBundle {
  const rule = "R1: renewal facts must retain the upstream renewal count. Missing facts do not mean zero.";
  const path = (capabilityContract["entrypoint"] as { path: string }).path;
  const baseline = readFileSync(new URL("../../../" + path, import.meta.url), "utf8");
  // 候选只追加注释，明确这是绑定/恢复协议夹具，不能当成有业务行为变化的发布实验。
  const candidate = baseline + "\n# renewal_count: protocol fixture only\n";
  const diff = `--- a/${path}\n+++ b/${path}\n+# renewal_count: protocol fixture only`;
  return {
    schema_version: 1, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING",
    input_provenance: "CALIBRATION_SYNTHETIC", goal: "Evaluate the changed fact projection.",
    baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40), catalog_commit: "c".repeat(40),
    catalog_basis_commit: "d".repeat(40), requirement_refs: ["R1"],
    changes: [{ path, diff_material_ref: "diff:product.py", candidate_material_ref: "candidate:product.py" }],
    materials: [
      { material_ref: "rule:R1", kind: "REGISTERED_RULE_EXCERPT", title: "R1", git_commit: "d".repeat(40), path: "rules.md", sha256: sha(rule), content: rule },
      { material_ref: "candidate:product.py", kind: "CANDIDATE_SOURCE", title: "candidate", git_commit: "b".repeat(40), path, sha256: sha(candidate), content: candidate },
      { material_ref: "baseline:product.py", kind: "BASELINE_SOURCE", title: "baseline", git_commit: "a".repeat(40), path, sha256: sha(baseline), content: baseline },
      { material_ref: "diff:product.py", kind: "UNTRUSTED_DIFF", title: "diff", git_commit: "b".repeat(40), path, sha256: sha(diff), content: diff },
    ],
    catalog: { status: "DRAFT", dispatch_authorized: false,
      requirements: [{ id: "R1", material_ref: "rule:R1", source_ref: "rules" }],
      capabilities: [{ id: "probe.commercial_decision", kind: "ORACLE", requirement_refs: ["R1"], dispatch_enabled: false }],
      scenarios: [{ id: "commercial_unit", requirement_refs: ["R1"], stages: [{ stage_ref: "commercial_unit.evaluate", depends_on: [] }] }],
      sources: [{ id: "sut_runtime", kind: "PRODUCT_EXECUTION", authority_scope: ["commercial_decision"] },
        { id: "rules", kind: "BUSINESS_RULE", authority_scope: ["R1"] }] },
    planning_contracts: [capabilityContract], gaps: [], current_candidate_coverage: [], evaluation_status: "NOT_RUN",
  };
}

function decision(action_kind: string, payload: Json): Json {
  return { action_kind, payload, requirement_refs: ["R1"], hypothesis_refs: [], evidence_refs: ["diff:product.py"],
    expected_information: "Determine whether the projected count preserves the provided renewal fact.",
    rationale: "The changed projection affects the count delivered to the customer response.",
    hypothesis_updates: [{ id: "H1", statement: "The changed projection may lose the upstream renewal count.",
      status: "OPEN", evidence_refs: ["diff:product.py"] }] };
}
const readRule = () => decision("READ", { material_ref: "rule:R1", start_line: 1, end_line: 1 });
const vagueCheck = () => decision("CHECK_INTENT", { capability_ref: "probe.commercial_decision", scenario_ref: "commercial_unit",
  objective: "Check that the renewal facts are correct.", observation_requirements: ["Collect the projected renewal facts."] });
function completeCheck(): Json {
  const original = vagueCheck();
  (original["payload"] as Json)["check_spec"] = {
    schema_version: 1, stage_ref: "commercial_unit.evaluate",
    binding: { contract_ref: capabilityContract["id"], fixture_ref: (capabilityContract["fixture"] as Json)["id"] },
    inputs: [{ name: "commercial_source.payment.status", value: "UNPAID" }],
    observations: [{ id: "O1", source_ref: "sut_runtime", field: "decision.eligibility" }],
    expectations: [{ observation_ref: "O1", operator: "EQUALS", value: "NEEDS_MORE_EVIDENCE", requirement_ref: "R1" }],
    control: { scenario_ref: "commercial_unit", stage_ref: "commercial_unit.evaluate", inputs: [{ name: "commercial_source.payment.status", value: "PAID" }],
      observation_ref: "O1", expected_relation: "DIFFERENT",
      reason: "Only payment status is overridden; other inputs come from the same trusted fixture." },
    change_relation: { diff_ref: "diff:product.py", changed_fields: ["renewal_count"], input_refs: ["commercial_source.payment.status"],
      output_refs: ["O1"], requirement_ref: "R1", claim: "SHOULD_PRESERVE", expected_baseline: "NEEDS_MORE_EVIDENCE",
      expected_candidate: "NEEDS_MORE_EVIDENCE", reason: "This comment-only protocol fixture predicts preservation, not an established business result." },
  };
  return original;
}

/**
 * 合法但内容不完整的协议压力夹具：普通版本只有数 KB，已能产生数百条缺项。
 * 大版本仍低于 Provider 响应上限，用来验证完整留账后不能装入下一次上下文的退出语义。
 */
function denseIncompleteCheck(large = false): Json {
  const original = vagueCheck();
  (original["payload"] as Json)["check_spec"] = {
    schema_version: 1, stage_ref: "unregistered",
    inputs: Array.from({ length: 32 }, () => ({ name: "", value: large ? "x".repeat(2000) : "" })),
    observations: Array.from({ length: 16 }, () => ({ id: "", source_ref: "", field: "" })),
    expectations: Array.from({ length: 32 }, () => ({ observation_ref: "unregistered", operator: "EQUALS", value: "", requirement_ref: "unregistered" })),
    control: { scenario_ref: "unregistered", stage_ref: "unregistered",
      inputs: Array.from({ length: 32 }, () => ({ name: "", value: "" })),
      observation_ref: "unregistered", expected_relation: "SAME", reason: "" },
  };
  assert.ok(Buffer.byteLength(JSON.stringify(original)) < 100_000, "压力用的原始响应本身仍在接收范围内");
  return original;
}
function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

/** 固定响应序列允许测试有意绕过 Provider grammar，验证最终 Runtime 守门仍有效。 */
async function harness(t: TestContext, responses: (Json | Promise<Json>)[], limits: Limits = {
  model_calls: 3, reads: 2, no_progress: 1, plan_repairs: 1,
}) {
  const requests: Json[] = [];
  const received = responses.map(() => deferred<void>());
  const server = createServer(async (req, res) => {
    res.setHeader("content-type", "application/json");
    if (req.url === "/api/tags") {
      res.end(JSON.stringify({ models: [{ name: "repair-test-model", digest: "e".repeat(64) }] })); return;
    }
    let text = "";
    for await (const chunk of req) text += String(chunk);
    const index = requests.length;
    requests.push(JSON.parse(text) as Json);
    received[index]?.resolve();
    const body = await responses[index];
    res.end(JSON.stringify({ model: "repair-test-model", done: true, message: { content: JSON.stringify(body) },
      prompt_eval_count: 100 + index, eval_count: 40 + index }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const endpoint = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const config = { databaseUrl: databaseUrl!, plannerPrompt: "Propose a bounded evidence-grounded check.",
    model: { endpoint, name: "repair-test-model", digest: "e".repeat(64), timeoutMs: 3000, maxOutputTokens: 1500 } };
  const opened = new Set<Runtime>();
  const open = async () => { const instance = new Runtime(config); opened.add(instance); await instance.store.migrate(); return instance; };
  const close = async (instance: Runtime) => { if (opened.delete(instance)) await instance.close(); };
  t.after(async () => {
    for (const instance of opened) await close(instance);
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  });
  // migrate/accept 失败也应释放自有 Provider socket，不能把一次环境失败变成悬挂测试。
  const runtime = await open();
  const runId = await runtime.acceptRun(bundle(), randomUUID() + randomUUID(), limits);
  return { runtime, runId, requests, received, open, close };
}
function command(p: Json) {
  return { command_id: p["next_command_id"] as string, based_on_decision_revision: p["decision_basis_revision"] as number };
}
async function step(runtime: Runtime, id: string) {
  const call = await runtime.decide(id, command(await runtime.projection(id)));
  assert.equal(call["status"], "COMPLETED");
  const operation = await runtime.submit(id, call["proposal"]);
  return { call, operation };
}
async function rejectedCheck(runtime: Runtime, id: string) {
  await step(runtime, id);
  const rejected = await step(runtime, id);
  assert.equal(rejected.operation["status"], "REJECTED");
  const result = rejected.operation["result"] as Json;
  assert.equal(result["status"], "NEEDS_REVISION");
  assert.equal(result["error_code"], "CHECK_SPEC_INCOMPLETE");
  assert.ok(((result["quality"] as Json)["issues"] as Json[]).length >= 4,
    "一次给出阶段、输入、观察、预期及对照等缺项，不能每次只修一个盲猜错误");
  return rejected;
}

pgTest("CHECK修正：重启、并发重放只消费一次修正，原提案与全部成本不被覆盖", async t => {
  const release = deferred<Json>();
  const h = await harness(t, [readRule(), vagueCheck(), release.promise]);
  const rejected = await rejectedCheck(h.runtime, h.runId);
  const original = (await h.runtime.store.pool.query("SELECT * FROM verify_runtime.operations WHERE id=$1", [rejected.operation["operation_id"]])).rows[0];
  const pending = await h.runtime.projection(h.runId);
  assert.equal(pending["status"], "ACTIVE", "no_progress=1 不能抢走明确授予的一次质量修正");
  assert.equal((pending["check_repair"] as Json)["attempts_used"], 0);
  await h.close(h.runtime);
  const resumed = await h.open();
  const beforeRepair = await resumed.projection(h.runId);
  const cmd = command(beforeRepair);
  const first = resumed.decide(h.runId, cmd);
  await h.received[2]!.promise;
  const duplicate = await resumed.decide(h.runId, cmd);
  assert.ok(["RESERVED", "DISPATCHED"].includes(String(duplicate["status"])));
  release.resolve(completeCheck());
  const finished = await first;
  assert.equal(finished["status"], "COMPLETED");
  assert.equal((finished["proposal"] as Json)["context_view_ref"], beforeRepair["context_view_ref"],
    "修正次数预留属于记账，不得改变 Graph 发起本次调用时看到的决策上下文绑定");
  const repairContext = JSON.parse(((h.requests[2]!["messages"] as Json[])[1]!["content"] as string))["context_view"]["check_repair"] as Json;
  assert.equal(repairContext["root_proposal_id"], (rejected.call["proposal"] as Json)["proposal_id"]);
  assert.deepEqual(repairContext["latest_proposal"], rejected.call["proposal"]);
  assert.equal((repairContext["quality"] as Json)["status"], "NEEDS_REVISION");
  const admissions = await Promise.all([resumed.submit(h.runId, finished["proposal"]), resumed.submit(h.runId, finished["proposal"])]);
  assert.deepEqual(admissions[0], admissions[1]);
  assert.equal((admissions[0]!["result"] as Json)["status"], "PROPOSED_ONLY");
  const quality = (admissions[0]!["result"] as Json)["quality"] as Json;
  assert.equal(quality["status"], "STRUCTURALLY_COMPLETE");
  assert.equal(quality["semantic_correctness"], "NOT_ESTABLISHED");
  assert.equal(quality["dispatch_qualified"], false);
  const final = await resumed.projection(h.runId);
  assert.equal(final["status"], "PLANNED");
  assert.equal(final["model_calls_used"], 3);
  assert.equal((final["check_repair"] as Json)["attempts_used"], 1);
  assert.equal((final["check_repair"] as Json)["status"], "SATISFIED");
  assert.equal((final["check_repair"] as Json)["root_proposal_id"], (rejected.call["proposal"] as Json)["proposal_id"]);
  assert.deepEqual(final["current_candidate_coverage"], []);
  assert.equal((final["final_eligibility"] as Json)["eligible"], false);
  assert.deepEqual((await resumed.store.pool.query("SELECT * FROM verify_runtime.operations WHERE id=$1", [original.id])).rows[0], original);
  const calls = (await resumed.store.pool.query("SELECT raw_response,proposal,usage FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at,id", [h.runId])).rows;
  assert.equal(calls.length, 3);
  assert.equal(calls.reduce((sum, c) => sum + c.usage.prompt_tokens, 0), 303);
  assert.equal(calls.reduce((sum, c) => sum + c.usage.output_tokens, 0), 123);
  assert.ok(calls.every(c => c.raw_response && c.proposal && c.usage.cost_basis === "LOCAL_NO_PROVIDER_BILL"));
  assert.deepEqual(await resumed.decide(h.runId, cmd), finished);
  assert.equal(h.requests.length, 3);
});

pgTest("CHECK修正：仍然笼统的第二提案留下独立记录并耗尽，不能换 proposal_id 重置", async t => {
  const h = await harness(t, [readRule(), vagueCheck(), vagueCheck()]);
  const first = await rejectedCheck(h.runtime, h.runId);
  const second = await step(h.runtime, h.runId);
  assert.equal(second.operation["status"], "REJECTED");
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "STOPPED");
  assert.equal(final["stop_reason"], "CHECK_REPAIR_LIMIT");
  assert.equal((final["check_repair"] as Json)["status"], "EXHAUSTED");
  assert.equal((final["check_repair"] as Json)["attempts_used"], 1);
  assert.notEqual((first.call["proposal"] as Json)["proposal_id"], (second.call["proposal"] as Json)["proposal_id"]);
  assert.equal((await h.runtime.store.pool.query("SELECT count(*)::int AS n FROM verify_runtime.operations WHERE run_id=$1 AND proposal->>'action_kind'='CHECK_INTENT'", [h.runId])).rows[0].n, 2);
  await assert.rejects(h.runtime.decide(h.runId, command(final)));
  assert.equal(h.requests.length, 3);
});

pgTest("CHECK修正：无效模型输出也消费修正，原质量缺项仍保留且不再推理", async t => {
  const h = await harness(t, [readRule(), vagueCheck(), { action_kind: "PASS" }]);
  await rejectedCheck(h.runtime, h.runId);
  const last = await h.runtime.decide(h.runId, command(await h.runtime.projection(h.runId)));
  assert.equal(last["status"], "INVALID");
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["stop_reason"], "CHECK_REPAIR_LIMIT");
  assert.equal((final["check_repair"] as Json)["attempts_used"], 1);
  assert.equal(((final["check_repair"] as Json)["quality"] as Json)["status"], "NEEDS_REVISION");
  assert.equal((final["latest_feedback"] as Json)["error_code"], "MODEL_OUTPUT_INVALID");
  assert.equal(final["model_calls_used"], 3);
  const calls = (await h.runtime.store.pool.query("SELECT status,raw_response,usage FROM verify_runtime.calls WHERE run_id=$1", [h.runId])).rows;
  assert.equal(calls.filter(c => c.status === "INVALID" && c.raw_response && c.usage.output_tokens === 42).length, 1);
});

pgTest("CHECK修正：追加 READ 不重置次数，完成读取留证后按修正上限停止", async t => {
  const h = await harness(t, [readRule(), vagueCheck(), decision("READ", { material_ref: "candidate:product.py", start_line: 1, end_line: 2 })]);
  await rejectedCheck(h.runtime, h.runId);
  const last = await step(h.runtime, h.runId);
  assert.equal(last.operation["status"], "COMPLETED");
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "STOPPED");
  assert.equal(final["stop_reason"], "CHECK_REPAIR_LIMIT");
  assert.equal(final["reads_used"], 2);
  assert.equal((final["read_receipts"] as Json[]).length, 2);
  assert.equal((final["check_repair"] as Json)["attempts_used"], 1);
  assert.equal(((final["check_repair"] as Json)["quality"] as Json)["status"], "NEEDS_REVISION");
});

pgTest("CHECK修正：显式两次额度允许第二次改正，累计成本仍受总预算约束", async t => {
  const h = await harness(t, [readRule(), vagueCheck(), vagueCheck(), completeCheck()],
    { model_calls: 4, reads: 1, no_progress: 1, plan_repairs: 2 });
  await rejectedCheck(h.runtime, h.runId);
  await step(h.runtime, h.runId);
  const intermediate = await h.runtime.projection(h.runId);
  assert.equal(intermediate["status"], "ACTIVE");
  assert.equal((intermediate["check_repair"] as Json)["attempts_used"], 1);
  await step(h.runtime, h.runId);
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "PLANNED");
  assert.equal(final["model_calls_used"], 4);
  assert.equal((final["check_repair"] as Json)["attempts_used"], 2);
});

pgTest("CHECK修正：取消后迟到的完整提案只留账，不恢复任务或返还调用额度", async t => {
  const release = deferred<Json>();
  const h = await harness(t, [readRule(), vagueCheck(), release.promise]);
  await rejectedCheck(h.runtime, h.runId);
  const cmd = command(await h.runtime.projection(h.runId));
  const flight = h.runtime.decide(h.runId, cmd);
  await h.received[2]!.promise;
  await h.runtime.cancel(h.runId);
  release.resolve(completeCheck());
  const late = await flight;
  assert.equal(late["status"], "COMPLETED", "迟到响应仍作为旧模型调用的事实保存");
  await assert.rejects(h.runtime.submit(h.runId, late["proposal"]));
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "CANCELLED");
  assert.equal(final["model_calls_used"], 3);
  assert.equal((final["check_repair"] as Json)["attempts_used"], 1);
  assert.equal((final["check_repair"] as Json)["status"], "PENDING", "取消不冒充成功修正或重新授予机会");
  assert.deepEqual(await h.runtime.decide(h.runId, cmd), late);
  assert.equal(h.requests.length, 3);
  assert.equal((await h.runtime.store.pool.query("SELECT count(*)::int AS n FROM verify_runtime.operations WHERE run_id=$1", [h.runId])).rows[0].n, 2);
});

pgTest("CHECK修正：UNKNOWN保留预留成本并阻塞，重启查询不派发第二次修正", async t => {
  const release = deferred<Json>();
  const h = await harness(t, [readRule(), vagueCheck(), release.promise]);
  await rejectedCheck(h.runtime, h.runId);
  const cmd = command(await h.runtime.projection(h.runId));
  const unknown = await h.runtime.decide(h.runId, cmd);
  assert.equal(unknown["status"], "UNKNOWN");
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "BLOCKED");
  assert.equal(final["model_calls_used"], 3);
  assert.equal((final["check_repair"] as Json)["attempts_used"], 1);
  await h.close(h.runtime);
  const resumed = await h.open();
  assert.deepEqual(await resumed.decide(h.runId, cmd), unknown);
  assert.equal((await resumed.projection(h.runId))["status"], "BLOCKED");
  assert.equal(h.requests.length, 3);
  release.resolve(completeCheck());
});

pgTest("CHECK修正：保留额度不能被探索读取消耗；零修正额度不暗中追加模型调用", async t => {
  await t.test("discovery cannot borrow reserved repair", async child => {
    const h = await harness(child, [readRule(), decision("READ", { material_ref: "candidate:product.py", start_line: 1, end_line: 2 })]);
    await step(h.runtime, h.runId);
    await step(h.runtime, h.runId);
    const before = await h.runtime.projection(h.runId);
    await h.runtime.decide(h.runId, command(before));
    assert.equal((await h.runtime.projection(h.runId))["status"], "STOPPED");
    assert.equal(h.requests.length, 2);
  });
  await t.test("no repair grant", async child => {
    const h = await harness(child, [readRule(), vagueCheck()], { model_calls: 2, reads: 1, no_progress: 1, plan_repairs: 0 });
    await rejectedCheck(h.runtime, h.runId);
    const final = await h.runtime.projection(h.runId);
    assert.equal(final["status"], "STOPPED");
    assert.equal(final["stop_reason"], "CHECK_REPAIR_LIMIT");
    assert.equal((final["check_repair"] as Json)["attempts_used"], 0);
    assert.equal(h.requests.length, 2);
  });
});

pgTest("CHECK修正：数百条缺项只投影一次，完整原提案与反馈保留后仍能修正", async t => {
  const h = await harness(t, [readRule(), denseIncompleteCheck(), completeCheck()]);
  const rejected = await rejectedCheck(h.runtime, h.runId);
  const before = await h.runtime.projection(h.runId);
  const issues = ((before["check_repair"] as Json)["quality"] as Json)["issues"] as Json[];
  assert.ok(issues.length >= 300);
  const operationBefore = await h.runtime.operation(h.runId, rejected.operation["operation_id"] as string);
  await step(h.runtime, h.runId);
  const request = h.requests[2]!;
  const context = JSON.parse(((request["messages"] as Json[])[1]!["content"] as string))["context_view"] as Json;
  const qualityView = ((context["check_repair"] as Json)["quality"] as Json);
  const messages = qualityView["issue_messages"] as Json;
  assert.deepEqual(qualityView["issue_columns"], ["code", "path", "message_ref"]);
  const expanded = (qualityView["issues"] as string[][]).map(([code, path, messageRef]) => ({ code, path,
    message: messages[messageRef!] }));
  assert.equal(qualityView["issue_encoding"], "CODE_PATH_WITH_SHARED_MESSAGES");
  assert.deepEqual(expanded, issues,
    "消息字典展开后每项code/path/message都与原始账本相同，不能截断或覆盖不同说明");
  assert.deepEqual((context["check_repair"] as Json)["latest_proposal"], rejected.call["proposal"],
    "压缩重复说明不能改写原始模型提案");
  assert.equal((context["latest_feedback"] as Json)["quality"], undefined);
  assert.equal((context["latest_feedback"] as Json)["rejected_action"], undefined);
  assert.equal((await h.runtime.projection(h.runId))["status"], "PLANNED");
  assert.deepEqual(await h.runtime.operation(h.runId, rejected.operation["operation_id"] as string), operationBefore);
});

pgTest("CHECK修正：超出上下文上限时持久BLOCKED，不消耗修正次数且原提案和费用完整", async t => {
  const h = await harness(t, [readRule(), denseIncompleteCheck(true)]);
  const rejected = await rejectedCheck(h.runtime, h.runId);
  const initial = await h.runtime.projection(h.runId);
  const originalRepair = initial["check_repair"];
  const operationBefore = await h.runtime.operation(h.runId, rejected.operation["operation_id"] as string);
  const callsBefore = (await h.runtime.store.pool.query(
    "SELECT id,status,raw_response,proposal,usage FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at,id", [h.runId])).rows;
  const cmd = command(initial);
  const blocked = await h.runtime.decide(h.runId, cmd);
  assert.equal(blocked["status"], "BLOCKED");
  assert.equal(blocked["reason"], "CONTEXT_TOO_LARGE");
  const final = await h.runtime.projection(h.runId);
  assert.equal(final["status"], "BLOCKED");
  assert.equal(final["stop_reason"], "CONTEXT_TOO_LARGE");
  assert.equal(final["model_calls_used"], 2, "未登记/派发第三个请求，不把超限当成模型调用收费");
  assert.equal((final["check_repair"] as Json)["attempts_used"], 0);
  assert.deepEqual(final["check_repair"], originalRepair, "超限不能丢弃未修正的规格及全部缺项");
  assert.deepEqual(await h.runtime.operation(h.runId, rejected.operation["operation_id"] as string), operationBefore);
  const callsAfter = (await h.runtime.store.pool.query(
    "SELECT id,status,raw_response,proposal,usage FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at,id", [h.runId])).rows;
  assert.deepEqual(callsAfter, callsBefore);
  assert.equal(callsAfter.reduce((sum, call) => sum + call.usage.prompt_tokens, 0), 201);
  assert.equal(callsAfter.reduce((sum, call) => sum + call.usage.output_tokens, 0), 81);
  assert.ok(callsAfter.every(call => call.raw_response && call.proposal));
  await assert.rejects(h.runtime.decide(h.runId, cmd), { message: "RUN_NOT_ACTIVE" });
  await h.close(h.runtime);
  const resumed = await h.open();
  assert.equal((await resumed.projection(h.runId))["stop_reason"], "CONTEXT_TOO_LARGE");
  await assert.rejects(resumed.decide(h.runId, cmd), { message: "RUN_NOT_ACTIVE" });
  assert.equal(h.requests.length, 2, "重复命令与重启均不重发无法容纳的请求");
});
