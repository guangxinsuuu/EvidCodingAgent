import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { test, type TestContext } from "node:test";
import type { PoolClient } from "pg";
import { Runtime } from "./runtime.js";
import { createVerifyServer } from "./server.js";
import { sha, type Json, type ReleaseBundle, type RuntimeConfig } from "./types.js";

const DATABASE_URL = process.env["EGAP_VERIFY_TEST_DATABASE_URL"];
const pgTest = (name: string, fn: (t: TestContext) => Promise<void>): void => {
  test(name, { skip: !DATABASE_URL, timeout: 20_000 }, fn);
};

/**
 * 这是合成协议/恢复夹具，不是业务发布通过证明；HTTP stub 只模拟 Provider 回执。
 * 真模型质量另由 live 演示验证，以下所有 Runtime 状态确实写入 PostgreSQL。
 */
function bundle(): ReleaseBundle {
  const source = "def decide(facts):\n    # 业务事实必须保留。\n    return facts\n";
  const rule = "R1: 续费事实必须由登记来源提供。\n缺证据不代表无续费。\n只提出检查，不签发PASS。\n";
  const patch = "--- a/product.py\n+++ b/product.py\n@@ -1 +1 @@\n-return None\n+return facts\n";
  return {
    schema_version: 1, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING",
    input_provenance: "CALIBRATION_SYNTHETIC", goal: "检查合成候选变化是否保留续费事实。",
    baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40),
    catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40), requirement_refs: ["R1"],
    changes: [{ path: "product.py", status: "M", category: "SOURCE", analysis_status: "MATERIAL_AVAILABLE",
      candidate_material_ref: "candidate:product.py", diff_material_ref: "diff:product.py" }],
    materials: [
      { material_ref: "rule:R1", kind: "REGISTERED_RULE_EXCERPT", title: "本地校准R1", git_commit: "d".repeat(40), path: "rules.md", sha256: sha(rule), content: rule },
      { material_ref: "candidate:product.py", kind: "CANDIDATE_SOURCE", title: "候选源码", git_commit: "b".repeat(40), path: "product.py", sha256: sha(source), content: source },
      { material_ref: "diff:product.py", kind: "UNTRUSTED_DIFF", title: "git格式的合成差异", git_commit: "b".repeat(40), path: "product.py", sha256: sha(patch), content: patch },
    ],
    catalog: { status: "DRAFT", dispatch_authorized: false,
      requirements: [{ id: "R1", section: "R1", source_ref: "business_rules", material_ref: "rule:R1", review_status: "LOCAL_CALIBRATION_PENDING_USER_REVIEW" }],
      capabilities: [{ id: "cap:release-check", name: "合成只读检查能力", kind: "ORACLE", requirement_refs: ["R1"], dispatch_enabled: false }],
      scenarios: [{ id: "scenario:renewal", requirement_refs: ["R1"] }] },
    gaps: ["LOCAL_SYNTHETIC_ONLY"], current_candidate_coverage: [], evaluation_status: "NOT_RUN",
  };
}

function decision(action_kind = "READ", payload: Json = { material_ref: "rule:R1", start_line: 1, end_line: 2 }): Json {
  return {
    action_kind, payload, requirement_refs: ["R1"], hypothesis_refs: [], evidence_refs: ["diff:product.py"],
    expected_information: "确定当前变更需要验证的业务事实边界。",
    rationale: "变化触及事实传递，先读适用规则，再提出可核验的检查。",
    hypothesis_updates: [{ id: "H1", statement: "候选可能遗漏续费事实。", status: "OPEN", evidence_refs: ["diff:product.py"] }],
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

/** 不用 setTimeout 猜测网络竞态；测试在 Provider 确实收到请求后再并发/取消。 */
async function provider(t: TestContext, body: () => Json | Promise<Json>) {
  const entered = deferred<void>();
  const requests: Json[] = [];
  const requestTexts: string[] = [];
  const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
    if (req.method === "GET" && req.url === "/api/tags") {
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ models: [{ name: "local-test-model", digest: "e".repeat(64) }] }));
      return;
    }
    if (req.method !== "POST" || req.url !== "/api/chat") {
      res.statusCode = 404; res.end(); return;
    }
    let raw = "";
    for await (const chunk of req) raw += String(chunk);
    requestTexts.push(raw);
    requests.push(JSON.parse(raw) as Json);
    entered.resolve();
    try {
      const response = await body();
      if (!res.destroyed) {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ model: "local-test-model", done: true,
          message: { role: "assistant", content: JSON.stringify(response) },
          prompt_eval_count: 120, eval_count: 50 }));
      }
    } catch {
      if (!res.destroyed) { res.statusCode = 500; res.end("provider test failure"); }
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
  return { endpoint: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, requests, requestTexts, entered };
}

function config(endpoint: string, timeoutMs = 3000): RuntimeConfig {
  return { databaseUrl: DATABASE_URL!, plannerPrompt: "Return one evidence-grounded local planning action.",
    model: { endpoint, name: "local-test-model", digest: "e".repeat(64), timeoutMs, maxOutputTokens: 1024 } };
}

async function runtime(t: TestContext, endpoint: string, timeoutMs = 3000): Promise<Runtime> {
  const instance = new Runtime(config(endpoint, timeoutMs));
  await instance.store.migrate();
  // 重启测试显式关闭旧实例；after 钩子只回收仍然打开的连接池。
  openRuntimes.add(instance);
  t.after(async () => { await closeRuntime(instance); });
  return instance;
}

const openRuntimes = new WeakSet<Runtime>();
async function closeRuntime(instance: Runtime): Promise<void> {
  if (openRuntimes.delete(instance)) await instance.close();
}

async function accept(instance: Runtime): Promise<string> {
  return instance.acceptRun(bundle(), randomUUID() + randomUUID(), { model_calls: 5, reads: 5, no_progress: 3 });
}

function command(projection: Json): { command_id: string; based_on_decision_revision: number } {
  return { command_id: projection["next_command_id"] as string,
    based_on_decision_revision: projection["decision_basis_revision"] as number };
}

async function propose(instance: Runtime, runId: string): Promise<Json> {
  const call = await instance.decide(runId, command(await instance.projection(runId)));
  assert.equal(call["status"], "COMPLETED");
  assert.ok(call["proposal"]);
  return call["proposal"] as Json;
}

pgTest("PG：并发重复命令只调用一次；重启恢复已完成提案，READ重放不重复收费", async (t) => {
  const release = deferred<Json>();
  const stub = await provider(t, () => release.promise);
  const first = await runtime(t, stub.endpoint);
  const runId = await accept(first);
  const before = await first.projection(runId);
  assert.equal(((before["budgets"] as Json)["pools"] as Json)["CODING"], 0,
    "本期只读DISCOVERY无需借用编码预算");
  const cmd = command(before);
  const inFlight = first.decide(runId, cmd);
  await stub.entered.promise;
  const duplicate = await first.decide(runId, cmd);
  assert.ok(["PENDING", "RESERVED", "DISPATCHED"].includes(String(duplicate["status"])));
  release.resolve(decision());
  const done = await inFlight;
  assert.equal(done["status"], "COMPLETED");
  assert.equal(stub.requests.length, 1);
  const afterCall = await first.projection(runId);
  assert.equal(afterCall["decision_basis_revision"], before["decision_basis_revision"], "调用记账不能使自身提案过期");
  await closeRuntime(first);
  const restarted = await runtime(t, stub.endpoint);
  const projection = await restarted.projection(runId);
  assert.deepEqual((projection["pending_decision"] as Json)["proposal"], done["proposal"]);
  const recovered = await restarted.decide(runId, cmd);
  assert.deepEqual(recovered, done);
  assert.equal(stub.requests.length, 1);
  const operation = await restarted.submit(runId, done["proposal"]);
  assert.equal(operation["status"], "COMPLETED");
  const replay = await restarted.submit(runId, done["proposal"]);
  assert.equal(replay["operation_id"], operation["operation_id"]);
  const rows = await restarted.store.pool.query("SELECT state FROM verify_runtime.runs WHERE id=$1", [runId]);
  assert.equal(rows.rows[0].state.model_calls_used, 1);
  assert.equal(rows.rows[0].state.reads_used, 1);
  const calls = await restarted.store.pool.query("SELECT count(*)::int AS n FROM verify_runtime.calls WHERE run_id=$1", [runId]);
  const operations = await restarted.store.pool.query("SELECT count(*)::int AS n FROM verify_runtime.operations WHERE run_id=$1", [runId]);
  assert.equal(calls.rows[0].n, 1);
  assert.equal(operations.rows[0].n, 1);
});

pgTest("PG：模型超时保持UNKNOWN，重启与重复命令不再次派发", async (t) => {
  const pending = deferred<Json>();
  const stub = await provider(t, () => pending.promise);
  const first = await runtime(t, stub.endpoint, 150);
  const runId = await accept(first);
  const cmd = command(await first.projection(runId));
  const unknown = await first.decide(runId, cmd);
  assert.equal(unknown["status"], "UNKNOWN");
  assert.equal(stub.requests.length, 1);
  await closeRuntime(first);
  const restarted = await runtime(t, stub.endpoint, 150);
  const replay = await restarted.decide(runId, cmd);
  assert.equal(replay["status"], "UNKNOWN");
  assert.equal(stub.requests.length, 1);
  const saved = await restarted.store.pool.query("SELECT state FROM verify_runtime.runs WHERE id=$1", [runId]);
  assert.equal(saved.rows[0].state.model_calls_used, 1, "未知调用保留预算占用");
  assert.notEqual(saved.rows[0].state.status, "ACTIVE");
  pending.resolve(decision());
});

pgTest("PG：取消后的动作不派发；已在途模型回执不能恢复取消任务", async (t) => {
  const release = deferred<Json>();
  const stub = await provider(t, () => release.promise);
  const instance = await runtime(t, stub.endpoint);
  const cancelled = await accept(instance);
  const commandBeforeCancel = command(await instance.projection(cancelled));
  await instance.cancel(cancelled);
  await assert.rejects(instance.decide(cancelled, commandBeforeCancel));
  assert.equal(stub.requests.length, 0);

  const inFlightRun = await accept(instance);
  const flight = instance.decide(inFlightRun, command(await instance.projection(inFlightRun)));
  await stub.entered.promise;
  await instance.cancel(inFlightRun);
  release.resolve(decision());
  await flight;
  assert.equal((await instance.projection(inFlightRun))["status"], "CANCELLED");
  const rows = await instance.store.pool.query("SELECT count(*)::int AS n FROM verify_runtime.operations WHERE run_id=$1", [inFlightRun]);
  assert.equal(rows.rows[0].n, 0);
});

pgTest("PG：未知材料、伪造证据、越界READ及未读取就CHECK均留下拒绝记录", async (t) => {
  const cases: [string, Json][] = [
    ["unknown_material", decision("READ", { material_ref: "secret:credentials", start_line: 1, end_line: 2 })],
    ["fabricated_evidence", { ...decision(), evidence_refs: ["receipt:never-observed"] }],
    ["unscoped_requirement", { ...decision(), requirement_refs: ["R999"] }],
    ["out_of_range", decision("READ", { material_ref: "rule:R1", start_line: 2, end_line: 500 })],
    ["unread_check", decision("CHECK_INTENT", { capability_ref: "cap:release-check", scenario_ref: "scenario:renewal",
      objective: "检查续费事实。", observation_requirements: ["原生续费事件"] })],
  ];
  for (const [label, body] of cases) {
    await t.test(label, async (child) => {
      const stub = await provider(child, () => body);
      const instance = await runtime(child, stub.endpoint);
      const runId = await accept(instance);
      const proposal = await propose(instance, runId);
      const result = await instance.submit(runId, proposal);
      assert.equal(result["status"], "REJECTED");
      assert.equal((await instance.projection(runId))["decision_basis_revision"], 2);
      const record = await instance.store.pool.query("SELECT state FROM verify_runtime.runs WHERE id=$1", [runId]);
      assert.equal(record.rows[0].state.reads_used, 0);
    });
  }
});

pgTest("PG：客户端不能篡改模型提案；Final能力不足返回明确缺口而非PASS", async (t) => {
  let body = decision();
  const stub = await provider(t, () => body);
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const read = await propose(instance, runId);
  await assert.rejects(instance.submit(runId, { ...read, based_on_decision_revision: -1 }));
  await assert.rejects(instance.submit(runId, { ...read, rationale: "caller rewrote trusted model result" }));
  const result = await instance.submit(runId, read);
  assert.equal(result["status"], "COMPLETED");
  body = decision("REQUEST_FINAL", {});
  const finalProposal = await propose(instance, runId);
  const final = await instance.submit(runId, finalProposal);
  assert.notEqual(final["status"], "PASS");
  assert.match(JSON.stringify(final), /FINAL|NOT_IMPLEMENTED|GAP|UNAVAILABLE/i);
  const projection = await instance.projection(runId);
  assert.deepEqual(projection["current_candidate_coverage"], []);
});

pgTest("PG：相同READ的新提案复用材料，重放该提案仍幂等且不重复消耗读取额度", async (t) => {
  let body = decision();
  const stub = await provider(t, () => body);
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const first = await instance.submit(runId, await propose(instance, runId));
  assert.equal(first["status"], "COMPLETED");
  const readRef = (first["result"] as Json)["receipt_ref"] as string;
  body = { ...decision(), evidence_refs: [readRef], hypothesis_refs: ["H1"],
    hypothesis_updates: [{ id: "H2", statement: "登记规则要求区分缺少证据与无续费事实。",
      status: "SUPPORTED", evidence_refs: [readRef] }] };
  const secondProposal = await propose(instance, runId);
  const second = await instance.submit(runId, secondProposal);
  const replay = await instance.submit(runId, secondProposal);
  assert.equal(replay["operation_id"], second["operation_id"]);
  assert.equal(stub.requests.length, 2);
  const rows = await instance.store.pool.query("SELECT state FROM verify_runtime.runs WHERE id=$1", [runId]);
  assert.equal(rows.rows[0].state.model_calls_used, 2);
  assert.equal(rows.rows[0].state.reads_used, 1);
  assert.equal(rows.rows[0].state.read_receipts.length, 1);
  assert.equal(rows.rows[0].state.no_progress, 1, "命中缓存不能假装取得新证据");
  assert.ok(rows.rows[0].state.hypotheses.some((hypothesis: Json) => hypothesis["id"] === "H2"),
    "缓存动作仍需原子接受同一决策依据上的合法假设更新");
});

/**
 * 在真实事务提交后暂停调用栈，模拟进程在“登记完成→派发”间失去执行机会。
 * 数据/锁/取消仍由实际 PostgreSQL 和 Runtime 完成；不替换 Store 或伪造成功回执。
 */
function holdCommittedTransaction(instance: Runtime, predicate: (value: unknown) => boolean) {
  const committed = deferred<void>();
  const resume = deferred<void>();
  const transaction = instance.store.transaction.bind(instance.store);
  let held = false;
  instance.store.transaction = async <T>(fn: (client: PoolClient) => Promise<T>): Promise<T> => {
    const result = await transaction(fn);
    if (!held && predicate(result)) {
      held = true;
      committed.resolve();
      await resume.promise;
    }
    return result;
  };
  return { committed, resume };
}

pgTest("PG：MODEL已预留但尚未派发时取消，outbox被撤销且Provider零请求", async (t) => {
  const stub = await provider(t, () => decision());
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const cmd = command(await instance.projection(runId));
  const barrier = holdCommittedTransaction(instance,
    (value) => value !== null && typeof value === "object" && (value as Json)["status"] === "RESERVED");
  const deciding = instance.decide(runId, cmd);
  await barrier.committed.promise;
  await instance.cancel(runId);
  barrier.resume.resolve();
  const result = await deciding;
  assert.equal(result["status"], "CANCELLED");
  assert.equal(stub.requests.length, 0);
  const outbox = await instance.store.pool.query("SELECT status FROM verify_runtime.outbox WHERE run_id=$1", [runId]);
  assert.deepEqual(outbox.rows.map((row) => row.status), ["CANCELLED"]);
});

pgTest("PG：READ已消费提案但尚未读取时取消，不能生成新的材料回执", async (t) => {
  const stub = await provider(t, () => decision());
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const proposal = await propose(instance, runId);
  const barrier = holdCommittedTransaction(instance, (value) => typeof value === "string");
  const submitting = instance.submit(runId, proposal);
  await barrier.committed.promise;
  await instance.cancel(runId);
  barrier.resume.resolve();
  const result = await submitting;
  assert.equal(result["status"], "CANCELLED");
  const projection = await instance.projection(runId);
  assert.equal(projection["status"], "CANCELLED");
  assert.deepEqual(projection["read_receipts"], []);
  assert.deepEqual(projection["current_candidate_coverage"], []);
});

pgTest("PG：连续不合法模型响应有界停止，保留真实响应与调用成本占用", async (t) => {
  const stub = await provider(t, () => ({ ...decision(), action_kind: "PASS" }));
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const result = await instance.decide(runId, command(await instance.projection(runId)));
    assert.equal(result["status"], "INVALID");
  }
  const stopped = await instance.projection(runId);
  assert.equal(stopped["status"], "STOPPED");
  assert.equal(stopped["stop_reason"], "NO_PROGRESS_LIMIT");
  await assert.rejects(instance.decide(runId, command(stopped)));
  assert.equal(stub.requests.length, 3);
  const rows = await instance.store.pool.query("SELECT status,raw_response FROM verify_runtime.calls WHERE run_id=$1", [runId]);
  assert.equal(rows.rows.length, 3);
  assert.ok(rows.rows.every((row) => row.status === "INVALID" && row.raw_response.length > 0));
  assert.equal(stopped["model_calls_used"], 3);
  assert.deepEqual(stopped["operations"], []);
});

pgTest("PG：冻结提案返回后权威依据已改变，原提案留下STALE_DECISION拒绝而不能读取", async (t) => {
  const stub = await provider(t, () => decision());
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const proposal = await propose(instance, runId);
  // 故障注入表示后续版本中的外部条件更新；只修改本测试唯一run的权威revision。
  await instance.store.transaction(async (client) => {
    const row = await instance.store.lock(client, runId);
    row.state.decision_basis_revision += 1;
    await instance.store.save(client, row.state);
  });
  const rejected = await instance.submit(runId, proposal);
  assert.equal(rejected["status"], "REJECTED");
  assert.equal((rejected["result"] as Json)["error_code"], "STALE_DECISION");
  assert.deepEqual((await instance.projection(runId))["read_receipts"], []);
});

pgTest("PG：过期扫描持有旧快照时完成回执先提交，迟到UNKNOWN不能降级已完成调用", async (t) => {
  const stub = await provider(t, () => decision());
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const completed = await instance.decide(runId, command(await instance.projection(runId)));
  assert.equal(completed["status"], "COMPLETED");
  const before = await instance.projection(runId);
  // reconcile 的过期 SELECT 与后续事务之间允许其他事务完成调用。
  // 测试直接递交该旧扫描结果；私有入口仅此测试使用，不增加生产 HTTP 权限。
  const reconciliation = instance as unknown as {
    markUnknown(id: string, callId: string, reason: string): Promise<void>;
  };
  await reconciliation.markUnknown(runId, completed["model_call_id"] as string, "STALE_TEST_SCAN");
  const after = await instance.projection(runId);
  assert.equal(after["status"], "ACTIVE");
  assert.equal(after["event_seq"], before["event_seq"], "无状态变化的旧扫描不记虚假UNKNOWN事件");
  assert.deepEqual(after["pending_decision"], before["pending_decision"]);
  assert.deepEqual(after["model_calls"], before["model_calls"]);
  assert.equal(stub.requests.length, 1);
});

pgTest("PG+HTTP：run token隔离，规划器不能导入Run、扩大权限或伪造执行回执", async (t) => {
  const stub = await provider(t, () => decision());
  const instance = await runtime(t, stub.endpoint);
  const tokenA = randomUUID() + randomUUID(), tokenB = randomUUID() + randomUUID();
  const runA = await instance.acceptRun(bundle(), tokenA);
  const runB = await instance.acceptRun(bundle(), tokenB);
  const app = createVerifyServer(instance);
  t.after(async () => { await app.close(); });
  const headersA = { authorization: `Bearer ${tokenA}` };
  const headersB = { authorization: `Bearer ${tokenB}` };
  assert.equal((await app.inject({ method: "GET", url: `/runs/${runA}` })).statusCode, 403);
  assert.equal((await app.inject({ method: "GET", url: `/runs/${runA}`, headers: headersB })).statusCode, 403);
  assert.equal((await app.inject({ method: "GET", url: `/runs/${runB}`, headers: headersA })).statusCode, 403);
  const view = await app.inject({ method: "GET", url: `/runs/${runA}`, headers: headersA });
  assert.equal(view.statusCode, 200);
  const cmd = command(view.json() as Json);
  const denied = await app.inject({ method: "POST", url: `/runs/${runA}/decide`, headers: headersB, payload: cmd });
  assert.equal(denied.statusCode, 403);
  const extraAuthority = await app.inject({ method: "POST", url: `/runs/${runA}/decide`, headers: headersA,
    payload: { ...cmd, actor: "admin-secret-must-not-return", model_endpoint: "https://external.example" } });
  assert.equal(extraAuthority.statusCode, 400);
  assert.equal(extraAuthority.body.includes("admin-secret-must-not-return"), false);
  for (const url of ["/runs", `/runs/${runA}/receipts`, `/runs/${runA}/tools/execute`, `/runs/${runA}/budget`]) {
    const deniedRoute = await app.inject({ method: "POST", url, headers: headersA,
      payload: { result: "PASS", authority: "operator", budget: 9999 } });
    assert.equal(deniedRoute.statusCode, 404, `规划器不应存在写入入口 ${url}`);
  }
  assert.equal(stub.requests.length, 0, "所有拒绝入口都不能调用模型");
  const call = await app.inject({ method: "POST", url: `/runs/${runA}/decide`, headers: headersA, payload: cmd });
  assert.equal(call.statusCode, 200);
  const invalidSchema = await app.inject({ method: "POST", url: `/runs/${runA}/proposals`, headers: headersA,
    payload: { ...call.json().proposal, requirement_refs: ["private-sensitive-marker", "private-sensitive-marker"] } });
  assert.equal(invalidSchema.statusCode, 400);
  assert.deepEqual(invalidSchema.json(), { error_code: "INVALID_DECISION_PROPOSAL" },
    "内部用于模型纠错的validation_issues不能扩散到公开HTTP错误");
  assert.equal(invalidSchema.body.includes("private-sensitive-marker"), false);
  const operation = await app.inject({ method: "POST", url: `/runs/${runA}/proposals`, headers: headersA,
    payload: call.json().proposal });
  assert.equal(operation.statusCode, 200);
  const opId = operation.json().operation_id as string;
  // 生成schema会随已读窗口收窄；HTTP重放仍按原proposal身份找原操作，不能误判为新读。
  const replay = await app.inject({ method: "POST", url: `/runs/${runA}/proposals`, headers: headersA,
    payload: call.json().proposal });
  assert.equal(replay.statusCode, 200);
  assert.equal(replay.json().operation_id, opId);
  assert.equal((await instance.projection(runA))["reads_used"], 1);
  assert.equal((await app.inject({ method: "GET", url: `/runs/${runA}/operations/${opId}`, headers: headersA })).statusCode, 200);
  assert.equal((await app.inject({ method: "GET", url: `/runs/${runB}/operations/${opId}`, headers: headersB })).statusCode, 404,
    "操作查找必须同时绑定run，不能仅凭操作UUID跨run读取");
  assert.equal(stub.requests.length, 1);
});

pgTest("PG：原始模型wire往返持久化后逐字节一致，动作选择字段在所有生成分支中仍然最先", async (t) => {
  const stub = await provider(t, () => decision());
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const call = await instance.decide(runId, command(await instance.projection(runId)));
  assert.equal(call["status"], "COMPLETED");
  const row = (await instance.store.pool.query<{ request: Json; request_text: string }>(
    "SELECT request,request_text FROM verify_runtime.calls WHERE id=$1", [call["model_call_id"]],
  )).rows[0]!;
  // jsonb 供检索而非wire归档：它不保留对象键顺序；native grammar会受该顺序影响。
  // 若这里把request_text改回JSON.stringify(request)，模型可能先填payload再被迫选动作。
  assert.equal(stub.requestTexts.length, 1);
  assert.equal(stub.requestTexts[0], row.request_text, "发送的HTTP body必须等于原始持久化请求，不重建或重新排序");
  const sent = JSON.parse(stub.requestTexts[0]!) as Json;
  assert.deepEqual(sent, row.request, "查询投影与原始wire表达相同JSON值");
  const branches = ((sent["format"] as Json)["oneOf"] as Json[]);
  assert.equal(branches.length, 6, "初始尚未读完适用规则，不提供CHECK生成分支");
  for (const branch of branches) {
    assert.equal(Object.keys(branch["properties"] as Json)[0], "action_kind",
      "Provider应先决定动作，再填写与该动作匹配的payload");
  }
});

pgTest("PG：只读源码不能据此编造合同断言；完整读取规则后同一变化可形成有界检查计划", async (t) => {
  let body = decision("READ", { material_ref: "candidate:product.py", start_line: 1, end_line: 3 });
  const stub = await provider(t, () => body);
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const source = await instance.submit(runId, await propose(instance, runId));
  assert.equal(source["status"], "COMPLETED");
  const sourceRef = (source["result"] as Json)["receipt_ref"] as string;
  const check = { ...decision("CHECK_INTENT", { capability_ref: "cap:release-check", scenario_ref: "scenario:renewal",
    objective: "验证本次变化是否保留登记的续费事实，并区分缺证据与不存在。",
    observation_requirements: ["固定候选业务观察", "登记来源中的原始续费事实"] }),
    evidence_refs: ["diff:product.py", sourceRef] };
  body = check;
  const premature = await instance.submit(runId, await propose(instance, runId));
  assert.equal(premature["status"], "REJECTED");
  assert.equal((premature["result"] as Json)["error_code"], "APPLICABLE_RULE_READ_REQUIRED");
  assert.deepEqual((premature["result"] as Json)["missing_rule_reads"], ["rule:R1"]);
  assert.equal((await instance.projection(runId))["status"], "ACTIVE");

  const ruleMaterial = bundle().materials.find((material) => material.material_ref === "rule:R1")!;
  body = decision("READ", { material_ref: "rule:R1", start_line: 1, end_line: ruleMaterial.content.split("\n").length });
  const rule = await instance.submit(runId, await propose(instance, runId));
  assert.equal(rule["status"], "COMPLETED");
  const ruleRef = (rule["result"] as Json)["receipt_ref"] as string;
  body = { ...check, evidence_refs: ["diff:product.py", sourceRef, ruleRef] };
  const planned = await instance.submit(runId, await propose(instance, runId));
  assert.equal(planned["status"], "COMPLETED");
  assert.equal((planned["result"] as Json)["status"], "PROPOSED_ONLY");
  const projection = await instance.projection(runId);
  assert.equal(projection["status"], "PLANNED");
  assert.equal(projection["reads_used"], 2);
  assert.equal(projection["model_calls_used"], 4);
  assert.deepEqual(projection["current_candidate_coverage"], []);
  assert.equal((projection["final_eligibility"] as Json)["eligible"], false);
  assert.equal(stub.requests.length, 4);
});

pgTest("PG：规则ID本身不构成观察需求，须说明具体事实后才接收检查计划", async (t) => {
  const rule = bundle().materials.find((material) => material.material_ref === "rule:R1")!;
  let body = decision("READ", { material_ref: "rule:R1", start_line: 1, end_line: rule.content.split("\n").length });
  const stub = await provider(t, () => body);
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  await instance.submit(runId, await propose(instance, runId));
  for (const observation of ["R1", " R1 "]) {
    body = decision("CHECK_INTENT", { capability_ref: "cap:release-check", scenario_ref: "scenario:renewal",
      objective: "核对业务事实。", observation_requirements: [observation] });
    const rejected = await instance.submit(runId, await propose(instance, runId));
    assert.equal(rejected["status"], "REJECTED");
    assert.equal((rejected["result"] as Json)["error_code"], "OBSERVATION_DETAILS_REQUIRED");
    assert.equal((rejected["result"] as Json)["correction"],
      "A rule ID is not an observation. Describe the concrete product input/output/state to collect and the rule-backed distinction the experiment should test.");
  }
  body = decision("CHECK_INTENT", { capability_ref: "cap:release-check", scenario_ref: "scenario:renewal",
    objective: "核对业务事实。", observation_requirements: ["收集R1适用对象的原生续费记录与候选输出，区分缺记录与明确未续费。"] });
  assert.equal((await instance.submit(runId, await propose(instance, runId)))["status"], "COMPLETED");
  assert.equal((await instance.projection(runId))["status"], "PLANNED");
});

pgTest("PG：INVALID反馈提供安全uniqueItems定位并保留真实用量，不复制原始响应内容", async (t) => {
  const sentinel = "private-sensitive-marker";
  let body = { ...decision(), requirement_refs: ["R1", "R1"], rationale: sentinel };
  const stub = await provider(t, () => body);
  const instance = await runtime(t, stub.endpoint);
  const runId = await accept(instance);
  const invalid = await instance.decide(runId, command(await instance.projection(runId)));
  assert.equal(invalid["status"], "INVALID");
  const feedback = (await instance.projection(runId))["latest_feedback"] as Json;
  const issues = feedback["validation_issues"] as Json[];
  assert.ok(issues.some((issue) => issue["keyword"] === "uniqueItems" && issue["path"] === "/requirement_refs"));
  for (const issue of issues) assert.deepEqual(Object.keys(issue).sort(), ["keyword", "path"]);
  assert.equal(JSON.stringify(feedback).includes(sentinel), false);
  const receipt = (await instance.store.pool.query("SELECT raw_response,usage FROM verify_runtime.calls WHERE id=$1", [invalid["model_call_id"]])).rows[0];
  assert.ok(receipt.raw_response.includes(sentinel), "实际原文仍保存在受限的调用证据中");
  assert.equal(receipt.usage.prompt_tokens, 120);
  assert.equal(receipt.usage.output_tokens, 50);
  body = { ...decision(), requirement_refs: ["R1"], rationale: "修正重复引用，保留原事实不确定性。" };
  const corrected = await instance.decide(runId, command(await instance.projection(runId)));
  assert.equal(corrected["status"], "COMPLETED");
  assert.equal(stub.requests.length, 2, "纠错是新的有预算调用，不抹掉旧调用");
});

pgTest("PG：模型、Prompt、Schema及Runtime构建身份冻结，换Prompt不能接管原run新调用或重派发", async (t) => {
  const stub = await provider(t, () => decision());
  const original = await runtime(t, stub.endpoint);
  const token = randomUUID() + randomUUID();
  const runId = await original.acceptRun(bundle(), token);
  const initial = await original.projection(runId);
  const recorded = initial["identities"] as Json;
  for (const field of ["runtime_build_sha256", "planner_prompt_sha256", "action_schema_sha256", "model_manifest_digest"]) {
    assert.match(String(recorded[field]), /^[0-9a-f]{64}$/);
  }
  assert.equal(recorded["model_name"], "local-test-model");
  assert.equal(recorded["planner_prompt_sha256"], sha(config(stub.endpoint).plannerPrompt));
  assert.ok(Object.keys(recorded["module_sha256"] as Json).includes("context.js"));
  const changed = new Runtime({ ...config(stub.endpoint), plannerPrompt: "Different prompt identity." });
  openRuntimes.add(changed);
  t.after(async () => { await closeRuntime(changed); });
  assert.notEqual(changed.configDigest, original.configDigest);
  await assert.rejects(changed.acceptRun(bundle(), token, undefined, runId), { message: "RUN_IDENTITY_CONFLICT" });
  const cmd = command(initial);
  await assert.rejects(changed.decide(runId, cmd), { message: "CONFIG_DRIFT" });
  assert.equal(stub.requests.length, 0);

  const barrier = holdCommittedTransaction(original,
    (value) => value !== null && typeof value === "object" && (value as Json)["status"] === "RESERVED");
  const originalDecide = original.decide(runId, cmd);
  await barrier.committed.promise;
  const wrongResume = await changed.decide(runId, cmd);
  assert.equal(wrongResume["status"], "CANCELLED", "已登记但未派发的调用也必须重检冻结配置");
  barrier.resume.resolve();
  assert.equal((await originalDecide)["status"], "CANCELLED");
  assert.equal(stub.requests.length, 0);
  const final = await original.projection(runId);
  assert.deepEqual(final["identities"], recorded);
});
