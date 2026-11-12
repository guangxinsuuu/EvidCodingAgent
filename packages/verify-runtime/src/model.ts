import { RuntimeError, requireThat, type Json, type RuntimeConfig } from "./types.js";

/** 只有可信启动配置可指定模型地址；本版本只允许本机 Ollama，不自动下载或转云。 */
export function validateModelConfig(model: RuntimeConfig["model"]): void {
  const url = new URL(model.endpoint);
  requireThat(url.protocol === "http:" && url.hostname === "127.0.0.1" &&
    !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash,
  "LOCAL_MODEL_ENDPOINT_REQUIRED", 400);
  requireThat(/^[a-zA-Z0-9._:/-]{1,160}$/.test(model.name) && /^[a-f0-9]{64}$/.test(model.digest), "MODEL_IDENTITY_REQUIRED", 400);
  requireThat(Number.isInteger(model.timeoutMs) && model.timeoutMs >= 50 && model.timeoutMs <= 180_000 &&
    Number.isInteger(model.maxOutputTokens) && model.maxOutputTokens >= 128 && model.maxOutputTokens <= 3000,
  "MODEL_LIMITS_INVALID", 400);
}
/**
 * Runtime 的 DISPATCHED 提交 → 此单次 POST → 原始响应回执。网络不在 DB 事务内。
 * 不重试、不跟重定向；超时/截断/连接中断不能证明未执行，调用者记 UNKNOWN。
 */
export async function invokeLocalModel(config: RuntimeConfig["model"], requestText: string): Promise<string> {
  const tagsResponse = await fetch(new URL("/api/tags", config.endpoint), { redirect: "error", signal: AbortSignal.timeout(3000) });
  const tags = await tagsResponse.json() as { models?: { name: string; digest: string }[] };
  requireThat(tagsResponse.ok && tags.models?.some(m => m.name === config.name && m.digest === config.digest), "MODEL_DIGEST_DRIFT");
  const response = await fetch(new URL("/api/chat", config.endpoint), {
    method: "POST", headers: { "content-type": "application/json" }, body: requestText,
    redirect: "error", signal: AbortSignal.timeout(config.timeoutMs),
  });
  // 错误响应也归档；不把 Provider 4xx/5xx 当作没有发生过的免费请求。
  const reader = response.body?.getReader();
  if (!reader) throw new RuntimeError("MODEL_EMPTY_RESPONSE");
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const next = await reader.read();
    if (next.done) break;
    size += next.value.byteLength;
    if (size > 131_072) { await reader.cancel(); throw new RuntimeError("MODEL_RESPONSE_TOO_LARGE"); }
    chunks.push(next.value);
  }
  return Buffer.concat(chunks).toString("utf8");
}
