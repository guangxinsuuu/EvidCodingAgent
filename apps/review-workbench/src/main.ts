import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { loadConfig } from "./config.js";
import { loadHistoricalCase } from "./importer.js";
import { createReviewServer } from "./server.js";
import { ReviewStore } from "./store.js";

/**
 * 启动：私密配置 → 审查 schema → 固定历史材料幂等导入 → 同源 HTTP。
 * 不启动客服实验，不调用模型，不消费 Runtime outbox；导入不会改写已有审查决定。
 */
export async function main(): Promise<void> {
  const config = await loadConfig(process.argv[2]);
  const store = new ReviewStore(config.database_url);
  const origin = `http://127.0.0.1:${config.port}`;
  const app = createReviewServer(store, { origin, identities: config.identities });
  try {
    await store.migrate();
    await store.importCase(await loadHistoricalCase(resolve(".")));
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
