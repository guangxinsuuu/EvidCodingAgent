/**
 * 有界本地模型验收：真实历史原件 + 真实 PostgreSQL + 同一 Python Graph + MCP HTTP。
 * 使用独立 live-review/live-runtime 库；本脚本的 QA 接受是测试账户行为，不是用户签署。
 * 不构建、不下载模型、不覆盖历史证据；完整模型记录保留在私有 Runtime 库。
 */
import assert from "node:assert/strict";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { ReviewStore } from "../apps/review-workbench/dist/store.js";
import { WorkflowStore } from "../apps/review-workbench/dist/workflow-store.js";
import { createReviewServer } from "../apps/review-workbench/dist/server.js";
import { loadHistoricalCase } from "../apps/review-workbench/dist/importer.js";
import { loadEvidenceArchive } from "../apps/review-workbench/dist/evidence-archive.js";
import { createArchiveMcpServer, McpArchiveReader, RegisteredArchive } from "../apps/review-workbench/dist/evidence-mcp.js";
import { EvidenceWorker } from "../apps/review-workbench/dist/evidence-worker.js";
import { Runtime } from "../packages/verify-runtime/dist/runtime.js";
import { createVerifyServer } from "../packages/verify-runtime/dist/server.js";
import { privateText } from "../apps/review-workbench/dist/config.js";

const base = "artifacts/review-workbench";
const reviewUrl = (await privateText(`${base}/live-review-database-url.private`)).trim();
const runtimeUrl = (await privateText(`${base}/live-runtime-database-url.private`)).trim();
assert.equal(new URL(reviewUrl).pathname, "/egap_evidence_live_review");
assert.equal(new URL(runtimeUrl).pathname, "/egap_evidence_live_runtime");
const id = `evidence-live-${randomUUID()}`, output = resolve(base, id);
await mkdir(output, { mode: 0o700 });
const summary = { id, mode: "LOCAL_REAL_MODEL_INTEGRATION_KNOWN_ARCHIVE", user_qa_approval: false,
  protected_final: false, release_authorized: false, scenarios: [], status: "RUNNING" };
const log = (event) => process.stdout.write(JSON.stringify(event) + "\n");
const code = randomBytes(32).toString("hex"), sha = s => createHash("sha256").update(s).digest("hex");
const qa = { id: `${id}:qa`, display_name: "自动化验收 QA（非用户批准）", role: "QA", tenant_id: "tenant-b" };
const owner = { id: `${id}:owner`, display_name: "自动化验收 Owner", role: "BUSINESS", tenant_id: "tenant-b" };
const store = new ReviewStore(reviewUrl), workflow = new WorkflowStore(store.pool);
const registry = new RegisteredArchive(), mcpToken = randomBytes(32).toString("hex");
const mcp = createArchiveMcpServer(registry, mcpToken);
await mcp.listen({ host: "127.0.0.1", port: 0 });
const endpoint = `http://127.0.0.1:${mcp.server.address().port}/mcp`;
let worker;
const workerConfig = JSON.parse(await privateText(`${base}/worker-config.private.json`));
const runtime = new Runtime({ databaseUrl: runtimeUrl, model: workerConfig.model,
  plannerPrompt: await readFile("services/verify-agent/planner-prompt.txt", "utf8"), evidenceReadPort: new McpArchiveReader(endpoint, mcpToken),
  assertEvidenceDispatchAllowed: runId => worker.assertDispatchAllowed(runId) });
const runtimeApi = createVerifyServer(runtime);
const origin = "http://127.0.0.1:4178";
const app = createReviewServer(store, { origin, identities: [{ actor: qa, code_hash: sha(code) }], workflow });
try {
  await store.migrate(); await workflow.migrate(); await runtime.store.migrate();
  const legacy = await loadHistoricalCase(process.cwd());
  const archive = await loadEvidenceArchive(process.cwd());
  await workflow.seedCase({ legacy: { ...legacy, id, title: "端到端验收副本 · 历史原件" }, archive,
    actors: [qa, owner], owner_id: owner.id, qa_assignee_id: qa.id });
  await runtimeApi.listen({ host: "127.0.0.1", port: 0 });
  worker = new EvidenceWorker(workflow, runtime, registry, `http://127.0.0.1:${runtimeApi.server.address().port}`,
    `live-worker-${randomUUID()}`, undefined, output);
  await app.listen({ host: "127.0.0.1", port: 4178 });
  const login = await fetch(`${origin}/api/v1/session`, { method: "POST", headers: { origin, "content-type": "application/json" }, body: JSON.stringify({ code }) });
  assert.equal(login.status, 200);
  const session = await login.json();
  const headers = { origin, "content-type": "application/json", "x-csrf-token": session.csrf_token, cookie: login.headers.get("set-cookie").split(";")[0] };
  const api = async (path, body) => {
    const response = await fetch(`${origin}/api/v2${path}`, { headers, ...(body ? { method: "POST", body: JSON.stringify(body) } : {}) });
    const result = await response.json(); assert.equal(response.status, 200, `API_${response.status}_${result.error_code}`); return result;
  };
  const tasks = (await api("/me/review-tasks")).tasks.filter(t => t.case_id === id);
  assert.equal(tasks.length, 2);
  const taskId = tasks.find(t => t.obligation_id === "EVIDENCE_LINKAGE").id;
  const detail = () => api(`/review-tasks/${taskId}`);
  const basis = d => ({ idempotency_key: randomUUID(), expected_task_revision: d.task.task_revision,
    expected_assignment_revision: d.task.assignment_revision, expected_case_scope_revision: d.task.case_scope_revision,
    reviewed_bundle_id: d.task.bound_bundle_id, reviewed_bundle_digest: d.task.bound_bundle_digest, candidate_sha: d.task.candidate_sha });
  await api(`/review-tasks/${taskId}/claim`, basis(await detail()));
  for (const scenario of ["ORIGINALS", "RAW_PROVIDER_MISSING"]) {
    const d = await detail(), target = d.allowed_targets[0];
    const templates = scenario === "ORIGINALS" ? d.item_templates.slice(0, 3) : d.item_templates.slice(3, 4);
    const request = await api(`/review-tasks/${taskId}/evidence-requests`, { ...basis(d),
      goal: scenario === "ORIGINALS" ? "逐项核实本次指定 Job 与阶段的草稿、最终私有备注和交付关联。读取确切原件后提出响应，保留业务语义未审查。" : "提供指定 Job 的 provider 原始模型响应；如固定归档确实未采集，准确指出范围受限的缺失，不能用草稿替代。",
      items: templates.map((template, index) => ({ item_id: `E${index + 1}`, kind: template.kind, target,
        required: true, acceptance_criteria: template.acceptance_criteria })) });
    log({ event: "scenario_started", scenario, request_id: request.request_id, model_budget: 6, read_budget: 4 });
    const execution = await worker.runOnce(); log({ event: "worker_finished", scenario, ...execution });
    const after = await detail(), current = after.requests.find(r => r.id === request.request_id);
    const item = { scenario, request_id: request.request_id, execution_state: current.execution_state,
      fulfillment_state: current.fulfillment_state, response_id: current.current_response_id,
      dispositions: current.current_response?.body.items.map(i => ({ item_id: i.item_id, disposition: i.validated_disposition, unavailable_reason: i.unavailable_reason })) ?? [],
      read_receipts: current.current_response?.read_receipts.length ?? 0, accepted: false };
    summary.scenarios.push(item);
    if (current.current_response) {
      assert.equal(current.execution_state, "FINISHED");
      if (scenario === "ORIGINALS") {
        assert.equal(current.fulfillment_state, "COMPLETE");
        assert.equal(current.current_response.body.items.length, 3);
        assert.ok(current.current_response.body.items.every(i => i.validated_disposition === "PROVIDED"));
        assert.equal(current.current_response.read_receipts.length, 3);
        assert.ok(current.current_response.read_receipts.every(r => r.transport === "MCP_ARCHIVE"));
      } else {
        assert.equal(current.fulfillment_state, "UNAVAILABLE");
        assert.equal(current.current_response.body.items[0].unavailable_reason, "NOT_CAPTURED_IN_PINNED_ARCHIVE");
        assert.equal(current.current_response.read_receipts.length, 0);
      }
      const accepted = await api(`/evidence-requests/${request.request_id}/accept`, { ...basis(after), request_spec_revision: current.request_spec_revision,
        response_id: current.current_response.id, response_digest: current.current_response.digest, review_policy_version: after.case.review_policy_version });
      item.accepted = accepted.review_state === "ACCEPTED";
      item.obligation_status = accepted.status;
      assert.notEqual(accepted.status, "COMPLETED"); // 单个阶段最多3/12，不能误关闭整体义务。
    }
    const snapshot = await workflow.loadRequest(request.request_id);
    const projection = await runtime.reconcileProjection(snapshot.mapping.run_id);
    item.model_calls = projection.model_calls_used; item.read_calls = projection.reads_used;
    const calls = await runtime.store.pool.query("SELECT request_text FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at,id", [snapshot.mapping.run_id]);
    item.later_model_saw_read_receipts = calls.rows.some(r => JSON.parse(r.request_text).messages.some(m =>
      m.role === "user" && (JSON.parse(m.content).context_view?.read_receipts?.length ?? 0) > 0));
    if (scenario === "ORIGINALS" && item.accepted) assert.equal(item.later_model_saw_read_receipts, true);
    assert.equal(await worker.runOnce(), null); // 已交付请求不重复派发。
  }
  summary.status = summary.scenarios.every(x => x.accepted) ? "COMPLETED" : "MODEL_OR_PROTOCOL_GAP";
  summary.model = { name: workerConfig.model.name, digest: workerConfig.model.digest };
  summary.budget = (await detail()).case;
} catch (e) {
  summary.status = "VALIDATION_FAILED"; summary.error = e instanceof Error ? e.message : "UNKNOWN";
  process.exitCode = 1;
} finally {
  await writeFile(`${output}/summary.private.json`, JSON.stringify(summary, null, 2), { mode: 0o600 });
  log({ event: "validation_finished", id, status: summary.status, scenarios: summary.scenarios, error: summary.error ?? null });
  await app.close(); await runtimeApi.close(); await mcp.close(); await runtime.close(); await store.close();
}
