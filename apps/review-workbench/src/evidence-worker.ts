import { createHash, randomBytes, randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { spawn } from "node:child_process";
import { Runtime, type Limits, type RuntimeConfig } from "@egap/verify-runtime";
import { createVerifyServer } from "@egap/verify-runtime/server";
import type { EvidenceResponse } from "@egap/contracts";
import { assertLocalDatabase, loadConfig, privateText } from "./config.js";
import { ReviewStore } from "./store.js";
import { WorkflowStore } from "./workflow-store.js";
import type { RequestSnapshot, RunMapping, RuntimeResult, WorkItem } from "./workflow-types.js";
import { createArchiveMcpServer, McpArchiveReader, RegisteredArchive } from "./evidence-mcp.js";

export const WORKER_CONFIG = "artifacts/review-workbench/worker-config.private.json";
interface WorkerConfig { runtime_database_url: string; model: RuntimeConfig["model"] }
interface Credential { run_id: string; token: string }
export interface GraphLaunch { runtime_origin: string; run_id: string; token_file: string; output_file: string }
export type GraphRunner = (launch: GraphLaunch) => Promise<void>;

/** Worker → 同一 Python ReAct Graph → scoped Runtime HTTP；子进程从不取得任何数据库凭据。 */
export async function launchGraph(launch: GraphLaunch): Promise<void> {
  const executable = resolve("services/verify-agent/.venv/bin/python");
  const args = ["-m", "verify_agent", "--runtime", launch.runtime_origin, "--run-id", launch.run_id,
    "--token-file", launch.token_file, "--output", launch.output_file, "--max-iterations", "32"];
  // 避免继承调用者环境中的数据库、模型密钥或外部 tracing 出口。
  const env = { PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: process.env.HOME ?? "", LANG: "en_US.UTF-8",
    PYTHONPATH: resolve("services/verify-agent/src"), LANGSMITH_TRACING: "false", LANGCHAIN_TRACING_V2: "false" };
  await new Promise<void>((done, reject) => {
    const child = spawn(executable, args, { cwd: resolve("."), env, stdio: ["ignore", "ignore", "ignore"] });
    let forced: ReturnType<typeof setTimeout> | undefined;
    const timeout = setTimeout(() => { child.kill("SIGTERM"); forced = setTimeout(() => child.kill("SIGKILL"), 5_000); }, 25 * 60_000);
    child.once("error", () => { clearTimeout(timeout); if (forced) clearTimeout(forced); reject(new Error("GRAPH_PROCESS_START_FAILED")); });
    child.once("exit", (code) => {
      clearTimeout(timeout);
      if (forced) clearTimeout(forced);
      // exit=2 也可能是正常 BLOCKED；事实由 Runtime projection 决定，不能按进程码伪造结果。
      if (code === 0 || code === 2) done(); else reject(new Error("GRAPH_PROCESS_INTERRUPTED"));
    });
  });
}

/**
 * Workbench outbox → 稳定映射/预算预留 → Runtime acceptRun → Graph → Runtime 结果 → inbox。
 * 两库之间没有分布式事务。所有重试复用已冻结的 run/token/config；重复结果由 inbox 去重。
 * UNKNOWN 不释放父预算；外部调用前再核对租约/撤回，转派本身不撤销正在调查的请求。
 */
export class EvidenceWorker {
  private readonly dispatch = new Map<string, WorkItem>();
  constructor(readonly store: WorkflowStore, readonly runtime: Runtime, readonly registry: RegisteredArchive,
    readonly runtimeOrigin: string, readonly workerId = `evidence-worker-${randomUUID()}`,
    readonly graph: GraphRunner = launchGraph, readonly privateRoot = resolve("artifacts/review-workbench/worker")) {}

  /** Runtime 在事务外每次派发前调用；拒绝时不会把迟到结果变成有效证据。 */
  async assertDispatchAllowed(runId: string): Promise<void> {
    const work = this.dispatch.get(runId);
    if (!work || !(await this.store.isDispatchable(work))) throw new Error("EVIDENCE_REQUEST_NO_LONGER_DISPATCHABLE");
  }

  private async credential(work: WorkItem): Promise<{ credential: Credential; path: string; tokenPath: string }> {
    await mkdir(this.privateRoot, { recursive: true, mode: 0o700 });
    const key = createHash("sha256").update(`${work.request_id}/${work.request_spec_revision}`).digest("hex");
    const path = resolve(this.privateRoot, `${key}.credential.private.json`);
    // 文件先于 mapping 落盘；崩溃后读回同一凭据，绝不悄悄生成第二个已派发身份。
    try { await writeFile(path, JSON.stringify({ run_id: randomUUID(), token: randomBytes(32).toString("hex") }), { flag: "wx", mode: 0o600 }); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
    const credential = JSON.parse(await privateText(path)) as Credential;
    if (!/^[0-9a-f-]{36}$/.test(credential.run_id) || !/^[0-9a-f]{64}$/.test(credential.token)) throw new Error("WORKER_CREDENTIAL_INVALID");
    const tokenPath = resolve(this.privateRoot, `${key}.token.private`);
    try { await writeFile(tokenPath, credential.token, { flag: "wx", mode: 0o600 }); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
    if ((await privateText(tokenPath)).trim() !== credential.token) throw new Error("WORKER_TOKEN_CONFLICT");
    return { credential, path, tokenPath };
  }

  /** 只从权威 Runtime 持久化投影装配回流；Graph 的 stdout 和退出码不能签发响应。 */
  private result(request: RequestSnapshot, runId: string, projection: Record<string, unknown>): RuntimeResult {
    const operations = projection.operations as { status: string; result: Record<string, unknown> | null }[];
    const completed = operations.find(op => op.status === "COMPLETED" && op.result?.evidence_response);
    const uncertain = (projection.model_calls as { status: string }[]).some(c => ["UNKNOWN", "DISPATCHED", "RESERVED"].includes(c.status)) || operations.some(op => ["UNKNOWN", "DISPATCHED", "QUEUED"].includes(op.status));
    const state: RuntimeResult["execution_state"] = uncertain || projection.status === "ACTIVE" ? "UNKNOWN" : completed ? "FINISHED" : projection.status === "CANCELLED" ? "CANCELLED" : "BLOCKED";
    const clarification = operations.find(op => op.result?.action_kind === "CLARIFY")?.result?.proposal as { question?: unknown } | undefined;
    return { request_id: request.id, request_spec_revision: request.request_spec_revision, run_id: runId,
      input_digest: request.input_digest, execution_state: state, runtime_event_seq: Number(projection.event_seq),
      consumed: { model_calls: Number(projection.model_calls_used), read_calls: Number(projection.reads_used) },
      read_receipts: projection.read_receipts as Record<string, unknown>[],
      ...(completed ? { response: completed.result!.evidence_response as EvidenceResponse } : {}),
      ...(projection.stop_reason === "CLARIFICATION_REQUIRED" ? { clarification: typeof clarification?.question === "string" ? clarification.question : "请补充请求所需的业务依据。" } : {}),
    };
  }

  /** 回流失败后的有界只读对账：不重新派发模型/MCP，不生成凭据，不增加预算。 */
  async reconcileOnce(): Promise<Record<string, unknown> | null> {
    const work = await this.store.claimReconciliationWork(this.workerId, 45_000);
    if (!work) return null;
    try {
      const request = await this.store.loadRequest(work.request_id, work.request_spec_revision);
      if (!request.mapping) throw new Error("RECONCILIATION_MAPPING_MISSING");
      const projection = await this.runtime.reconcileProjection(request.mapping.run_id);
      const result = this.result(request, request.mapping.run_id, projection);
      if (result.execution_state === "UNKNOWN") {
        await this.store.failWork(work, "RUNTIME_RESULT_STILL_UNKNOWN", true);
        return { request_id: request.id, execution_state: "UNKNOWN", reconciliation: "NO_REDISPATCH" };
      }
      const application = await this.store.applyRuntimeResult(`runtime:${request.mapping.run_id}:${projection.event_seq}`, result);
      return { request_id: request.id, run_id: request.mapping.run_id, execution_state: result.execution_state, reconciliation: "RECOVERED_WITHOUT_REDISPATCH", application };
    } catch {
      await this.store.failWork(work, "RECONCILIATION_UNAVAILABLE", true);
      return { request_id: work.request_id, execution_state: "UNKNOWN", reconciliation: "SOURCE_UNAVAILABLE" };
    }
  }

  async runOnce(): Promise<Record<string, unknown> | null> {
    const work = await this.store.claimWork(this.workerId, 45_000);
    if (!work) return null;
    let runId: string | null = null, heartbeatFailed = false;
    const heartbeat = setInterval(() => {
      void this.store.renewWork(work, 45_000).then(ok => { if (!ok) heartbeatFailed = true; }).catch(() => { heartbeatFailed = true; });
    }, 10_000);
    heartbeat.unref();
    try {
      const request = await this.store.loadRequest(work.request_id, work.request_spec_revision);
      const { credential, path, tokenPath } = await this.credential(work);
      const limits: Limits = { model_calls: 6, reads: 4, no_progress: 2, plan_repairs: 0 };
      const mapping: RunMapping = request.mapping ?? {
        request_id: request.id, request_spec_revision: work.request_spec_revision, run_id: credential.run_id,
        frozen_input_digest: request.input_digest, execution_profile: "EVIDENCE_REVIEW_V2_MCP",
        config_digest: this.runtime.configDigest, limits: { ...limits }, credential_ref: path,
        allocation: { model_calls: limits.model_calls, read_calls: limits.reads }, consumed: { model_calls: 0, read_calls: 0 }, settlement: "RESERVED",
      };
      if (mapping.run_id !== credential.run_id || mapping.credential_ref !== path || mapping.config_digest !== this.runtime.configDigest) throw new Error("FROZEN_RUN_CONFIGURATION_CONFLICT");
      await this.store.reserveRunMapping(work, mapping);
      runId = mapping.run_id;
      this.dispatch.set(runId, work);
      if (heartbeatFailed) throw new Error("WORKER_LEASE_LOST");
      await this.assertDispatchAllowed(runId);
      this.registry.register(runId, request.input.materials);
      await this.runtime.acceptRun(request.input, credential.token, mapping.limits as unknown as Limits, runId);
      let projection = await this.runtime.projection(runId);
      if (projection.status === "ACTIVE") {
        const output = resolve(this.privateRoot, `${runId}-${work.attempts}-${randomUUID()}.graph.private.json`);
        await this.graph({ runtime_origin: this.runtimeOrigin, run_id: runId, token_file: tokenPath, output_file: output });
        projection = await this.runtime.projection(runId);
      }
      const result = this.result(request, runId, projection);
      const applied = await this.store.applyRuntimeResult(`runtime:${runId}:${projection.event_seq}`, result);
      return { request_id: request.id, run_id: runId, execution_state: result.execution_state, response_proposed: Boolean(result.response), consumed: result.consumed, application: applied };
    } catch (error) {
      // 若 Run 已创建，不能推断模型尚未执行。保持预留，等待相同 Run 对账；不启动替代 Run。
      const code = error instanceof Error && /^[A-Z_]+$/.test(error.message) ? error.message : "WORKER_EXECUTION_FAILED";
      await this.store.failWork(work, code, runId !== null, runId === null && /CONFLICT|INVALID|EXHAUSTED|BINDING|INTEGRITY/.test(code));
      return { request_id: work.request_id, execution_state: runId ? "UNKNOWN" : "BLOCKED", error_code: code };
    } finally { clearInterval(heartbeat); if (runId) this.dispatch.delete(runId); }
  }
}

/** 独立 Worker 启动；CLI --once 可审阅一次派发结果，默认循环仅消费已有人提交的请求。 */
export async function workerMain(): Promise<void> {
  const web = await loadConfig();
  const config = JSON.parse(await privateText(WORKER_CONFIG)) as WorkerConfig;
  assertLocalDatabase(config.runtime_database_url);
  if (config.runtime_database_url === web.database_url) throw new Error("SEPARATE_RUNTIME_DATABASE_REQUIRED");
  const review = new ReviewStore(web.database_url), store = new WorkflowStore(review.pool);
  const registry = new RegisteredArchive(), mcpToken = randomBytes(32).toString("hex");
  const mcp = createArchiveMcpServer(registry, mcpToken);
  await mcp.listen({ host: "127.0.0.1", port: 0 });
  const mcpAddress = mcp.server.address();
  if (!mcpAddress || typeof mcpAddress === "string") throw new Error("MCP_START_FAILED");
  let worker: EvidenceWorker;
  const runtime = new Runtime({ databaseUrl: config.runtime_database_url, model: config.model,
    plannerPrompt: await readFile("services/verify-agent/planner-prompt.txt", "utf8"),
    evidenceReadPort: new McpArchiveReader(`http://127.0.0.1:${mcpAddress.port}/mcp`, mcpToken),
    assertEvidenceDispatchAllowed: id => worker.assertDispatchAllowed(id) });
  const api = createVerifyServer(runtime);
  let stop = false;
  const onStop = (): void => { stop = true; };
  process.once("SIGINT", onStop); process.once("SIGTERM", onStop);
  try {
    await store.migrate(); await runtime.store.migrate();
    await api.listen({ host: "127.0.0.1", port: 0 });
    const address = api.server.address();
    if (!address || typeof address === "string") throw new Error("RUNTIME_START_FAILED");
    worker = new EvidenceWorker(store, runtime, registry, `http://127.0.0.1:${address.port}`);
    process.stdout.write(JSON.stringify({ event: "evidence_worker_ready", scope: "LOCAL_ARCHIVED_EVIDENCE", protocol: "MCP_2025_06_18_JSON" }) + "\n");
    do {
      const result = await worker.reconcileOnce() ?? await worker.runOnce();
      if (result) process.stdout.write(JSON.stringify(result) + "\n");
      if (process.argv.includes("--once")) break;
      if (!stop) await new Promise(done => setTimeout(done, 1_000));
    } while (!stop);
  } finally {
    process.off("SIGINT", onStop); process.off("SIGTERM", onStop);
    await api.close(); await mcp.close(); await runtime.close(); await review.close();
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void workerMain().catch(() => { process.stderr.write("Evidence worker startup failed; inspect private local configuration.\n"); process.exitCode = 1; });
}
