import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import type { EnvironmentInstance } from "@egap/contracts";
import { boundedMs, MlInferenceApi, MlInferenceApiError } from "./api.js";
import type { ApiErrorCode, EnvironmentGuard, EvidenceSink, JobObservation, JobStatus } from "./api.js";

export interface BaselineSmokeOptions {
  readonly environment: EnvironmentInstance;
  readonly guard: EnvironmentGuard;
  readonly evidenceSink: EvidenceSink;
  readonly requestTimeoutMs?: number;
  readonly jobTimeoutMs?: number;
  readonly pollIntervalMs?: number;
}
export type SmokeVerdict = "PASS" | "FAIL" | "INCONCLUSIVE";
export interface SmokeCaseResult {
  readonly case_id: "valid-image" | "invalid-image" | "unsupported-mime" | "unknown-job";
  readonly verdict: SmokeVerdict;
  readonly job_id: string | null;
  readonly observed_state: JobStatus | null;
  readonly reason: "EXPECTED_TERMINAL_SHAPE" | "UNEXPECTED_TERMINAL_SHAPE" | "EXPECTED_HTTP_REJECTION" |
    "UNEXPECTED_ACCEPTANCE" | "EXPECTED_NOT_FOUND" | "UNEXPECTED_EXISTING_JOB" | "JOB_NOT_FOUND" |
    "NONTERMINAL_DEADLINE" | ApiErrorCode;
}
export interface BaselineSmokeResult {
  readonly kind: "api-smoke";
  readonly evidence_scope: "calibration-only";
  readonly business_oracle: "not-performed";
  readonly model_identity: "not-observed";
  readonly environment_instance_id: string;
  readonly fixture_reset_id: string;
  readonly lease_epoch: number;
  readonly started_at: string;
  readonly completed_at: string;
  readonly verdict: SmokeVerdict;
  readonly cases: readonly SmokeCaseResult[];
}

/**
 * 单调时钟界定一次任务观察窗口；HTTP接受后直到实际done/failed才能报告终态。
 * 超时保留queued/processing，不合成业务failed；GET失败也不重发POST。
 */
async function observeTerminal(api: MlInferenceApi, id: string, timeout: number, interval: number): Promise<{
  terminal: boolean; observation: JobObservation | null; reason: SmokeCaseResult["reason"];
}> {
  const deadline = performance.now() + timeout;
  let observation: JobObservation | null = null;
  while (performance.now() < deadline) {
    const remaining = Math.floor(deadline - performance.now());
    if (remaining < 1) break;
    try {
      observation = await api.getPrediction(id, Math.min(remaining, 30_000));
    } catch (error) {
      if (!(error instanceof MlInferenceApiError) || error.code === "EVIDENCE_FAILED") throw error;
      return { terminal: false, observation, reason: error.code };
    }
    if (performance.now() >= deadline) break;
    if (observation === null) return { terminal: false, observation, reason: "JOB_NOT_FOUND" };
    if (observation.status === "done" || observation.status === "failed") return { terminal: true, observation, reason: "EXPECTED_TERMINAL_SHAPE" };
    await delay(Math.min(interval, Math.max(0, deadline - performance.now())));
  }
  return { terminal: false, observation, reason: "NONTERMINAL_DEADLINE" };
}

/**
 * 已登记环境的SMOKE action → 本入口 → MlInferenceApi → 逐请求guard/HTTP/证据。
 * 四项只校准原生API与推理链路：颜色图片没有正确类别，模型/持久性/恢复契约仍未验证。
 * 证据写失败立即抛出；未知POST结果后停止本套件，保留副作用待上层对账，绝不伪造PASS。
 */
export async function runBaselineSmoke(options: BaselineSmokeOptions): Promise<BaselineSmokeResult> {
  const startedAt = new Date().toISOString();
  const timeout = boundedMs(options.jobTimeoutMs ?? 30_000, 300_000);
  const interval = boundedMs(options.pollIntervalMs ?? 250, 5_000);
  const api = new MlInferenceApi({ environment: options.environment,
    expectedEnvironmentId: options.environment.environment_instance_id,
    expectedLeaseEpoch: options.environment.lease_epoch,
    guard: options.guard, evidenceSink: options.evidenceSink,
    ...(options.requestTimeoutMs === undefined ? {} : { requestTimeoutMs: options.requestTimeoutMs }) });
  const cases: SmokeCaseResult[] = [];
  const ids = ["valid-image", "invalid-image", "unsupported-mime", "unknown-job"] as const;
  for (const caseId of ids) {
    let id: string | null = null;
    let observation: JobObservation | null = null;
    let verdict: SmokeVerdict = "INCONCLUSIVE";
    let reason: SmokeCaseResult["reason"] = "NONTERMINAL_DEADLINE";
    let stop = false;
    try {
      if (caseId === "unknown-job") {
        id = randomUUID().replaceAll("-", "");
        observation = await api.getPrediction(id);
        verdict = observation === null ? "PASS" : "FAIL";
        reason = observation === null ? "EXPECTED_NOT_FOUND" : "UNEXPECTED_EXISTING_JOB";
      } else {
        const submitted = await api.submitFixture(caseId === "valid-image" ? "valid-png" : caseId === "invalid-image" ? "invalid-png" : "unsupported-mime");
        if (caseId === "unsupported-mime") {
          id = submitted.accepted ? submitted.id : null;
          verdict = submitted.accepted ? "FAIL" : "PASS";
          reason = submitted.accepted ? "UNEXPECTED_ACCEPTANCE" : "EXPECTED_HTTP_REJECTION";
        } else if (submitted.accepted) {
          id = submitted.id;
          const result = await observeTerminal(api, id, timeout, interval);
          observation = result.observation; reason = result.reason;
          if (!result.terminal) stop = true;
          if (result.terminal && observation) {
            const expected = caseId === "valid-image" ? observation.status === "done" &&
              observation.predictions?.length === 5 && !observation.error_present && observation.duration_ms !== null :
              observation.status === "failed" && observation.error_present && observation.predictions === null;
            verdict = expected ? "PASS" : "FAIL";
            reason = expected ? "EXPECTED_TERMINAL_SHAPE" : "UNEXPECTED_TERMINAL_SHAPE";
          }
        }
      }
    } catch (error) {
      if (!(error instanceof MlInferenceApiError) || error.code === "EVIDENCE_FAILED") throw error;
      reason = error.code;
      // 无论异常发生在POST还是后续GET，都停止新增副作用；调用方先核对环境/未确定动作。
      stop = true;
    }
    cases.push(Object.freeze({ case_id: caseId, verdict, job_id: id, observed_state: observation?.status ?? null, reason }));
    if (stop) break;
  }
  return Object.freeze({ kind: "api-smoke", evidence_scope: "calibration-only", business_oracle: "not-performed",
    model_identity: "not-observed", environment_instance_id: api.environment.environment_instance_id,
    fixture_reset_id: api.environment.fixture_reset_id, lease_epoch: api.environment.lease_epoch,
    started_at: startedAt, completed_at: new Date().toISOString(),
    verdict: cases.length === ids.length && cases.every((item) => item.verdict === "PASS") ? "PASS" :
      cases.some((item) => item.verdict === "FAIL") ? "FAIL" : "INCONCLUSIVE", cases: Object.freeze(cases) });
}
