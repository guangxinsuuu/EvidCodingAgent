import { createHash } from "node:crypto";
import { parseEnvironmentInstance, parseSupportJob, parseSupportRequest, parseSupportSubmission } from "@egap/contracts";
import type { EnvironmentInstance, SupportJob, SupportRequest, SupportSubmission } from "@egap/contracts";
import { syntheticSupportFixture } from "./fixtures.js";
import type { SyntheticSupportFixture } from "./fixtures.js";

export type ApiErrorCode = "INVALID_ENVIRONMENT" | "INVALID_SESSION" | "ENVIRONMENT_EXPIRED" | "ENVIRONMENT_REJECTED" |
  "GUARD_TIMEOUT" | "REQUEST_TIMEOUT" | "TRANSPORT_ERROR" | "HTTP_ERROR" | "INVALID_RESPONSE" | "EVIDENCE_FAILED" | "UNEXPECTED_ACCEPTANCE";
/** 仅固定错误码跨出适配器，响应正文和 bearer 永不进入异常文本。 */
export class GenaiSupportApiError extends Error {
  constructor(readonly code: ApiErrorCode, readonly httpStatus: number | null = null) {
    super(`GenAI support API operation failed: ${code}.`); this.name = "GenaiSupportApiError";
  }
}
export interface ApiObservation {
  readonly kind: "genai-api-smoke";
  readonly evidence_scope: "calibration-only";
  readonly execution_mode: "scripted-fake" | "live";
  readonly environment_instance_id: string;
  readonly fixture_reset_id: string;
  readonly lease_epoch: number;
  readonly observed_at: string;
  readonly service: "support-gateway";
  readonly operation: "submit-fixture" | "get-job" | "health";
  readonly http_status: number | null;
  readonly outcome: "OBSERVED" | "NOT_FOUND" | "REJECTED_INPUT" | "ERROR";
  readonly error_code: ApiErrorCode | null;
  readonly request: Readonly<Record<string, string | number>>;
  /** 只保存形状/关联和摘要；不保留回复正文、Token 或任意错误内容。 */
  readonly response: Readonly<Record<string, string | number | boolean | null>>;
}
export type EnvironmentGuard = (environment: Readonly<EnvironmentInstance>) => Promise<void>;
export type EvidenceSink = (observation: Readonly<ApiObservation>) => Promise<void>;
export interface AdapterOptions {
  readonly environment: EnvironmentInstance;
  readonly expectedEnvironmentId: string;
  readonly expectedLeaseEpoch: number;
  /** trusted controller 提供会话；expectedTenantId 仅校验响应，不能替代服务端授权。 */
  readonly session: { readonly expectedTenantId: string; readonly bearerToken: string };
  readonly executionMode: "scripted-fake" | "live";
  readonly guard: EnvironmentGuard;
  readonly evidenceSink: EvidenceSink;
  readonly requestTimeoutMs?: number;
}
export type Submission = { readonly accepted: true; readonly submission: SupportSubmission } |
  { readonly accepted: false; readonly reason: "INVALID_INPUT" };
export function boundedMs(value: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) throw new TypeError("Invalid bounded duration.");
  return value;
}
function jobId(value: string): string {
  if (typeof value !== "string" || !/^[a-f0-9]{32}$/.test(value) || value.length !== 32) throw new TypeError("Invalid support job ID.");
  return value;
}

/**
 * runSupportSmoke → GenaiSupportApi → 每次实查环境 guard → 有界 HTTP → evidenceSink。
 * 实现自有 DRAFT API 客户端，不假称上游已有这些路由；无自动 POST 重试。
 * HTTP 自述字段不是可信 Tool Trace，也不证明业务正确或持久性。
 */
export class GenaiSupportApi {
  readonly environment: Readonly<EnvironmentInstance>;
  readonly #origin: string;
  readonly #guard: EnvironmentGuard;
  readonly #sink: EvidenceSink;
  readonly #timeout: number;
  readonly #tenant: string;
  readonly #bearer: string;
  readonly #mode: AdapterOptions["executionMode"];

  constructor(options: AdapterOptions) {
    try { this.environment = parseEnvironmentInstance(options.environment); }
    catch { throw new GenaiSupportApiError("INVALID_ENVIRONMENT"); }
    if (this.environment.environment_instance_id !== options.expectedEnvironmentId ||
        this.environment.lease_epoch !== options.expectedLeaseEpoch) throw new GenaiSupportApiError("INVALID_ENVIRONMENT");
    const input = this.environment.endpoints["support-gateway"];
    if (!input) throw new GenaiSupportApiError("INVALID_ENVIRONMENT");
    const url = new URL(input);
    if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
        !url.port || Number(url.port) < 1024 || Number(url.port) > 65535 || url.pathname !== "/" ||
        url.username || url.password || url.search || url.hash) throw new GenaiSupportApiError("INVALID_ENVIRONMENT");
    const session = options.session;
    if (!session || typeof session.expectedTenantId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(session.expectedTenantId) ||
        /\s/.test(session.expectedTenantId) || typeof session.bearerToken !== "string" ||
        !/^[A-Za-z0-9._~-]{1,4096}$/.test(session.bearerToken) || /\s/.test(session.bearerToken) ||
        !["scripted-fake", "live"].includes(options.executionMode)) throw new GenaiSupportApiError("INVALID_SESSION");
    this.#origin = url.origin; this.#guard = options.guard; this.#sink = options.evidenceSink;
    this.#tenant = session.expectedTenantId; this.#bearer = session.bearerToken; this.#mode = options.executionMode;
    this.#timeout = boundedMs(options.requestTimeoutMs ?? 5000, 30_000); this.#checkExpiry();
  }
  #checkExpiry(): void {
    if (Date.now() >= Date.parse(this.environment.expires_at)) throw new GenaiSupportApiError("ENVIRONMENT_EXPIRED");
  }
  async #emit(observation: Pick<ApiObservation, "operation" | "http_status" | "outcome" | "error_code" | "request" | "response">): Promise<void> {
    try {
      await this.#sink(Object.freeze({ kind: "genai-api-smoke", evidence_scope: "calibration-only", execution_mode: this.#mode,
        environment_instance_id: this.environment.environment_instance_id, fixture_reset_id: this.environment.fixture_reset_id,
        lease_epoch: this.environment.lease_epoch, observed_at: new Date().toISOString(), service: "support-gateway",
        ...observation, request: Object.freeze({ ...observation.request }), response: Object.freeze({ ...observation.response }) }));
    } catch { throw new GenaiSupportApiError("EVIDENCE_FAILED"); }
  }
  async #request<T>(operation: ApiObservation["operation"], method: "GET" | "POST", path: string,
    request: ApiObservation["request"], parse: (value: unknown) => T, summary: (value: T) => ApiObservation["response"],
    options: { body?: string; absent?: 404 | 422; timeoutMs?: number } = {}): Promise<T | null> {
    let status: number | null = null;
    let value: T | null;
    const timeout = Math.min(this.#timeout, boundedMs(options.timeoutMs ?? this.#timeout, 30_000));
    const deadline = performance.now() + timeout;
    try {
      this.#checkExpiry();
      let guardTimer: ReturnType<typeof setTimeout> | undefined;
      try {
        // 迟到的 guard 只能结束自己的读取，不能在超时后继续派发 HTTP。
        await Promise.race([this.#guard(this.environment).catch(() => { throw new GenaiSupportApiError("ENVIRONMENT_REJECTED"); }),
          new Promise<never>((_resolve, reject) => { guardTimer = setTimeout(() => reject(new GenaiSupportApiError("GUARD_TIMEOUT")), timeout); })]);
      } finally { clearTimeout(guardTimer); }
      this.#checkExpiry();
      const remaining = Math.floor(deadline - performance.now());
      if (remaining < 1) throw new GenaiSupportApiError("GUARD_TIMEOUT");
      const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), remaining);
      try {
        const response = await fetch(`${this.#origin}${path}`, { method, redirect: "manual", signal: controller.signal,
          headers: { accept: "application/json", authorization: `Bearer ${this.#bearer}`,
            ...(options.body === undefined ? {} : { "content-type": "application/json" }) },
          ...(options.body === undefined ? {} : { body: options.body }) });
        status = response.status;
        // 负例的202本身已违反拒绝协议，即使其body回显revision=0或根本无效也已确定。
        // 不用正例Schema把这个已知违反降为INCONCLUSIVE；任务身份未知，交上层对账。
        if (method === "POST" && options.absent === 422 && status === 202) {
          await response.body?.cancel(); throw new GenaiSupportApiError("UNEXPECTED_ACCEPTANCE", status);
        }
        if (status === options.absent) { await response.body?.cancel(); value = null; }
        else {
          if (status !== (method === "POST" ? 202 : 200)) {
            await response.body?.cancel(); throw new GenaiSupportApiError("HTTP_ERROR", status);
          }
          if (response.headers.get("content-type")?.split(";")[0]?.trim() !== "application/json") {
            await response.body?.cancel(); throw new GenaiSupportApiError("INVALID_RESPONSE", status);
          }
          const reader = response.body?.getReader(); if (!reader) throw new GenaiSupportApiError("INVALID_RESPONSE", status);
          const chunks: Uint8Array[] = []; let size = 0;
          while (true) {
            const block = await reader.read(); if (block.done) break;
            size += block.value.byteLength;
            if (size > 65_536) { await reader.cancel(); throw new GenaiSupportApiError("INVALID_RESPONSE", status); }
            chunks.push(block.value);
          }
          try { value = parse(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
          catch { throw new GenaiSupportApiError("INVALID_RESPONSE", status); }
        }
        this.#checkExpiry();
        if (performance.now() >= deadline) throw new GenaiSupportApiError("REQUEST_TIMEOUT", status);
      } catch (error) {
        if (error instanceof GenaiSupportApiError) throw error;
        throw new GenaiSupportApiError(controller.signal.aborted ? "REQUEST_TIMEOUT" : "TRANSPORT_ERROR", status);
      } finally { clearTimeout(timer); }
    } catch (error) {
      const safe = error instanceof GenaiSupportApiError ? error : new GenaiSupportApiError("ENVIRONMENT_REJECTED");
      await this.#emit({ operation, http_status: status, outcome: "ERROR", error_code: safe.code, request,
        response: safe.code === "UNEXPECTED_ACCEPTANCE" ? { job_identity: "UNKNOWN", reconciliation_required: true } : {} });
      throw safe;
    }
    await this.#emit({ operation, http_status: status, outcome: value === null ?
      options.absent === 422 ? "REJECTED_INPUT" : "NOT_FOUND" : "OBSERVED", error_code: null, request,
      response: value === null ? {} : summary(value) });
    return value;
  }
  /** 请求只含 fixture 工单引用；仅负例接受422，认证失败/500不会伪装成正常输入拒绝。 */
  async submitFixture(kind: SyntheticSupportFixture): Promise<Submission> {
    const fixture = syntheticSupportFixture(kind);
    const result = await this.#request("submit-fixture", "POST", "/support-resolution-jobs",
      { fixture: kind, payload_sha256: fixture.sha256 }, (value) => {
        const row = parseSupportSubmission(value);
        if (row.tenant_id !== this.#tenant || row.ticket_id !== fixture.expectedTicket.ticket_id ||
            row.ticket_revision !== fixture.expectedTicket.ticket_revision) throw new TypeError("Mismatched submission identity.");
        return row;
      }, (row) => ({ job_id: row.job_id, ticket_id: row.ticket_id, ticket_revision: row.ticket_revision, status: row.status }),
      { body: fixture.body, ...(kind === "invalid-ticket-revision" ? { absent: 422 as const } : {}) });
    return result ? { accepted: true, submission: result } : { accepted: false, reason: "INVALID_INPUT" };
  }
  /** GET 的工单/版本来自登记或 POST 回执，不能拿其他工单草稿填补当前结果。 */
  async getJob(id: string, expectedTicket: SupportRequest, timeoutMs?: number): Promise<SupportJob | null> {
    const checkedId = jobId(id); const ticket = parseSupportRequest(expectedTicket);
    return this.#request("get-job", "GET", `/support-resolution-jobs/${checkedId}`, { job_id: checkedId }, (value) => {
      const row = parseSupportJob(value);
      if (row.job_id !== checkedId || row.tenant_id !== this.#tenant || row.ticket_id !== ticket.ticket_id ||
          row.ticket_revision !== ticket.ticket_revision) throw new TypeError("Mismatched job identity.");
      return row;
    }, (row) => ({ job_id: row.job_id, ticket_id: row.ticket_id, ticket_revision: row.ticket_revision, status: row.status,
      draft_present: row.draft !== null, error_code: row.error_code,
      draft_sha256: row.draft ? createHash("sha256").update(JSON.stringify(row.draft)).digest("hex") : null }),
    { absent: 404, ...(timeoutMs === undefined ? {} : { timeoutMs }) });
  }
  /** 仅观察 API 存活，不把服务自报 health 当成真实 LLM/队列/持久化资格。 */
  async health(): Promise<void> {
    await this.#request("health", "GET", "/health", {}, (value) => {
      if (!value || typeof value !== "object" || Array.isArray(value) ||
          Object.keys(value).sort().join(",") !== "schema_version,status" ||
          (value as { schema_version?: unknown }).schema_version !== 1 ||
          (value as { status?: unknown }).status !== "ok") throw new TypeError("Invalid health response.");
      return true;
    }, () => ({ api: "reachable", llm: "not-observed", tool_trace: "not-observed" }));
  }
}
