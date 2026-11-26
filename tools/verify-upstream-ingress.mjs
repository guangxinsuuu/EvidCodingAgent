#!/usr/bin/env node
/** 操作员联调：真实来源/PG/Runtime/LangGraph + 明确标记的模型协议桩。
 * 不调用业务写接口、不启动 Codex、不把固定 READ 次序算作模型自主调查。
 * 所有私有输入/回执留在本次目录，Graph 进程只持当前 Run 的 scoped token。
 */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { constants, openSync, closeSync, fstatSync, readFileSync, writeFileSync, existsSync, realpathSync } from "node:fs";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { randomBytes, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { Runtime } from "../packages/verify-runtime/dist/runtime.js";
import { SourceReaderSet } from "../packages/verify-runtime/dist/source-authority.js";
import { createVerifyServer } from "../packages/verify-runtime/dist/server.js";
import { sha, validateBundle } from "../packages/verify-runtime/dist/types.js";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const directory = resolve(process.argv[2] ?? "");
assert.ok(directory.startsWith(join(root, "artifacts/upstream-integration") + "/") && realpathSync(directory) === directory);
const save = (name, value) => writeFileSync(join(directory, name), JSON.stringify(value, null, 2) + "\n", { mode: 0o600, flag: "wx" });
function privateText(path) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try { const s = fstatSync(fd); assert.ok(s.isFile() && !(s.mode & 0o077) && s.size < 2_000_000); return readFileSync(fd, "utf8"); }
  finally { closeSync(fd); }
}
async function sourceRequest(config, path, body) {
  const url = new URL(config.base_url);
  assert.equal(url.protocol, "http:"); assert.equal(url.hostname, "127.0.0.1");
  const response = await fetch(config.base_url + path, { method: body ? "POST" : "GET", redirect: "error",
    headers: { authorization: "Bearer " + config.token, "content-type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(5000) });
  return { status: response.status, body: await response.json() };
}
async function graph(url, runId, name) {
  // 不把数据库、来源发布者或模型配置传给 Graph 子进程。
  const output = join(directory, name + ".private.json");
  const child = spawn(join(root, "services/verify-agent/.venv/bin/python"), ["-m", "verify_agent", "--runtime", url,
    "--run-id", runId, "--token-file", join(directory, "graph-token.private"), "--output", output, "--max-iterations", "16"], {
    cwd: root, env: { PATH: process.env.PATH, PYTHONPATH: join(root, "services/verify-agent/src"), LANG: "en_US.UTF-8" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const chunks = []; child.stdout.on("data", c => chunks.push(c)); child.stderr.on("data", c => chunks.push(c));
  const code = await new Promise((resolveExit, reject) => {
    const timer = setTimeout(() => { child.kill("SIGTERM"); reject(new Error("GRAPH_TIMEOUT")); }, 60_000);
    child.once("error", reject); child.once("exit", n => { clearTimeout(timer); resolveExit(n); });
  });
  writeFileSync(join(directory, name + ".private.log"), Buffer.concat(chunks), { mode: 0o600, flag: "wx" });
  assert.equal(code, 0, "Graph must finish its scoped run");
  return JSON.parse(privateText(output));
}

async function main() {
  assert.ok(!existsSync(join(directory, "verification-result.json")), "Use a new output directory for another integration run");
  const bundle = validateBundle(JSON.parse(privateText(join(directory, "release-bundle.private.json"))));
  const instanceIds = [...new Set(bundle.external_sources.map(s => s.instance_id))];
  const readers = SourceReaderSet.fromFiles(instanceIds.map(id => join(root, "artifacts/source-platform", id, "planner-reader.private.json")));
  assert.equal((await readers.authorize(bundle)).length, 7);
  const sourceId = bundle.external_sources.find(s => s.namespace === "diagnostics").instance_id;
  const sourceDirectory = join(root, "artifacts/source-platform", sourceId);
  const operator = JSON.parse(privateText(join(sourceDirectory, "operator.private.json")));
  const readRefs = ["jira:EGAPLAB-100", "acceptance:phase-review-contract", "providers:phase-platforms",
    "diagnostics:INV-LAB-100-OBSERVATION", "diagnostics:INV-LAB-100", "diagnostics:INV-LAB-100-HANDOFF", "rule:R5"];
  const diff = bundle.changes.find(c => bundle.materials.some(m => m.material_ref === c.diff_material_ref))?.diff_material_ref;
  assert.ok(diff);
  const script = [{ kind: "UPDATE_DIRECTION" }, ...readRefs.map(ref => ({ kind: "READ", ref })), { kind: "STOP" }];
  let calls = 0;
  const wire = [];
  const model = createServer(async (request, response) => {
    response.setHeader("content-type", "application/json");
    if (request.url === "/api/tags") {
      response.end(JSON.stringify({ models: [{ name: "upstream-ingress-protocol-stub", digest: "1".repeat(64) }] })); return;
    }
    const parts = []; for await (const chunk of request) parts.push(chunk);
    const original = Buffer.concat(parts).toString("utf8");
    const step = script[calls++];
    if (!step) { response.statusCode = 500; response.end("{}"); return; }
    const material = bundle.materials.find(m => m.material_ref === step.ref);
    if (material) assert.ok(material.content.split("\n").length <= 200);
    const body = { action_kind: step.kind, requirement_refs: ["R5"], hypothesis_refs: ["H:source-boundary"], evidence_refs: [diff],
      expected_information: "核对独立来源、观察范围与当前变化的关联", rationale: "联调协议固定步骤，不代表模型自主选择",
      hypothesis_updates: step.kind === "UPDATE_DIRECTION" ? [{ id: "H:source-boundary", statement: "模型出口变化可能影响事实与原始输出的可追溯关系", status: "OPEN", evidence_refs: [diff] }] : [],
      payload: step.kind === "UPDATE_DIRECTION" ? { focus: "核对变化及上游依据的身份边界" } : step.kind === "READ" ?
        { material_ref: step.ref, start_line: 1, end_line: material.content.split("\n").length } :
        { reason: "来源读取联调完成；原件尚未导入，没有当前候选实验或发布资格。" },
      decision_frame: { unresolved_questions: [{ id: "Q:source", question: "哪些依据可以支持下一步验证", why_unresolved: "尚需区分声明接口、上游假设与实际原件", evidence_refs: [diff] }],
        selected_question_id: "Q:source", outcome_branches: [
          { observation: "版本和范围一致", implication: "继续读取相关依据", next_action: "READ" },
          { observation: "原件缺失或范围不一致", implication: "保留缺项并澄清", next_action: "CLARIFY" }] } };
    const modelRequest = JSON.parse(original);
    const context = JSON.parse(modelRequest.messages.find(m => m.role === "user").content).context_view;
    // 核对真实 wire 上每份已读材料仍是完整原片段，压缩没有把未知状态或正文吞掉。
    for (const receipt of context.read_receipts) {
      const source = bundle.materials.find(m => m.material_ref === receipt.material_ref);
      assert.equal(receipt.content, source.content.split("\n").slice(receipt.start_line - 1, receipt.end_line).join("\n"));
      assert.equal(receipt.fragment_sha256, sha(receipt.content));
      assert.equal(receipt.proves_current_business_behavior, false);
    }
    wire.push({ request_sha256: sha(original), request_bytes: Buffer.byteLength(original), read_count: context.read_receipts.length,
      action_kind: step.kind, material_ref: step.ref ?? null });
    response.end(JSON.stringify({ model: "upstream-ingress-protocol-stub", done: true, message: { content: JSON.stringify(body) }, prompt_eval_count: 0, eval_count: 0 }));
  });
  await new Promise(resolveListen => model.listen(0, "127.0.0.1", resolveListen));
  const config = { databaseUrl: privateText(join(root, "artifacts/review-workbench/runtime-test-database-url.private")).trim(),
    plannerPrompt: "Protocol integration fixture; not a model quality evaluation.", sourceReader: readers,
    model: { endpoint: `http://127.0.0.1:${model.address().port}`, name: "upstream-ingress-protocol-stub", digest: "1".repeat(64), timeoutMs: 3000, maxOutputTokens: 2000 } };
  const token = randomBytes(32).toString("hex"), runId = randomUUID();
  writeFileSync(join(directory, "graph-token.private"), token, { mode: 0o600, flag: "wx" });
  let runtime = new Runtime(config), server;
  const checks = [];
  try {
    await runtime.store.migrate();
    const limits = { model_calls: 12, reads: 8, no_progress: 3, plan_repairs: 0 };
    await runtime.acceptRun(bundle, token, limits, runId);
    save("run-identity.json", { run_id: runId, runtime_identities: runtime.identities,
      bundle_sha256: sha(readFileSync(join(directory, "release-bundle.private.json"), "utf8")),
      qualification: "REAL_SOURCE_PG_LANGGRAPH_WITH_SCRIPTED_MODEL_PROTOCOL" });
    server = createVerifyServer(runtime); const url = await server.listen({ host: "127.0.0.1", port: 0 });
    const first = await graph(url, runId, "graph");
    assert.equal(first.outcome, "STOPPED"); assert.equal(calls, 9);
    let projection = await runtime.projection(runId);
    assert.equal(projection.read_receipts.length, 7); assert.deepEqual(projection.current_candidate_coverage, []);
    assert.equal(projection.final_eligibility.eligible, false);
    assert.ok(projection.read_receipts.every(r => r.transport === "SOURCE_HTTP_REAUTHORIZED_SNAPSHOT" && r.source_authorization.length === 7));
    checks.push({ name: "graph_reads_all_seven_sources", result: "CONFIRMED", model_protocol_calls: calls, real_model_calls: 0 });
    assert.ok(wire.every(row => row.request_bytes <= 100_000));
    checks.push({ name: "model_context_preserves_read_content_within_existing_limit", result: "CONFIRMED",
      maximum_request_bytes: Math.max(...wire.map(row => row.request_bytes)) });
    const operations = projection.operations.length;
    // 关闭并重新创建 TS Runtime 和 HTTP Server，再用新 Python 进程从 PG 恢复。
    await server.close(); server = undefined; await runtime.close(); runtime = new Runtime(config);
    await runtime.acceptRun(bundle, token, limits, runId);
    server = createVerifyServer(runtime); const restartUrl = await server.listen({ host: "127.0.0.1", port: 0 });
    const replay = await graph(restartUrl, runId, "graph-replay");
    assert.equal(replay.outcome, "STOPPED"); assert.equal(calls, 9);
    projection = await runtime.projection(runId); assert.equal(projection.operations.length, operations);
    checks.push({ name: "new_runtime_and_graph_resume_without_new_calls", result: "CONFIRMED" });
    const cachedRead = projection.operations.find(o => o.action_kind === "READ");
    assert.ok(cachedRead);
    const cachedId = (await runtime.store.pool.query("SELECT id FROM verify_runtime.operations WHERE run_id=$1 AND proposal->>'action_kind'='READ' ORDER BY created_at LIMIT 1", [runId])).rows[0].id;
    const grant = { principal_id: "lab:planner-reader", namespace: "diagnostics", object_id: "INV-LAB-100", permission: "READ" };
    for (const [name, revoke, restore] of [
      ["acl_service_unavailable_rejects_fresh_cache", { acl_available: false }, { acl_available: true }],
      ["revoked_diagnosis_rejects_cached_operation", { ...grant, enabled: false }, { ...grant, enabled: true }],
    ]) {
      try {
        assert.equal((await sourceRequest(operator, "/v1/control", revoke)).status, 200);
        await assert.rejects(runtime.projection(runId), /SOURCE_ACCESS_UNAVAILABLE/);
        await assert.rejects(runtime.operation(runId, cachedId), /SOURCE_ACCESS_UNAVAILABLE/);
        await assert.rejects(runtime.decide(runId, { command_id: projection.next_command_id, based_on_decision_revision: projection.decision_basis_revision }), /SOURCE_ACCESS_UNAVAILABLE/);
        assert.equal(calls, 9); checks.push({ name, result: "CONFIRMED" });
      } finally { assert.equal((await sourceRequest(operator, "/v1/control", restore)).status, 200); }
    }
    for (const role of ["product-reader", "outsider-reader", "qa-publisher"]) {
      const actor = JSON.parse(privateText(join(sourceDirectory, role + ".private.json")));
      const denied = await sourceRequest(actor, "/v1/objects/diagnostics/INV-LAB-100/versions/1");
      assert.equal(denied.status, 404); checks.push({ name: role + "_cannot_read_diagnosis", result: "CONFIRMED" });
    }
    for (const [name, edit, error] of [
      ["upstream_cannot_authorize_repair", v => { v.repair_authorized = true; }, /PROJECT1_AUTHORITY_UPGRADE_FORBIDDEN/],
      ["other_candidate_handoff_rejected", v => { v.candidate.commit = "f".repeat(40); }, /PROJECT1_CANDIDATE_MISMATCH/],
    ]) {
      const changed = structuredClone(bundle), material = changed.materials.find(m => m.material_ref === "diagnostics:INV-LAB-100-HANDOFF");
      const value = JSON.parse(material.content); edit(value); material.content = JSON.stringify(value);
      material.sha256 = sha(material.content); material.source_byte_end = Buffer.byteLength(material.content);
      await assert.rejects(runtime.acceptRun(changed, token), error); checks.push({ name, result: "CONFIRMED" });
    }
    save("projection.private.json", await runtime.projection(runId));
    const callsRows = await runtime.store.pool.query("SELECT id,status,applied,basis FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at", [runId]);
    save("model-protocol-calls.json", { calls: callsRows.rows, wire, qualification: "SCRIPTED_MODEL_NOT_REAL_LLM" });
    const result = { status: "UPSTREAM_INGRESS_CONFIRMED", run_id: runId, source_instances: instanceIds, source_count: 7,
      checks, graph_model_protocol_calls: calls, real_model_calls: 0, business_writes: 0, current_candidate_coverage: [], final_eligible: false };
    save("verification-result.json", result);
    console.log(JSON.stringify({ status: result.status, run_id: runId, checks: checks.length, source_count: 7, real_model_calls: 0, final_eligible: false }));
  } catch (error) {
    save("verification-failure.json", { status: "INCOMPLETE", code: error.code ?? error.name ?? "ERROR", message: error.code ? undefined : String(error.message).slice(0, 500), calls });
    throw error;
  } finally {
    if (server) await server.close(); await runtime.close();
    model.closeAllConnections(); await new Promise(resolveClose => model.close(resolveClose));
  }
}
main().catch(error => { console.error(JSON.stringify({ status: "INCOMPLETE", code: error.code ?? error.name ?? "ERROR" })); process.exitCode = 1; });
