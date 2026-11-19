import Fastify from "fastify";
import { createHash, timingSafeEqual } from "node:crypto";
import type { EvidenceMaterial } from "@egap/contracts";

const VERSION = "2025-06-18";
const digest = (v: string): string => createHash("sha256").update(v).digest("hex");
export interface ArchiveRead {
  run_id: string; operation_id: string; material_ref: string; sha256: string; start_line: number; end_line: number;
}
export interface ArchiveReadResult {
  material_ref: string; sha256: string; start_line: number; end_line: number; content: string; segment_sha256: string;
}
const readSchema = { type: "object", additionalProperties: false,
  required: ["run_id", "operation_id", "material_ref", "sha256", "start_line", "end_line"], properties: {
    run_id: { type: "string" }, operation_id: { type: "string" }, material_ref: { type: "string" },
    sha256: { type: "string", pattern: "^[a-f0-9]{64}$" }, start_line: { type: "integer", minimum: 1 }, end_line: { type: "integer", minimum: 1 },
  } };

/**
 * Worker 仅登记已由 Workbench 授权的冻结 Run 材料；MCP 不读任意路径，也不写审查决定。
 * 缓存按材料字节/范围而非模型自选 nonce；不把工作流状态混进永不过期的材料缓存。
 */
export class RegisteredArchive {
  private readonly scopes = new Map<string, Map<string, EvidenceMaterial>>();
  private readonly cache = new Map<string, ArchiveReadResult>();
  register(runId: string, materials: EvidenceMaterial[]): void {
    const next = new Map(materials.map(m => [m.material_ref, structuredClone(m)]));
    if (next.size !== materials.length || materials.some(m => digest(m.content) !== m.sha256)) throw new Error("INVALID_ARCHIVE_REGISTRATION");
    const prior = this.scopes.get(runId);
    if (prior && JSON.stringify([...prior]) !== JSON.stringify([...next])) throw new Error("ARCHIVE_SCOPE_CONFLICT");
    this.scopes.set(runId, next);
  }
  list(runId: string): object[] {
    const materials = this.scopes.get(runId);
    if (!materials) throw new Error("ARCHIVE_SCOPE_DENIED");
    return [...materials.values()].map(({ content, ...m }) => ({ ...m, line_count: content.split("\n").length }));
  }
  read(input: ArchiveRead): ArchiveReadResult {
    const keys = ["run_id", "operation_id", "material_ref", "sha256", "start_line", "end_line"];
    if (!input || typeof input !== "object" || Object.keys(input).length !== keys.length || keys.some(k => !(k in input)) ||
      typeof input.run_id !== "string" || typeof input.operation_id !== "string" || !input.operation_id || typeof input.material_ref !== "string" ||
      typeof input.sha256 !== "string" || !Number.isSafeInteger(input.start_line) || !Number.isSafeInteger(input.end_line)) throw new Error("INVALID_READ_ARGUMENTS");
    const m = this.scopes.get(input.run_id)?.get(input.material_ref);
    if (!m || m.sha256 !== input.sha256) throw new Error("ARCHIVE_SCOPE_DENIED");
    const lines = m.content.split("\n");
    if (input.start_line < 1 || input.end_line < input.start_line || input.end_line > lines.length || input.end_line - input.start_line >= 200) throw new Error("ARCHIVE_RANGE_REJECTED");
    const key = `${m.material_ref}/${m.sha256}/${input.start_line}/${input.end_line}`;
    const cached = this.cache.get(key);
    if (cached) return structuredClone(cached);
    const content = lines.slice(input.start_line - 1, input.end_line).join("\n");
    if (Buffer.byteLength(content) > 65_536) throw new Error("ARCHIVE_RANGE_TOO_LARGE");
    const result = { material_ref: m.material_ref, sha256: m.sha256, start_line: input.start_line,
      end_line: input.end_line, content, segment_sha256: digest(content) };
    this.cache.set(key, result); return structuredClone(result);
  }
}

/**
 * 受限 MCP Streamable HTTP 服务：initialize / tools/list / tools/call；JSON 响应、无 SSE。
 * MCP 不是授权来源；仅受信 Worker 的 bearer 可读取其登记范围，浏览器凭据不兼容。
 * Agent READ → Runtime outbox → McpArchiveReader → 本服务 → Runtime 收据；没有人工接受工具。
 */
export function createArchiveMcpServer(registry: RegisteredArchive, token: string) {
  if (token.length < 32) throw new Error("MCP_PRIVATE_TOKEN_REQUIRED");
  const app = Fastify({ logger: false, bodyLimit: 16_384, requestTimeout: 10_000 });
  app.addHook("onRequest", async (request, reply) => {
    const host = request.headers.host ?? "";
    if (!/^127\.0\.0\.1:\d+$/.test(host) || (request.headers.origin !== undefined && request.headers.origin !== `http://${host}`)) {
      return reply.code(403).send({ error: "ORIGIN_REJECTED" });
    }
    const actual = createHash("sha256").update(request.headers.authorization ?? "").digest();
    const expected = createHash("sha256").update(`Bearer ${token}`).digest();
    if (!timingSafeEqual(actual, expected)) return reply.code(403).send({ error: "ACCESS_DENIED" });
    reply.header("cache-control", "no-store");
  });
  app.get("/mcp", async (_request, reply) => reply.code(405).header("allow", "POST").send());
  app.post("/mcp", async (request, reply) => {
    const msg = request.body as { jsonrpc?: string; id?: string | number; method?: string; params?: Record<string, unknown> } | null;
    const error = (code: number, message: string) => ({ jsonrpc: "2.0", id: msg?.id ?? null, error: { code, message } });
    if (!msg || Array.isArray(msg) || msg.jsonrpc !== "2.0" || typeof msg.method !== "string") return reply.code(400).send(error(-32600, "Invalid Request"));
    if (msg.id === undefined) {
      if (msg.method === "notifications/initialized") return reply.code(202).send();
      return reply.code(400).send(error(-32600, "Unsupported notification"));
    }
    if (msg.method !== "initialize" && request.headers["mcp-protocol-version"] !== VERSION) return reply.code(400).send(error(-32600, "Protocol version required"));
    let result: unknown;
    if (msg.method === "initialize") {
      result = { protocolVersion: VERSION, capabilities: { tools: { listChanged: false } }, serverInfo: { name: "egap-registered-archive", version: "0.1.0" } };
    } else if (msg.method === "ping") result = {};
    else if (msg.method === "tools/list") result = { tools: [
      { name: "evidence.list_registered", description: "列出该 Run 已授权归档材料；不扩展来源范围。", inputSchema: {
        type: "object", additionalProperties: false, required: ["run_id"], properties: { run_id: { type: "string" } } }, annotations: { readOnlyHint: true, openWorldHint: false } },
      { name: "evidence.read_registered", description: "读取冻结材料的确切行范围，返回字节摘要。", inputSchema: readSchema, annotations: { readOnlyHint: true, openWorldHint: false } },
    ] };
    else if (msg.method === "tools/call") {
      try {
        const args = msg.params?.arguments as Record<string, unknown> | undefined;
        let body: unknown;
        if (msg.params?.name === "evidence.read_registered") body = registry.read(args as unknown as ArchiveRead);
        else if (msg.params?.name === "evidence.list_registered" && args && Object.keys(args).length === 1 && typeof args.run_id === "string") body = { materials: registry.list(args.run_id) };
        else return error(-32602, "Unknown tool or arguments");
        result = { content: [{ type: "text", text: JSON.stringify(body) }], structuredContent: body, isError: false };
      } catch (e) { result = { content: [{ type: "text", text: e instanceof Error ? e.message : "READ_FAILED" }], isError: true }; }
    } else return error(-32601, "Method not found");
    return { jsonrpc: "2.0", id: msg.id, result };
  });
  return app;
}

/** Runtime 的真实 HTTP read port；重定向拒绝、有界超时，失败由 Runtime 记账，不隐式派生新 Run。 */
export class McpArchiveReader {
  private sequence = 0;
  private ready: Promise<void> | undefined;
  constructor(private readonly endpoint: string, private readonly token: string, private readonly timeoutMs = 5_000) {
    const url = new URL(endpoint);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.pathname !== "/mcp" || url.username || url.password || url.search || url.hash) throw new Error("LOCAL_MCP_REQUIRED");
  }
  private async call(method: string, params: object): Promise<Record<string, unknown>> {
    const id = ++this.sequence;
    const response = await fetch(this.endpoint, { method: "POST", redirect: "error", signal: AbortSignal.timeout(this.timeoutMs),
      headers: { authorization: `Bearer ${this.token}`, "content-type": "application/json", accept: "application/json, text/event-stream", "mcp-protocol-version": VERSION },
      body: JSON.stringify({ jsonrpc: "2.0", id, method, params }) });
    if (!response.ok) throw new Error("MCP_TRANSPORT_FAILED");
    // 该适配器绑定自有 JSON-only server，不宣称是可接任意 SSE 服务的通用 MCP SDK。
    const reader = response.body?.getReader();
    if (!reader) throw new Error("MCP_EMPTY_RESPONSE");
    const chunks: Uint8Array[] = []; let size = 0;
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > 131_072) { await reader.cancel(); throw new Error("MCP_RESPONSE_TOO_LARGE"); }
      chunks.push(part.value);
    }
    const raw = Buffer.concat(chunks).toString("utf8");
    const value = JSON.parse(raw) as { id: number; error?: unknown; result?: Record<string, unknown> };
    if (value.id !== id || value.error || !value.result) throw new Error("MCP_PROTOCOL_FAILED");
    return value.result;
  }
  async read(input: ArchiveRead): Promise<ArchiveReadResult> {
    this.ready ??= (async () => {
      const init = await this.call("initialize", { protocolVersion: VERSION, capabilities: {}, clientInfo: { name: "egap-runtime", version: "0.1.0" } });
      if (init.protocolVersion !== VERSION) throw new Error("MCP_VERSION_MISMATCH");
      const notified = await fetch(this.endpoint, { method: "POST", redirect: "error", signal: AbortSignal.timeout(this.timeoutMs),
        headers: { authorization: `Bearer ${this.token}`, "content-type": "application/json", accept: "application/json, text/event-stream", "mcp-protocol-version": VERSION },
        body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) });
      if (notified.status !== 202) throw new Error("MCP_INITIALIZATION_FAILED");
    })().catch(e => { this.ready = undefined; throw e; });
    await this.ready;
    const result = await this.call("tools/call", { name: "evidence.read_registered", arguments: input });
    if (result.isError || !result.structuredContent) throw new Error("MCP_READ_REJECTED");
    const read = result.structuredContent as ArchiveReadResult;
    if (read.material_ref !== input.material_ref || read.sha256 !== input.sha256 || read.start_line !== input.start_line ||
      read.end_line !== input.end_line || typeof read.content !== "string" || digest(read.content) !== read.segment_sha256) throw new Error("MCP_READ_BINDING_MISMATCH");
    return read;
  }
}
