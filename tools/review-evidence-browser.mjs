/**
 * 已知归档的隔离浏览器验收入口。它恢复既有 Case，不创建人员决定或改写历史。
 * 浏览器或显式 HTTP 验收客户端负责接手/请求/接受/撤回；本启动器不自动作出人员决定。
 * Worker 仅消费这个 Case 已提交的请求；启动成功本身不证明浏览器点击验收通过。
 * 此处的实验 QA 身份不代表用户、公司业务批准或代码发布资格。
 */
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { mkdir, lstat, readFile, rename, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { ReviewStore, canonical } from "../apps/review-workbench/dist/store.js";
import { WorkflowStore } from "../apps/review-workbench/dist/workflow-store.js";
import { createReviewServer } from "../apps/review-workbench/dist/server.js";
import { RegisteredArchive, createArchiveMcpServer, McpArchiveReader } from "../apps/review-workbench/dist/evidence-mcp.js";
import { EvidenceWorker } from "../apps/review-workbench/dist/evidence-worker.js";
import { Runtime } from "../packages/verify-runtime/dist/runtime.js";
import { createVerifyServer } from "../packages/verify-runtime/dist/server.js";
import { assertLocalDatabase, privateText } from "../apps/review-workbench/dist/config.js";

const DEFAULT_CASE = "evidence-live-4a4640d4-36d4-4dcb-b849-4429dccb5d35";
const CASE_ID = /^evidence-live-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const BASE = "artifacts/review-workbench", ORIGIN = "http://127.0.0.1:4178";
const log = event => process.stdout.write(JSON.stringify(event) + "\n");

/** 验收器额外限制 claim 的 Case；即使检查后有外部队列插入，也绝不领取其他 Case。 */
class BrowserCaseStore extends WorkflowStore {
  constructor(pool, caseId) { super(pool); this.caseId = caseId; }
  async assertNoForeignWork(client = this.pool) {
    const result = await client.query(`SELECT 1 FROM egap_review_v2.outbox o
      JOIN egap_review_v2.requests r ON r.id=o.request_id
      WHERE r.case_id<>$1 AND o.state IN ('PENDING','LEASED','UNKNOWN','RECONCILING') LIMIT 1`, [this.caseId]);
    if (result.rowCount) throw new Error("OTHER_CASE_HAS_PENDING_WORK");
  }
  async claimScoped(workerId, leaseMs, reconcile) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(workerId) || !Number.isSafeInteger(leaseMs) || leaseMs < 1 || leaseMs > 300_000) throw new Error("INVALID_WORKER_LEASE");
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN"); await this.assertNoForeignWork(client);
      const eligibility = reconcile
        ? "o.state IN ('UNKNOWN','RECONCILING') AND (o.lease_until IS NULL OR o.lease_until<now())"
        : "(o.state='PENDING' OR (o.state='LEASED' AND o.lease_until<now())) AND r.withdrawn=false AND o.spec_revision=r.request_spec_revision";
      const row = (await client.query(`SELECT o.* FROM egap_review_v2.outbox o JOIN egap_review_v2.requests r ON r.id=o.request_id
        WHERE r.case_id=$1 AND ${eligibility} ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT 1`, [this.caseId])).rows[0];
      if (!row) { await client.query("COMMIT"); return null; }
      await client.query("UPDATE egap_review_v2.outbox SET state=$2,worker_id=$3,lease_until=now()+($4::int*interval '1 millisecond'),attempts=attempts+1 WHERE id=$1",
        [row.id, reconcile ? "RECONCILING" : "LEASED", workerId, leaseMs]);
      await client.query("COMMIT");
      return { id: row.id, request_id: row.request_id, request_spec_revision: row.spec_revision, worker_id: workerId, attempts: row.attempts + 1 };
    } catch (error) { await client.query("ROLLBACK"); throw error; } finally { client.release(); }
  }
  claimWork(workerId, leaseMs = 30_000) { return this.claimScoped(workerId, leaseMs, false); }
  claimReconciliationWork(workerId, leaseMs = 30_000) { return this.claimScoped(workerId, leaseMs, true); }
}

async function privateDirectory(path) {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const stat = await lstat(path);
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0) throw new Error("PRIVATE_DIRECTORY_REQUIRED");
}

/** 只汇总状态、覆盖与成本；不把草稿/备注/模型解释、凭据引用写进摘要或 stdout。 */
async function snapshot(store, caseId, qa, sessionId, status) {
  const listed = (await store.listTasks(qa)).tasks.filter(task => task.case_id === caseId);
  const tasks = [], requests = [];
  let budget = null;
  for (const listedTask of listed) {
    const detail = await store.getTask(listedTask.id, qa), task = detail.task;
    const required = new Set(detail.required_obligation_coverage.map(item => canonical(item)));
    const provided = new Set();
    for (const request of detail.requests) {
      const response = request.current_response?.body;
      if (!request.withdrawn && request.review_state === "ACCEPTED" && request.current_response?.archived === false) for (const item of response?.items ?? []) {
        const asked = request.input.request_items.find(r => r.item_id === item.item_id);
        if (asked && item.validated_disposition === "PROVIDED") {
          const key = canonical({ kind: asked.kind, target: asked.target });
          if (required.has(key)) provided.add(key);
        }
      }
      requests.push({ id: request.id, task_id: task.id, spec_revision: request.request_spec_revision,
        execution_state: request.execution_state, review_state: request.review_state, fulfillment_state: request.fulfillment_state,
        withdrawn: request.withdrawn, response_id: request.current_response_id,
        items: (response?.items ?? []).map(item => ({ item_id: item.item_id, disposition: item.validated_disposition })) });
    }
    tasks.push({ id: task.id, obligation_id: task.obligation_id, status: task.status, claimed: task.claimed,
      task_revision: task.task_revision, assignment_revision: task.assignment_revision,
      coverage: { provided: provided.size, required: required.size } });
    budget = { cap: detail.case.budget_cap, reserved: detail.case.budget_reserved, consumed: detail.case.budget_consumed };
  }
  const mappings = (await store.pool.query(`SELECT m.request_id,m.spec_revision,m.data->>'run_id' AS run_id,
    m.data->'consumed' AS consumed,m.data->>'settlement' AS settlement
    FROM egap_review_v2.run_mappings m JOIN egap_review_v2.requests r ON r.id=m.request_id WHERE r.case_id=$1
    ORDER BY m.request_id,m.spec_revision`, [caseId])).rows;
  return { schema_version: 1, session_id: sessionId, case_id: caseId, status,
    scope: "KNOWN_ARCHIVE_CLIENT_ACCEPTANCE", browser_click_acceptance: "NOT_ESTABLISHED_BY_LAUNCHER",
    user_qa_approval: false, protected_final: false, release_authorized: false,
    // 多次只读查询不宣称是单一数据库事务快照；发布资格始终来自正式门禁，而非这个诊断文件。
    observation_consistency: "SEQUENTIAL_READS_NOT_ATOMIC", observed_at: new Date().toISOString(), tasks, requests, budget, runs: mappings };
}

export async function main(args = process.argv.slice(2)) {
  const caseId = args.length === 0 ? DEFAULT_CASE : args.length === 2 && args[0] === "--case-id" ? args[1] : "";
  if (!CASE_ID.test(caseId)) throw new Error("EXPECTED_EXACT_EVIDENCE_LIVE_CASE_ID");
  const reviewUrl = (await privateText(`${BASE}/live-review-database-url.private`)).trim();
  const runtimeUrl = (await privateText(`${BASE}/live-runtime-database-url.private`)).trim();
  for (const url of [reviewUrl, runtimeUrl]) assertLocalDatabase(url);
  if (new URL(reviewUrl).pathname !== "/egap_evidence_live_review" || new URL(runtimeUrl).pathname !== "/egap_evidence_live_runtime") throw new Error("ISOLATED_LIVE_DATABASES_REQUIRED");
  const sessionId = randomUUID(), folder = resolve(BASE, "browser-coverage", caseId);
  await privateDirectory(resolve(BASE, "browser-coverage")); await privateDirectory(folder);
  // 原先 live 工具以 Case 为 credential 根。恢复时必须保留相同绝对路径，不能生成第二套 Run 凭据。
  const credentialRoot = resolve(BASE, caseId); await privateDirectory(credentialRoot);
  const codePath = resolve(folder, "qa.code.private");
  try { await writeFile(codePath, randomBytes(32).toString("hex"), { flag: "wx", mode: 0o600 }); }
  catch (error) { if (error.code !== "EEXIST") throw error; }
  const code = (await privateText(codePath)).trim();
  if (!/^[a-f0-9]{64}$/.test(code)) throw new Error("INVALID_LOCAL_QA_CODE");

  const review = new ReviewStore(reviewUrl), store = new BrowserCaseStore(review.pool, caseId);
  let app, api, mcp, runtime, qa, worker, stopping = false, finalStatus = "STOPPED";
  const summaryPath = resolve(folder, `${sessionId}.summary.private.json`);
  const save = async status => {
    if (!qa) return;
    const summary = await snapshot(store, caseId, qa, sessionId, status);
    const pending = `${summaryPath}.tmp`;
    await writeFile(pending, JSON.stringify(summary, null, 2) + "\n", { mode: 0o600 });
    await rename(pending, summaryPath);
  };
  const stop = () => { if (!stopping) log({ event: "shutdown_requested", session_id: sessionId }); stopping = true; };
  process.once("SIGINT", stop); process.once("SIGTERM", stop);
  try {
    await store.migrate();
    const existing = (await review.pool.query("SELECT tenant_id,actors FROM egap_review_v2.cases WHERE id=$1", [caseId])).rows[0];
    qa = existing?.actors.find(actor => actor.id === `${caseId}:qa` && actor.role === "QA" && actor.tenant_id === existing.tenant_id);
    if (!qa) throw new Error("EXISTING_CASE_QA_REQUIRED");
    await store.assertNoForeignWork();
    const registry = new RegisteredArchive(), mcpToken = randomBytes(32).toString("hex");
    mcp = createArchiveMcpServer(registry, mcpToken); await mcp.listen({ host: "127.0.0.1", port: 0 });
    const config = JSON.parse(await privateText(`${BASE}/worker-config.private.json`));
    runtime = new Runtime({ databaseUrl: runtimeUrl, model: config.model,
      plannerPrompt: await readFile("services/verify-agent/planner-prompt.txt", "utf8"),
      evidenceReadPort: new McpArchiveReader(`http://127.0.0.1:${mcp.server.address().port}/mcp`, mcpToken),
      assertEvidenceDispatchAllowed: async runId => { if (stopping) throw new Error("BROWSER_ACCEPTANCE_STOPPING"); await worker.assertDispatchAllowed(runId); } });
    await runtime.store.migrate(); api = createVerifyServer(runtime); await api.listen({ host: "127.0.0.1", port: 0 });
    worker = new EvidenceWorker(store, runtime, registry, `http://127.0.0.1:${api.server.address().port}`,
      `browser-acceptance-${sessionId}`, undefined, credentialRoot);
    app = createReviewServer(review, { origin: ORIGIN, workflow: store,
      identities: [{ actor: qa, code_hash: createHash("sha256").update(code).digest("hex") }] });
    await app.listen({ host: "127.0.0.1", port: 4178 });
    await save("READY");
    log({ event: "READY", origin: `${ORIGIN}/#/tasks`, case_id: caseId, code_path: codePath, summary_path: summaryPath,
      personnel_actions: "EXTERNAL_CLIENT_ONLY", worker_scope: "EXACT_CASE", user_qa_approval: false });
    let nextSummary = Date.now() + 5_000;
    while (!stopping) {
      const result = await worker.reconcileOnce() ?? await worker.runOnce();
      if (result) {
        log({ event: "worker_result", request_id: result.request_id, run_id: result.run_id ?? null,
          execution_state: result.execution_state, error_code: result.error_code ?? null, reconciliation: result.reconciliation ?? null });
        await save("RUNNING");
      } else if (Date.now() >= nextSummary) { await save("RUNNING"); nextSummary = Date.now() + 5_000; }
      if (!stopping) await new Promise(done => setTimeout(done, 1_000));
    }
  } catch (error) {
    finalStatus = "FAILED";
    log({ event: "browser_acceptance_failed", error_code: /^[A-Z_]+$/.test(error.message ?? "") ? error.message : "BROWSER_ACCEPTANCE_FAILED" });
    process.exitCode = 1;
  } finally {
    stopping = true; process.off("SIGINT", stop); process.off("SIGTERM", stop);
    // SIGTERM 不捏造终态：等待当前有界调用返回，随后保存数据库事实，再关闭通道。
    await app?.close();
    try { await save(finalStatus); } catch { log({ event: "summary_unavailable", session_id: sessionId }); }
    await api?.close(); await mcp?.close(); await runtime?.close(); await review.close();
    log({ event: "browser_acceptance_stopped", status: finalStatus, summary_path: summaryPath });
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void main().catch(() => { process.stderr.write("Browser acceptance setup failed; check private local inputs.\n"); process.exitCode = 1; });
}
