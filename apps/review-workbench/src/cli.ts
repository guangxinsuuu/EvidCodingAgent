import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { privateText } from "./config.js";

/**
 * CLI → 同源登录 → 同一 API → 登出。它不导入 Store、不持数据库凭据，
 * 也不能选择任意 actor。写命令读取明确版本/幂等键的 JSON，网络不明时由操作者原样重放。
 * 用法：review:cli --code-file PRIVATE_FILE tasks|case ID|evidence ID|decision ID JSON_FILE|request ID JSON_FILE
 */
export async function cli(args: string[], origin = process.env["EGAP_REVIEW_ORIGIN"] ?? "http://127.0.0.1:4174"): Promise<void> {
  const [flag, codeFile, command, id, bodyFile] = args;
  const url = new URL(origin);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.pathname !== "/" || url.username || url.password || url.search || url.hash) throw new Error("LOCAL_ORIGIN_REQUIRED");
  if (flag !== "--code-file" || !codeFile || !command) throw new Error("CLI_ARGUMENTS_REQUIRED");
  const path = command === "tasks" ? "/api/v1/me/review-tasks" : command === "case" ? `/api/v1/review-cases/${encodeURIComponent(id ?? "")}`
    : command === "evidence" ? `/api/v1/evidence/${encodeURIComponent(id ?? "")}`
    : ["decision", "request"].includes(command) ? `/api/v1/review-cases/${encodeURIComponent(id ?? "")}/${command === "decision" ? "decisions" : "evidence-requests"}` : null;
  if (!path || (command !== "tasks" && !id)) throw new Error("INVALID_COMMAND");
  const mutation = command === "decision" || command === "request";
  const body = mutation && bodyFile ? JSON.stringify(JSON.parse(await readFile(bodyFile, "utf8"))) : undefined;
  if (mutation && !body) throw new Error("COMMAND_JSON_REQUIRED");
  const baseHeaders = { origin: url.origin, "content-type": "application/json" };
  const login = await fetch(`${url.origin}/api/v1/session`, { method: "POST", headers: baseHeaders,
    body: JSON.stringify({ code: (await privateText(codeFile)).trim() }), signal: AbortSignal.timeout(10_000), redirect: "error" });
  if (!login.ok) throw new Error(`LOGIN_HTTP_${login.status}`);
  const session = await login.json() as { csrf_token: string };
  const cookie = login.headers.get("set-cookie")?.split(";")[0];
  if (!cookie) throw new Error("SESSION_COOKIE_MISSING");
  const headers = { ...baseHeaders, cookie, "x-csrf-token": session.csrf_token };
  try {
    const response = await fetch(`${url.origin}${path}`, { method: mutation ? "POST" : "GET", headers,
      ...(body ? { body } : {}), signal: AbortSignal.timeout(10_000), redirect: "error" });
    const result = await response.json();
    process.stdout.write(JSON.stringify({ http_status: response.status, result }, null, 2) + "\n");
    if (!response.ok) process.exitCode = 1;
  } finally {
    await fetch(`${url.origin}/api/v1/logout`, { method: "POST", headers, body: "{}", signal: AbortSignal.timeout(5_000), redirect: "error" }).catch(() => undefined);
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void cli(process.argv.slice(2)).catch(() => { process.stderr.write("Review CLI failed; inspect arguments, private code file, and service availability.\n"); process.exitCode = 1; });
}
