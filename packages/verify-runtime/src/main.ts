import { readFile, writeFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { Runtime } from "./runtime.js";
import { createVerifyServer } from "./server.js";
import { requireThat, type Limits, type RuntimeConfig } from "./types.js";

/**
 * operator 私有启动配置 → acceptRun（固定包和预算）→ 回环 HTTP → Python graph。
 * 重启同一 config/run_id 只恢复身份，不重新分配预算。配置/ready 文件不进入 Git。
 */
export async function main(argv = process.argv.slice(2)): Promise<void> {
  requireThat(argv.length === 2 && argv[0] === "--config", "USAGE_EXPECTED_CONFIG_FILE", 400);
  const path = argv[1]!;
  requireThat(((await stat(path)).mode & 0o077) === 0, "PRIVATE_CONFIG_MODE_REQUIRED", 400);
  const c = JSON.parse(await readFile(path, "utf8")) as {
    database_url: string; model: RuntimeConfig["model"]; prompt_path: string;
    bundle_path: string; run_id: string; planner_token: string; limits: Limits; port?: number; ready_path: string;
  };
  const runtime = new Runtime({ databaseUrl: c.database_url, model: c.model, plannerPrompt: await readFile(c.prompt_path, "utf8") });
  try {
    await runtime.store.migrate();
    await runtime.acceptRun(JSON.parse(await readFile(c.bundle_path, "utf8")), c.planner_token, c.limits, c.run_id);
    const app = createVerifyServer(runtime);
    const url = await app.listen({ host: "127.0.0.1", port: c.port ?? 0 });
    await writeFile(c.ready_path, JSON.stringify({ runtime: url, run_id: c.run_id, scope: "LOCAL_SYNTHETIC_PLANNING", pid: process.pid }, null, 2), { mode: 0o600 });
    process.stdout.write(JSON.stringify({ status: "READY", runtime: url, run_id: c.run_id }) + "\n");
    let closing = false;
    const close = () => { if (closing) return; closing = true; void app.close().finally(() => runtime.close()); };
    process.once("SIGINT", close); process.once("SIGTERM", close);
  } catch (error) { await runtime.close(); throw error; }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { process.stderr.write("Verify Runtime startup failed; check private configuration and database.\n"); process.exitCode = 1; });
}
