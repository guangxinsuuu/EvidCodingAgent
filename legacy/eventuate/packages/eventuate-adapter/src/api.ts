import { parseEnvironmentInstance } from "@egap/contracts";
import type { EnvironmentInstance } from "@egap/contracts";
import { decimalMoney, longId, object, parseExactJson } from "./wire.js";

export type ServiceName = "customer-service" | "order-service" | "api-gateway-service";
export type OrderState = "PENDING" | "APPROVED" | "REJECTED";
export type RejectionReason = "INSUFFICIENT_CREDIT" | "UNKNOWN_CUSTOMER";
export type ApiErrorCode = "INVALID_ENVIRONMENT" | "ENVIRONMENT_EXPIRED" | "ENVIRONMENT_REJECTED" |
  "GUARD_TIMEOUT" | "REQUEST_TIMEOUT" | "TRANSPORT_ERROR" | "HTTP_ERROR" | "INVALID_RESPONSE" |
  "EVIDENCE_FAILED";

/** 固定错误文本；不携带底层异常、HTTP 错误正文或 URL，防止秘密进入报告。 */
export class EventuateApiError extends Error {
  constructor(readonly code: ApiErrorCode, readonly httpStatus: number | null = null) {
    super(`Eventuate API operation failed: ${code}.`);
    this.name = "EventuateApiError";
  }
}

export interface ApiObservation {
  readonly kind: "api-smoke";
  readonly evidence_scope: "calibration-only";
  readonly environment_instance_id: string;
  readonly fixture_reset_id: string;
  readonly lease_epoch: number;
  readonly observed_at: string;
  readonly service: ServiceName;
  readonly operation: "create-customer" | "get-customer" | "create-order" | "get-order" | "health-customers";
  readonly http_status: number | null;
  readonly outcome: "OBSERVED" | "NOT_FOUND" | "ERROR";
  readonly error_code: ApiErrorCode | null;
  /** 仅含合成 fixture 的 ID/金额；不保留姓名、任意 headers 或原始响应体。 */
  readonly request: Readonly<Record<string, string>>;
  readonly response: Readonly<Record<string, string | null>>;
}

/**
 * 持锁 provisioner 提供：核对账本登记、当前 epoch、容器实际 ID/labels/端口。
 * 本包只能校验静态形状；guard 必须每次实查，不能以空函数代替生产执行身份检查。
 */
export type EnvironmentGuard = (environment: Readonly<EnvironmentInstance>) => Promise<void>;
export type EvidenceSink = (observation: Readonly<ApiObservation>) => Promise<void>;

export interface AdapterOptions {
  readonly environment: EnvironmentInstance;
  readonly expectedEnvironmentId: string;
  readonly expectedLeaseEpoch: number;
  readonly service: ServiceName;
  readonly guard: EnvironmentGuard;
  readonly evidenceSink: EvidenceSink;
  readonly requestTimeoutMs?: number;
}

export interface CustomerObservation {
  readonly customerId: string;
  readonly creditLimit: string;
}
export interface OrderObservation {
  readonly orderId: string;
  readonly orderState: OrderState;
  readonly rejectionReason: RejectionReason | null;
}

export function boundedMs(value: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) throw new TypeError("Invalid bounded duration.");
  return value;
}

function endpoint(environment: EnvironmentInstance, service: ServiceName): string {
  const input = environment.endpoints[service];
  if (!input || !["customer-service", "order-service", "api-gateway-service"].includes(service)) {
    throw new EventuateApiError("INVALID_ENVIRONMENT");
  }
  const url = new URL(input);
  // 只使用本机 Docker 实测发布端口。端口是否属于本环境由 guard 验证，不能靠端口范围猜。
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
      !url.port || Number(url.port) < 1024 || Number(url.port) > 65535 ||
      url.pathname !== "/" || url.username || url.password || url.search || url.hash) {
    throw new EventuateApiError("INVALID_ENVIRONMENT");
  }
  return url.origin;
}

/**
 * runBaselineSmoke → EventuateApi 的有类型操作 → guard → 有界 HTTP → 脱敏 evidenceSink。
 * 不暴露通用 URL/path 或 headers 入口，不跟随重定向；POST 失败不自动重试，
 * 因为超时/断链后远端可能已创建记录，重试会制造第二次业务副作用。
 */
export class EventuateApi {
  readonly environment: Readonly<EnvironmentInstance>;
  readonly service: ServiceName;
  readonly #origin: string;
  readonly #guard: EnvironmentGuard;
  readonly #sink: EvidenceSink;
  readonly #timeout: number;

  constructor(options: AdapterOptions) {
    try {
      this.environment = parseEnvironmentInstance(options.environment);
    } catch {
      throw new EventuateApiError("INVALID_ENVIRONMENT");
    }
    if (this.environment.environment_instance_id !== options.expectedEnvironmentId ||
        this.environment.lease_epoch !== options.expectedLeaseEpoch) {
      throw new EventuateApiError("INVALID_ENVIRONMENT");
    }
    this.service = options.service;
    this.#origin = endpoint(this.environment, this.service);
    this.#guard = options.guard;
    this.#sink = options.evidenceSink;
    this.#timeout = boundedMs(options.requestTimeoutMs ?? 5_000, 30_000);
    this.#checkExpiry();
  }

  #checkExpiry(): void {
    if (Date.now() >= Date.parse(this.environment.expires_at)) throw new EventuateApiError("ENVIRONMENT_EXPIRED");
  }

  async #emit(observation: Omit<ApiObservation, "kind" | "evidence_scope" | "environment_instance_id" |
    "fixture_reset_id" | "lease_epoch" | "observed_at" | "service">): Promise<void> {
    try {
      await this.#sink(Object.freeze({
        kind: "api-smoke", evidence_scope: "calibration-only",
        environment_instance_id: this.environment.environment_instance_id,
        fixture_reset_id: this.environment.fixture_reset_id, lease_epoch: this.environment.lease_epoch,
        observed_at: new Date().toISOString(), service: this.service, ...observation,
        request: Object.freeze({ ...observation.request }), response: Object.freeze({ ...observation.response }),
      }));
    } catch {
      // 证据写入失败后停止；不能返回看似可用、实际没有持久观察记录的成功结果。
      throw new EventuateApiError("EVIDENCE_FAILED");
    }
  }

  async #request<T>(operation: ApiObservation["operation"], method: "GET" | "POST", path: string,
    requestSummary: Readonly<Record<string, string>>, parse: (value: unknown) => T,
    summary: (value: T) => Readonly<Record<string, string | null>>, options: {
      body?: string; allow404?: boolean; timeoutMs?: number;
    } = {}): Promise<T | null> {
    let status: number | null = null;
    let value: T | null;
    const timeout = Math.min(this.#timeout, boundedMs(options.timeoutMs ?? this.#timeout, 30_000));
    const deadline = performance.now() + timeout;
    try {
      this.#checkExpiry();
      // guard 与 HTTP 共享一次调用的截止时间；迟到的 guard 完成不能继续发请求。
      let guardTimer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          this.#guard(this.environment).catch(() => { throw new EventuateApiError("ENVIRONMENT_REJECTED"); }),
          new Promise<never>((_resolve, reject) => {
            guardTimer = setTimeout(() => reject(new EventuateApiError("GUARD_TIMEOUT")), timeout);
          }),
        ]);
      } finally {
        clearTimeout(guardTimer);
      }
      this.#checkExpiry();
      const remaining = Math.floor(deadline - performance.now());
      if (remaining < 1) throw new EventuateApiError("GUARD_TIMEOUT");
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), remaining);
      try {
        const response = await fetch(`${this.#origin}${path}`, {
          method, redirect: "manual", signal: controller.signal,
          headers: { accept: "application/json", ...(options.body ? { "content-type": "application/json" } : {}) },
          ...(options.body ? { body: options.body } : {}),
        });
        status = response.status;
        if (status === 404 && options.allow404) {
          await response.body?.cancel();
          value = null;
        } else {
          if (status !== 200 && !(method === "POST" && status === 201)) {
            await response.body?.cancel();
            throw new EventuateApiError("HTTP_ERROR", status);
          }
          const contentType = response.headers.get("content-type")?.split(";")[0]?.trim();
          if (contentType !== "application/json") {
            await response.body?.cancel();
            throw new EventuateApiError("INVALID_RESPONSE", status);
          }
          // 限制响应体，计时器也覆盖流读取；错误体永远不拷贝到 evidence。
          const chunks: Uint8Array[] = [];
          let size = 0;
          const reader = response.body?.getReader();
          if (!reader) throw new EventuateApiError("INVALID_RESPONSE", status);
          while (true) {
            const chunk = await reader.read();
            if (chunk.done) break;
            size += chunk.value.byteLength;
            if (size > 65_536) {
              await reader.cancel();
              throw new EventuateApiError("INVALID_RESPONSE", status);
            }
            chunks.push(chunk.value);
          }
          try {
            value = parse(parseExactJson(Buffer.concat(chunks).toString("utf8")));
          } catch {
            throw new EventuateApiError("INVALID_RESPONSE", status);
          }
        }
        this.#checkExpiry();
        if (performance.now() >= deadline) throw new EventuateApiError("REQUEST_TIMEOUT", status);
      } catch (error) {
        if (error instanceof EventuateApiError) throw error;
        throw new EventuateApiError(controller.signal.aborted ? "REQUEST_TIMEOUT" : "TRANSPORT_ERROR", status);
      } finally {
        clearTimeout(timer);
      }
    } catch (error) {
      const safe = error instanceof EventuateApiError ? error : new EventuateApiError("ENVIRONMENT_REJECTED");
      await this.#emit({ operation, http_status: status, outcome: "ERROR", error_code: safe.code,
        request: requestSummary, response: {} });
      throw safe;
    }
    await this.#emit({ operation, http_status: status, outcome: value === null ? "NOT_FOUND" : "OBSERVED",
      error_code: null, request: requestSummary, response: value === null ? {} : summary(value) });
    return value;
  }

  async createCustomer(name: string, creditLimit: string): Promise<string> {
    if (this.service === "order-service" || !/^egap-smoke-[a-z0-9-]{1,100}$/.test(name)) {
      throw new TypeError("Only synthetic smoke customer names are supported.");
    }
    const money = decimalMoney(creditLimit);
    return (await this.#request("create-customer", "POST", "/customers", { credit_limit: money },
      (value) => longId(object(value).customerId), (id) => ({ customer_id: id }),
      { body: JSON.stringify({ name, creditLimit: { amount: money } }) }))!;
  }

  /** GET 404 只表示此客户不存在；403/5xx/断链不能当成不存在。姓名不进入适配器返回或证据。 */
  async getCustomer(customerId: string): Promise<CustomerObservation | null> {
    if (this.service === "order-service") throw new TypeError("Wrong service for customer operation.");
    const id = longId(customerId);
    return this.#request("get-customer", "GET", `/customers/${id}`, { customer_id: id }, (value) => {
      const row = object(value);
      if (longId(row.customerId) !== id) throw new TypeError("Customer identity mismatch.");
      return { customerId: id, creditLimit: decimalMoney(object(row.creditLimit).amount) };
    }, (row) => ({ customer_id: row.customerId, credit_limit: row.creditLimit }), { allow404: true });
  }

  async createOrder(customerId: string, orderTotal: string): Promise<string> {
    if (this.service === "customer-service") throw new TypeError("Wrong service for order operation.");
    const id = longId(customerId);
    const money = decimalMoney(orderTotal);
    return (await this.#request("create-order", "POST", "/orders", { customer_id: id, order_total: money },
      (value) => longId(object(value).orderId), (orderId) => ({ order_id: orderId }),
      // id 已严格校验为 Java Long 数字字面量；金额保持 JSON 字符串并由 BigDecimal 解析。
      { body: `{"customerId":${id},"orderTotal":{"amount":${JSON.stringify(money)}}}` }))!;
  }

  async getOrder(orderId: string, timeoutMs?: number): Promise<OrderObservation> {
    if (this.service === "customer-service") throw new TypeError("Wrong service for order operation.");
    const id = longId(orderId);
    return (await this.#request("get-order", "GET", `/orders/${id}`, { order_id: id }, (value) => {
      const row = object(value);
      if (longId(row.orderId) !== id || !["PENDING", "APPROVED", "REJECTED"].includes(String(row.orderState))) {
        throw new TypeError("Invalid order response.");
      }
      const reason = row.rejectionReason ?? null;
      if (reason !== null && reason !== "INSUFFICIENT_CREDIT" && reason !== "UNKNOWN_CUSTOMER") {
        throw new TypeError("Invalid rejection reason.");
      }
      return { orderId: id, orderState: row.orderState as OrderState, rejectionReason: reason };
    }, (row) => ({ order_id: row.orderId, order_state: row.orderState, rejection_reason: row.rejectionReason }),
    timeoutMs === undefined ? {} : { timeoutMs }))!;
  }

  /** 只检查 HTTP 路由，不代表订单业务 PASS 或数据库 Oracle 已完成。 */
  async health(): Promise<void> {
    if (this.service === "order-service") throw new TypeError("Health check requires customer or gateway service.");
    await this.#request("health-customers", "GET", "/customers", {}, (value) => {
      if (!Array.isArray(object(value).customers)) throw new TypeError("Invalid customer list.");
      return "reachable";
    }, (state) => ({ route: state }));
  }
}
