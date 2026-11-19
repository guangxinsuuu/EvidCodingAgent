import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { loadConfig } from "./config.js";
import { loadHistoricalCase } from "./importer.js";
import { createReviewServer } from "./server.js";
import { ReviewStore } from "./store.js";
import { WorkflowStore } from "./workflow-store.js";
import { loadEvidenceArchive } from "./evidence-archive.js";

/**
 * 启动：私密配置 → v1/v2 审查 schema → 固定历史材料/原件登记 → 同源 HTTP。
 * Web 只登记请求，独立 evidence-worker 消费；页面刷新不触发模型或 Runtime 对账。
 */
export async function main(): Promise<void> {
  const config = await loadConfig(process.argv[2]);
  const store = new ReviewStore(config.database_url);
  const workflow = new WorkflowStore(store.pool);
  const origin = `http://127.0.0.1:${config.port}`;
  const app = createReviewServer(store, { origin, identities: config.identities, workflow });
  try {
    await store.migrate();
    await workflow.migrate();
    const legacy = await loadHistoricalCase(resolve("."));
    await store.importCase(legacy);
    const actors = config.identities.map(i => i.actor).filter(a => a.tenant_id === legacy.tenant_id);
    await workflow.seedCase({ legacy, archive: await loadEvidenceArchive(resolve(".")), actors,
      owner_id: actors.find(a => a.role === "BUSINESS")!.id, qa_assignee_id: actors.find(a => a.role === "QA")!.id });
    await app.listen({ host: "127.0.0.1", port: config.port });
  } catch (error) { await app.close(); await store.close(); throw error; }
  let closing = false;
  const stop = async (): Promise<void> => {
    if (closing) return; closing = true;
    const deadline = setTimeout(() => process.exit(1), 5_000); deadline.unref();
    try { await app.close(); await store.close(); } finally { clearTimeout(deadline); }
  };
  process.once("SIGINT", () => { void stop(); });
  process.once("SIGTERM", () => { void stop(); });
  process.stdout.write(JSON.stringify({ event: "listening", address: origin, scope: "LOCAL_HISTORICAL_REVIEW", agent_dispatch: false }) + "\n");
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void main().catch(() => { process.stderr.write("Review workbench startup failed; verify private configuration, database, and pinned report.\n"); process.exitCode = 1; });
}
