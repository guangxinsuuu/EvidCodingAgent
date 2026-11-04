import { randomBytes, randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import type { EnvironmentInstance } from "@egap/contracts";
import { boundedMs, EventuateApi, EventuateApiError } from "./api.js";
import type { ApiErrorCode, EnvironmentGuard, EvidenceSink, OrderObservation, OrderState, RejectionReason } from "./api.js";

export interface BaselineSmokeOptions {
  readonly environment: EnvironmentInstance;
  readonly guard: EnvironmentGuard;
  readonly evidenceSink: EvidenceSink;
  readonly requestTimeoutMs?: number;
  readonly orderTimeoutMs?: number;
  readonly pollIntervalMs?: number;
}
export type SmokeVerdict = "PASS" | "FAIL" | "INCONCLUSIVE";
export interface SmokeCaseResult {
  readonly case_id: "sufficient-credit" | "insufficient-credit" | "unknown-customer" | "exact-credit";
  readonly verdict: SmokeVerdict;
  readonly credit_limit: string | null;
  readonly order_total: string;
  readonly customer_id: string | null;
  readonly order_id: string | null;
  readonly expected_state: "APPROVED" | "REJECTED";
  readonly expected_rejection_reason: RejectionReason | null;
  readonly observed_state: OrderState | null;
  readonly observed_rejection_reason: RejectionReason | null;
  readonly reason: "EXPECTED_TERMINAL_STATE" | "UNEXPECTED_TERMINAL_STATE" | "PENDING_DEADLINE" |
    "FIXTURE_MISMATCH" | "UNKNOWN_CUSTOMER_NOT_CONFIRMED" | ApiErrorCode;
}
export interface BaselineSmokeResult {
  readonly kind: "api-smoke";
  readonly evidence_scope: "calibration-only";
  readonly business_oracle: "not-performed";
  readonly environment_instance_id: string;
  readonly fixture_reset_id: string;
  readonly lease_epoch: number;
  readonly started_at: string;
  readonly completed_at: string;
  readonly verdict: SmokeVerdict;
  readonly cases: readonly SmokeCaseResult[];
}

const CASES = [
  { case_id: "sufficient-credit", credit_limit: "100.00", order_total: "40.00",
    expected_state: "APPROVED", expected_rejection_reason: null },
  { case_id: "insufficient-credit", credit_limit: "100.00", order_total: "120.00",
    expected_state: "REJECTED", expected_rejection_reason: "INSUFFICIENT_CREDIT" },
  { case_id: "unknown-customer", credit_limit: null, order_total: "40.00",
    expected_state: "REJECTED", expected_rejection_reason: "UNKNOWN_CUSTOMER" },
  { case_id: "exact-credit", credit_limit: "100.00", order_total: "100.00",
    expected_state: "APPROVED", expected_rejection_reason: null },
] as const;

/**
 * 有界观察 Saga 的真实终态；成功 POST 只代表有了 orderId。
 * PENDING 达到截止时间仍保留 PENDING，不制造 REJECTED；单调时钟防系统校时延长轮询。
 */
async function observeTerminal(api: EventuateApi, orderId: string, timeout: number, interval: number):
  Promise<{ terminal: boolean; observation: OrderObservation | null; errorCode: ApiErrorCode | null }> {
  const deadline = performance.now() + timeout;
  let observation: OrderObservation | null = null;
  while (performance.now() < deadline) {
    const remaining = Math.floor(deadline - performance.now());
    if (remaining < 1) break;
    try {
      observation = await api.getOrder(orderId, Math.min(remaining, 30_000));
    } catch (error) {
      if (!(error instanceof EventuateApiError) || error.code === "EVIDENCE_FAILED") throw error;
      // 断链前已见到的 PENDING 仍是有用证据，不能因为后一次请求失败而抹去。
      return { terminal: false, observation, errorCode: error.code };
    }
    // 即便迟到的响应看起来是终态，也不把超出本次观察窗口的结果变成 PASS。
    if (performance.now() >= deadline) break;
    if (observation.orderState !== "PENDING") return { terminal: true, observation, errorCode: null };
    await delay(Math.min(interval, Math.max(0, deadline - performance.now())));
  }
  return { terminal: false, observation, errorCode: null };
}

/**
 * CLI/provisioner 登记环境并开启 SMOKE action → 本入口 → EventuateApi → guard/HTTP/evidence。
 * 每个已知客户场景新建独立 fixture，避免上一订单的额度预留污染下一场景。
 * 该结果只有 API 校准意义：Customer GET 的 creditLimit 不是剩余额度，不证明信用账本不变量。
 * evidenceSink 失败向上抛出，调用者必须将 action 视为未完成，不能签发成功凭证。
 */
export async function runBaselineSmoke(options: BaselineSmokeOptions): Promise<BaselineSmokeResult> {
  const startedAt = new Date().toISOString();
  const orderTimeout = boundedMs(options.orderTimeoutMs ?? 60_000, 300_000);
  const interval = boundedMs(options.pollIntervalMs ?? 500, 5_000);
  const shared = {
    environment: options.environment,
    expectedEnvironmentId: options.environment.environment_instance_id,
    expectedLeaseEpoch: options.environment.lease_epoch,
    guard: options.guard, evidenceSink: options.evidenceSink,
    ...(options.requestTimeoutMs === undefined ? {} : { requestTimeoutMs: options.requestTimeoutMs }),
  };
  const gateway = new EventuateApi({ ...shared, service: "api-gateway-service" });
  const customerApi = new EventuateApi({ ...shared, service: "customer-service" });
  const cases: SmokeCaseResult[] = [];
  for (const spec of CASES) {
    let customerId: string | null = null;
    let orderId: string | null = null;
    let observation: OrderObservation | null = null;
    let verdict: SmokeVerdict = "INCONCLUSIVE";
    let reason: SmokeCaseResult["reason"] = "UNKNOWN_CUSTOMER_NOT_CONFIRMED";
    try {
      if (spec.credit_limit === null) {
        // 随机选取正 Long，再由真实 Customer GET 404 证明不存在；不使用永久魔数。
        for (let attempt = 0; attempt < 3; attempt++) {
          const candidate = ((randomBytes(8).readBigUInt64BE() & 0x3fff_ffff_ffff_ffffn) + 1n).toString();
          if (await customerApi.getCustomer(candidate) === null) { customerId = candidate; break; }
        }
      } else {
        customerId = await gateway.createCustomer(`egap-smoke-${randomUUID()}`, spec.credit_limit);
        const customer = await gateway.getCustomer(customerId);
        if (customer?.creditLimit !== spec.credit_limit) {
          reason = "FIXTURE_MISMATCH";
          customerId = null;
        }
      }
      if (customerId !== null) {
        orderId = await gateway.createOrder(customerId, spec.order_total);
        const observed = await observeTerminal(gateway, orderId, orderTimeout, interval);
        observation = observed.observation;
        if (!observed.terminal) {
          reason = observed.errorCode ?? "PENDING_DEADLINE";
        } else if (observation?.orderState === spec.expected_state &&
            observation.rejectionReason === spec.expected_rejection_reason) {
          verdict = "PASS";
          reason = "EXPECTED_TERMINAL_STATE";
        } else {
          verdict = "FAIL";
          reason = "UNEXPECTED_TERMINAL_STATE";
        }
      }
    } catch (error) {
      if (!(error instanceof EventuateApiError) || error.code === "EVIDENCE_FAILED") throw error;
      // 网络/权限/观察故障不能当作业务拒绝或失败归因；只返回未确定。
      reason = error.code;
    }
    cases.push(Object.freeze({ ...spec, verdict, customer_id: customerId, order_id: orderId,
      observed_state: observation?.orderState ?? null,
      observed_rejection_reason: observation?.rejectionReason ?? null, reason }));
  }
  return Object.freeze({ kind: "api-smoke", evidence_scope: "calibration-only", business_oracle: "not-performed",
    environment_instance_id: gateway.environment.environment_instance_id,
    fixture_reset_id: gateway.environment.fixture_reset_id, lease_epoch: gateway.environment.lease_epoch,
    started_at: startedAt, completed_at: new Date().toISOString(),
    verdict: cases.every((item) => item.verdict === "PASS") ? "PASS" :
      cases.some((item) => item.verdict === "FAIL") ? "FAIL" : "INCONCLUSIVE",
    cases: Object.freeze(cases),
  });
}
