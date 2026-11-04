import { createHash } from "node:crypto";
import { parseEnvironmentInstance } from "@egap/contracts";
import type { EnvironmentInstance } from "@egap/contracts";
import { syntheticFixture } from "./fixtures.js";
import type { SyntheticFixture } from "./fixtures.js";

export type JobStatus = "queued" | "processing" | "done" | "failed";
export type ApiErrorCode = "INVALID_ENVIRONMENT" | "ENVIRONMENT_EXPIRED" | "ENVIRONMENT_REJECTED" |
  "GUARD_TIMEOUT" | "REQUEST_TIMEOUT" | "TRANSPORT_ERROR" | "HTTP_ERROR" | "INVALID_RESPONSE" | "EVIDENCE_FAILED";

/** 固定错误码不拼接HTTP正文、底层异常或URL，避免将潜在秘密带入证据。 */
export class MlInferenceApiError extends Error {
  constructor(readonly code: ApiErrorCode, readonly httpStatus: number | null = null) {
    super(`ML inference API operation failed: ${code}.`);
    this.name = "MlInferenceApiError";
  }
}

export interface ApiObservation {
  readonly kind: "api-smoke";
  readonly evidence_scope: "calibration-only";
  readonly environment_instance_id: string;
  readonly fixture_reset_id: string;
  readonly lease_epoch: number;
  readonly observed_at: string;
  readonly service: "inference-gateway";
  readonly operation: "submit-fixture" | "get-prediction" | "health";
  readonly http_status: number | null;
  readonly outcome: "OBSERVED" | "NOT_FOUND" | "REJECTED_MEDIA_TYPE" | "ERROR";
  readonly error_code: ApiErrorCode | null;
  /** 只记录合成fixture类型/摘要、任务ID和结构摘要；不保存原图或任意错误文本。 */
  readonly request: Readonly<Record<string, string>>;
  readonly response: Readonly<Record<string, string | number | boolean | null>>;
}
/** 持锁调用方每次实查登记身份、epoch、容器ID/labels与实际端口；静态URL校验不能替代它。 */
export type EnvironmentGuard = (environment: Readonly<EnvironmentInstance>) => Promise<void>;
export type EvidenceSink = (observation: Readonly<ApiObservation>) => Promise<void>;
export interface AdapterOptions {
  readonly environment: EnvironmentInstance;
  readonly expectedEnvironmentId: string;
  readonly expectedLeaseEpoch: number;
  readonly guard: EnvironmentGuard;
  readonly evidenceSink: EvidenceSink;
  readonly requestTimeoutMs?: number;
}
export interface JobObservation {
  readonly id: string;
  readonly status: JobStatus;
  readonly predictions: readonly { readonly label: string; readonly score: number }[] | null;
  readonly error_present: boolean;
  readonly duration_ms: number | null;
  /** 固定上游API未返回模型身份，不能从环境默认配置推断每个任务实际使用的模型。 */
  readonly model_identity: "not-observed";
}
export type Submission = { readonly accepted: true; readonly id: string; readonly status: "queued" } |
  { readonly accepted: false; readonly status: "unsupported-media-type" };

export function boundedMs(value: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) throw new TypeError("Invalid bounded duration.");
  return value;
}
function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new TypeError("Invalid response object.");
  return value as Record<string, unknown>;
}
function jobId(value: unknown): string {
  if (typeof value !== "string" || value.length !== 32 || !/^[a-f0-9]{32}$/.test(value)) throw new TypeError("Invalid job ID.");
  return value;
}
function parseJob(value: unknown, expectedId: string): JobObservation {
  const row = object(value);
  if (jobId(row.id) !== expectedId || !["queued", "processing", "done", "failed"].includes(String(row.status))) throw new TypeError("Invalid job identity/state.");
  let predictions: JobObservation["predictions"] = null;
  if (row.predictions !== null && row.predictions !== undefined) {
    if (!Array.isArray(row.predictions) || row.predictions.length > 1000) throw new TypeError("Invalid predictions.");
    predictions = Object.freeze(row.predictions.map((item: unknown) => {
      const p = object(item);
      if (typeof p.label !== "string" || p.label.length < 1 || p.label.length > 512 ||
          typeof p.score !== "number" || !Number.isFinite(p.score) || p.score < 0 || p.score > 1) throw new TypeError("Invalid prediction.");
      return Object.freeze({ label: p.label, score: p.score });
    }));
  }
  const duration = row.duration_ms ?? null;
  if (duration !== null && (typeof duration !== "number" || !Number.isFinite(duration) || duration < 0)) throw new TypeError("Invalid duration.");
  if (row.error !== undefined && row.error !== null && typeof row.error !== "string") throw new TypeError("Invalid error field.");
  return Object.freeze({ id: expectedId, status: row.status as JobStatus, predictions,
    error_present: typeof row.error === "string" && row.error.length > 0,
    duration_ms: duration, model_identity: "not-observed" });
}

/**
 * runBaselineSmoke → MlInferenceApi → guard → 有界HTTP → evidenceSink。
 * API只开放固定路由与合成输入；POST超时后可能已入队，绝不自动重试。
 * 此适配器观察HTTP事实，不签发ML-01持久性或ML-02模型一致性结论。
 */
export class MlInferenceApi {
  readonly environment: Readonly<EnvironmentInstance>;
  readonly #origin: string;
  readonly #guard: EnvironmentGuard;
  readonly #sink: EvidenceSink;
  readonly #timeout: number;

  constructor(options: AdapterOptions) {
    try { this.environment = parseEnvironmentInstance(options.environment); }
    catch { throw new MlInferenceApiError("INVALID_ENVIRONMENT"); }
    if (this.environment.environment_instance_id !== options.expectedEnvironmentId ||
        this.environment.lease_epoch !== options.expectedLeaseEpoch) throw new MlInferenceApiError("INVALID_ENVIRONMENT");
    const input = this.environment.endpoints["inference-gateway"];
    if (!input) throw new MlInferenceApiError("INVALID_ENVIRONMENT");
    const url = new URL(input);
    if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
        !url.port || Number(url.port) < 1024 || Number(url.port) > 65535 || url.pathname !== "/" ||
        url.username || url.password || url.search || url.hash) throw new MlInferenceApiError("INVALID_ENVIRONMENT");
    this.#origin = url.origin; this.#guard = options.guard; this.#sink = options.evidenceSink;
    this.#timeout = boundedMs(options.requestTimeoutMs ?? 5000, 30_000);
    this.#checkExpiry();
  }

  #checkExpiry(): void {
    if (Date.now() >= Date.parse(this.environment.expires_at)) throw new MlInferenceApiError("ENVIRONMENT_EXPIRED");
  }

  async #emit(observation: Omit<ApiObservation, "kind" | "evidence_scope" | "environment_instance_id" |
    "fixture_reset_id" | "lease_epoch" | "observed_at" | "service">): Promise<void> {
    try {
      await this.#sink(Object.freeze({ kind: "api-smoke", evidence_scope: "calibration-only",
        environment_instance_id: this.environment.environment_instance_id,
        fixture_reset_id: this.environment.fixture_reset_id, lease_epoch: this.environment.lease_epoch,
        observed_at: new Date().toISOString(), service: "inference-gateway", ...observation,
        request: Object.freeze({ ...observation.request }), response: Object.freeze({ ...observation.response }) }));
    } catch { throw new MlInferenceApiError("EVIDENCE_FAILED"); }
  }

  async #request<T>(operation: ApiObservation["operation"], method: "GET" | "POST", path: string,
    request: ApiObservation["request"], parse: (value: unknown) => T,
    summary: (value: T) => ApiObservation["response"], options: {
      body?: FormData; absent?: 404 | 415; timeoutMs?: number;
    } = {}): Promise<T | null> {
    let status: number | null = null;
    let value: T | null;
    const timeout = Math.min(this.#timeout, boundedMs(options.timeoutMs ?? this.#timeout, 30_000));
    const deadline = performance.now() + timeout;
    try {
      this.#checkExpiry();
      let guardTimer: ReturnType<typeof setTimeout> | undefined;
      try {
        // 超时只结束本次调用；迟到的guard不能继续触发HTTP副作用。
        await Promise.race([this.#guard(this.environment).catch(() => { throw new MlInferenceApiError("ENVIRONMENT_REJECTED"); }),
          new Promise<never>((_resolve, reject) => { guardTimer = setTimeout(() => reject(new MlInferenceApiError("GUARD_TIMEOUT")), timeout); })]);
      } finally { clearTimeout(guardTimer); }
      this.#checkExpiry();
      const remaining = Math.floor(deadline - performance.now());
      if (remaining < 1) throw new MlInferenceApiError("GUARD_TIMEOUT");
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), remaining);
      try {
        const response = await fetch(`${this.#origin}${path}`, { method, redirect: "manual", signal: controller.signal,
          headers: { accept: "application/json" }, ...(options.body ? { body: options.body } : {}) });
        status = response.status;
        if (status === options.absent) {
          await response.body?.cancel(); value = null;
        } else {
          if (status !== (method === "POST" ? 202 : 200)) {
            await response.body?.cancel(); throw new MlInferenceApiError("HTTP_ERROR", status);
          }
          if (response.headers.get("content-type")?.split(";")[0]?.trim() !== "application/json") {
            await response.body?.cancel(); throw new MlInferenceApiError("INVALID_RESPONSE", status);
          }
          const chunks: Uint8Array[] = [];
          let size = 0;
          const reader = response.body?.getReader();
          if (!reader) throw new MlInferenceApiError("INVALID_RESPONSE", status);
          while (true) {
            const block = await reader.read(); if (block.done) break;
            size += block.value.byteLength;
            if (size > 65_536) { await reader.cancel(); throw new MlInferenceApiError("INVALID_RESPONSE", status); }
            chunks.push(block.value);
          }
          try { value = parse(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
          catch { throw new MlInferenceApiError("INVALID_RESPONSE", status); }
        }
        this.#checkExpiry();
        if (performance.now() >= deadline) throw new MlInferenceApiError("REQUEST_TIMEOUT", status);
      } catch (error) {
        if (error instanceof MlInferenceApiError) throw error;
        throw new MlInferenceApiError(controller.signal.aborted ? "REQUEST_TIMEOUT" : "TRANSPORT_ERROR", status);
      } finally { clearTimeout(timer); }
    } catch (error) {
      const safe = error instanceof MlInferenceApiError ? error : new MlInferenceApiError("ENVIRONMENT_REJECTED");
      await this.#emit({ operation, http_status: status, outcome: "ERROR", error_code: safe.code, request, response: {} });
      throw safe;
    }
    await this.#emit({ operation, http_status: status, outcome: value === null ?
      options.absent === 415 ? "REJECTED_MEDIA_TYPE" : "NOT_FOUND" : "OBSERVED",
      error_code: null, request, response: value === null ? {} : summary(value) });
    return value;
  }

  /** 固定三类合成fixture；415只有被明确允许的负例才能成为已观察拒绝。202不等于任务完成。 */
  async submitFixture(kind: SyntheticFixture): Promise<Submission> {
    const fixture = syntheticFixture(kind);
    const form = new FormData();
    form.append("file", new Blob([new Uint8Array(fixture.bytes)], { type: fixture.contentType }), fixture.filename);
    const accepted = await this.#request("submit-fixture", "POST", "/predictions",
      { fixture: kind, payload_sha256: fixture.sha256, content_type: fixture.contentType }, (value) => {
        const row = object(value); const id = jobId(row.id);
        if (row.status !== "queued") throw new TypeError("Submission must acknowledge queued state.");
        return { accepted: true as const, id, status: "queued" as const };
      }, (row) => ({ job_id: row.id, status: row.status }),
      { body: form, ...(kind === "unsupported-mime" ? { absent: 415 as const } : {}) });
    return accepted ?? { accepted: false, status: "unsupported-media-type" };
  }

  /** 404为缺失事实；503/超时/断链不等于未知任务。任意工作器错误文本不跨出本适配器。 */
  async getPrediction(id: string, timeoutMs?: number): Promise<JobObservation | null> {
    const checked = jobId(id);
    return this.#request("get-prediction", "GET", `/predictions/${checked}`, { job_id: checked },
      (value) => parseJob(value, checked), (row) => ({ job_id: row.id, status: row.status,
        prediction_count: row.predictions?.length ?? null, error_present: row.error_present,
        duration_ms: row.duration_ms, model_identity: row.model_identity,
        predictions_sha256: row.predictions === null ? null : createHash("sha256").update(JSON.stringify(row.predictions)).digest("hex") }),
      { absent: 404, ...(timeoutMs === undefined ? {} : { timeoutMs }) });
  }

  /** 上游health只证明Gateway可连接Redis；它没有检查Worker或ONNX模型，不作为推理PASS。 */
  async health(): Promise<void> {
    await this.#request("health", "GET", "/health", {}, (value) => {
      const row = object(value);
      if (row.status !== "ok" || row.redis !== true) throw new TypeError("Gateway/Redis health check did not pass.");
      return true;
    }, () => ({ gateway_redis: "reachable", worker_model: "not-observed" }));
  }
}
