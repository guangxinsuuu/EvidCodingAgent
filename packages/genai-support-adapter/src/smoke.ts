import { randomBytes } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import type { SupportJob } from "@egap/contracts";
import { boundedMs, GenaiSupportApi, GenaiSupportApiError } from "./api.js";
import type { AdapterOptions, ApiErrorCode } from "./api.js";
import { SYNTHETIC_TICKET } from "./fixtures.js";

export type SmokeVerdict = "PASS" | "FAIL" | "INCONCLUSIVE";
export interface SmokeCase {
  readonly case_id: "health" | "valid-ticket" | "invalid-ticket-revision" | "unknown-job";
  readonly verdict: SmokeVerdict;
  readonly observed_state: SupportJob["status"] | null;
  readonly reason: ApiErrorCode | "EXPECTED_SHAPE" | "UNEXPECTED_RESULT" | "NONTERMINAL_DEADLINE" | "JOB_NOT_FOUND";
}
export interface SupportSmokeResult {
  readonly kind: "genai-api-smoke";
  readonly evidence_scope: "calibration-only";
  readonly execution_mode: AdapterOptions["executionMode"];
  readonly environment_instance_id: string;
  readonly fixture_reset_id: string;
  readonly lease_epoch: number;
  readonly business_oracle: "not-performed";
  readonly trusted_tool_trace: "not-observed";
  readonly semantic_quality: "not-evaluated";
  readonly verdict: SmokeVerdict;
  readonly cases: readonly SmokeCase[];
}

/**
 * 未来登记的 SMOKE action → 本入口 → GenaiSupportApi → 环境检查/HTTP/证据。
 * 固定四项只验证 DRAFT API 接线；不会依据 policy_ref 或回复文字判定 SUP-01…08。
 * POST/GET 不确定后停止新的提交；证据失败抛出交上层对账，不将未知转成业务 FAILED。
 */
export async function runSupportSmoke(options: AdapterOptions & {
  readonly jobTimeoutMs?: number; readonly pollIntervalMs?: number;
}): Promise<SupportSmokeResult> {
  const api = new GenaiSupportApi(options);
  const timeout = boundedMs(options.jobTimeoutMs ?? 30_000, 300_000);
  const interval = boundedMs(options.pollIntervalMs ?? 250, 5000);
  const cases: SmokeCase[] = [];
  for (const caseId of ["health", "valid-ticket", "invalid-ticket-revision", "unknown-job"] as const) {
    let verdict: SmokeVerdict = "INCONCLUSIVE";
    let reason: SmokeCase["reason"] = "NONTERMINAL_DEADLINE";
    let observed: SupportJob["status"] | null = null;
    let stop = false;
    try {
      if (caseId === "health") { await api.health(); verdict = "PASS"; reason = "EXPECTED_SHAPE"; }
      else if (caseId === "invalid-ticket-revision") {
        const result = await api.submitFixture("invalid-ticket-revision");
        verdict = result.accepted ? "FAIL" : "PASS"; reason = result.accepted ? "UNEXPECTED_RESULT" : "EXPECTED_SHAPE";
        // 负例意外受理也可能创建任务：立即停止，交调用方依据已登记证据对账。
        stop = result.accepted;
      } else if (caseId === "unknown-job") {
        const result = await api.getJob(randomBytes(16).toString("hex"), SYNTHETIC_TICKET);
        verdict = result === null ? "PASS" : "FAIL"; reason = result === null ? "EXPECTED_SHAPE" : "UNEXPECTED_RESULT";
      } else {
        const result = await api.submitFixture("renewal-dispute");
        if (!result.accepted) { verdict = "FAIL"; reason = "UNEXPECTED_RESULT"; stop = true; }
        else {
          const deadline = performance.now() + timeout;
          while (performance.now() < deadline) {
            const remaining = Math.floor(deadline - performance.now()); if (remaining < 1) break;
            const job = await api.getJob(result.submission.job_id, SYNTHETIC_TICKET, Math.min(remaining, 30_000));
            observed = job?.status ?? null;
            if (performance.now() >= deadline) break;
            if (!job) { reason = "JOB_NOT_FOUND"; break; }
            if (job.status === "DONE" || job.status === "FAILED") {
              verdict = job.status === "DONE" ? "PASS" : "FAIL";
              reason = job.status === "DONE" ? "EXPECTED_SHAPE" : "UNEXPECTED_RESULT"; break;
            }
            await delay(Math.min(interval, Math.max(0, deadline - performance.now())));
          }
          stop = verdict !== "PASS";
        }
      }
    } catch (error) {
      if (!(error instanceof GenaiSupportApiError) || error.code === "EVIDENCE_FAILED") throw error;
      if (error.code === "UNEXPECTED_ACCEPTANCE") verdict = "FAIL";
      reason = error.code; stop = true;
    }
    cases.push(Object.freeze({ case_id: caseId, verdict, reason, observed_state: observed }));
    if (stop) break;
  }
  return Object.freeze({ kind: "genai-api-smoke", evidence_scope: "calibration-only", execution_mode: options.executionMode,
    environment_instance_id: api.environment.environment_instance_id, fixture_reset_id: api.environment.fixture_reset_id,
    lease_epoch: api.environment.lease_epoch, business_oracle: "not-performed", trusted_tool_trace: "not-observed",
    semantic_quality: "not-evaluated", cases: Object.freeze(cases),
    verdict: cases.some(item => item.verdict === "FAIL") ? "FAIL" :
      cases.length === 4 && cases.every(item => item.verdict === "PASS") ? "PASS" : "INCONCLUSIVE" });
}
