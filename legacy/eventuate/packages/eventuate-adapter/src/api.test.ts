import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import type { EnvironmentInstance } from "@egap/contracts";
import { EventuateApi, EventuateApiError, runBaselineSmoke } from "./index.js";
import type { AdapterOptions, ApiObservation, EnvironmentGuard, EvidenceSink } from "./index.js";
import { decimalMoney, longId, parseExactJson } from "./wire.js";

function environment(url = "http://127.0.0.1:32123"): EnvironmentInstance {
  return {
    environment_instance_id: `environment_${randomUUID()}`, authority_mode: "file-lab",
    evidence_scope: "calibration-only", environment_config_digest: "a".repeat(64),
    environment_profile_digest: null, source_commit: "b".repeat(40), build_revision: "test-build",
    fixture_reset_id: `fixture_${randomUUID()}`, lease_epoch: 1,
    endpoints: { "customer-service": url, "order-service": url, "api-gateway-service": url },
    image_digests: { test: "c".repeat(64) }, observed_topology_ref: "test/topology.json",
    schema_mapping_ref: null, resource_manifest_ref: "test/resources.json",
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  };
}

function options(env: EnvironmentInstance, observations: ApiObservation[] = [],
  guard: EnvironmentGuard = async () => {}): AdapterOptions {
  return { environment: env, expectedEnvironmentId: env.environment_instance_id,
    expectedLeaseEpoch: env.lease_epoch, service: "api-gateway-service", guard,
    evidenceSink: async (row) => { observations.push(row); }, requestTimeoutMs: 1_000 };
}

async function serverTest(handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>,
  run: (url: string) => Promise<void>): Promise<void> {
  const server = createServer((req, res) => {
    Promise.resolve(handler(req, res)).catch(() => { res.destroy(); });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address !== "string");
  try {
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

function json(res: ServerResponse, body: string, status = 200): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(body);
}

function errorCode(code: string): (error: unknown) => boolean {
  return (error) => error instanceof EventuateApiError && error.code === code;
}

test("exact Money and Long preserve the actual wire decimal tokens", () => {
  assert.deepEqual(parseExactJson('{"id":9223372036854775806,"money":{"amount":100.00},"text":"12.30"}'),
    { id: "9223372036854775806", money: { amount: "100.00" }, text: "12.30" });
  assert.equal(decimalMoney("9007199254740993.10"), "9007199254740993.10");
  assert.equal(decimalMoney("100.0000"), "100.00");
  assert.equal(longId("9223372036854775807"), "9223372036854775807");
  for (const invalid of [100.00, "1e2", "0.001", "-1.00", "NaN", "100.00\n"]) {
    assert.throws(() => decimalMoney(invalid));
  }
  for (const invalid of [42, "9223372036854775808", "01", "1/../../orders", "1\n"]) assert.throws(() => longId(invalid));
});

test("constructor binds expected identity/epoch and rejects expired or non-local registered URLs", () => {
  const env = environment();
  assert.throws(() => new EventuateApi({ ...options(env), expectedEnvironmentId: `environment_${randomUUID()}` }), errorCode("INVALID_ENVIRONMENT"));
  assert.throws(() => new EventuateApi({ ...options(env), expectedLeaseEpoch: 2 }), errorCode("INVALID_ENVIRONMENT"));
  assert.throws(() => new EventuateApi(options({ ...env, expires_at: new Date(0).toISOString() })), errorCode("ENVIRONMENT_EXPIRED"));
  for (const url of ["https://127.0.0.1:12345", "http://example.com:12345", "http://127.0.0.1", "http://127.0.0.1:80",
    "http://127.0.0.1:12345/other", "http://user:password@127.0.0.1:12345", "http://127.0.0.1:12345/?token=secret"]) {
    assert.throws(() => new EventuateApi(options(environment(url))), errorCode("INVALID_ENVIRONMENT"));
  }
  assert.throws(() => new EventuateApi({ ...options(env), requestTimeoutMs: Infinity }));
});

test("guard runs for every request; stale epoch and expiry prevent another HTTP call", async () => {
  let calls = 0;
  await serverTest((_req, res) => { calls++; json(res, '{"customers":[]}'); }, async (url) => {
    const env = environment(url);
    const observations: ApiObservation[] = [];
    let guards = 0;
    const api = new EventuateApi(options(env, observations, async (received) => {
      assert.equal(received.environment_instance_id, env.environment_instance_id);
      assert(Object.isFrozen(received.endpoints));
      if (++guards > 1) throw new Error("stale epoch contains secret");
    }));
    await api.health();
    await assert.rejects(api.health(), errorCode("ENVIRONMENT_REJECTED"));
    assert.equal(calls, 1);
    assert.equal(guards, 2);
    assert.equal(observations[1]?.error_code, "ENVIRONMENT_REJECTED");
    assert(!JSON.stringify(observations).includes("secret"));
    const expires = new EventuateApi(options({ ...env, expires_at: new Date(Date.now() + 20).toISOString() }));
    await delay(30);
    await assert.rejects(expires.health(), errorCode("ENVIRONMENT_EXPIRED"));
    assert.equal(calls, 1);
  });
});

test("late guard completion cannot dispatch HTTP after its deadline", async () => {
  let calls = 0;
  await serverTest((_req, res) => { calls++; json(res, '{"customers":[]}'); }, async (url) => {
    const api = new EventuateApi({ ...options(environment(url), [], async () => { await delay(60); }), requestTimeoutMs: 20 });
    await assert.rejects(api.health(), errorCode("GUARD_TIMEOUT"));
    await delay(80);
    assert.equal(calls, 0);
  });
});

test("HTTP 403, redirects and disconnects remain observation failures and never business rejection", async () => {
  for (const mode of ["forbidden", "redirect", "disconnect"] as const) {
    let calls = 0;
    const observations: ApiObservation[] = [];
    await serverTest((_req, res) => {
      calls++;
      if (mode === "disconnect") { res.destroy(); return; }
      if (mode === "redirect") { res.writeHead(302, { location: "/secret-destination" }); res.end("secret"); return; }
      json(res, '{"access_token":"secret"}', 403);
    }, async (url) => {
      const api = new EventuateApi(options(environment(url), observations));
      await assert.rejects(api.createOrder("123", "40.00"), errorCode(mode === "disconnect" ? "TRANSPORT_ERROR" : "HTTP_ERROR"));
      assert.equal(calls, 1);
      assert.equal(observations[0]?.outcome, "ERROR");
      assert.deepEqual(observations[0]?.response, {});
      assert(!JSON.stringify(observations).includes("secret"));
    });
  }
});

test("response streaming is bounded by time and size; bad response identity is rejected", async () => {
  for (const mode of ["timeout", "oversize", "wrong-id"] as const) {
    await serverTest((_req, res) => {
      res.writeHead(200, { "content-type": "application/json" });
      if (mode === "timeout") { res.write('{"orderId":'); return; }
      res.end(mode === "oversize" ? JSON.stringify({ extra: "x".repeat(70_000) }) : '{"orderId":456,"orderState":"APPROVED"}');
    }, async (url) => {
      const api = new EventuateApi({ ...options(environment(url)), requestTimeoutMs: 60 });
      await assert.rejects(api.getOrder("123"), errorCode(mode === "timeout" ? "REQUEST_TIMEOUT" : "INVALID_RESPONSE"));
    });
  }
});

test("failed evidence persistence never returns a successful API result", async () => {
  await serverTest((_req, res) => json(res, '{"orderId":123}'), async (url) => {
    const api = new EventuateApi({ ...options(environment(url)), evidenceSink: async () => { throw new Error("disk secret"); } });
    await assert.rejects(api.createOrder("1", "40.00"), errorCode("EVIDENCE_FAILED"));
  });
});

/** 模拟固定基线 Controller 的真实 JSON 形状；故意使用超出 JS 安全整数的 Long。 */
function fakeSut(mode: "healthy" | "pending" | "wrong-terminal" | "unknown-forbidden" = "healthy") {
  let nextId = 9_007_199_254_740_993n;
  const customers = new Map<string, string>();
  const orders = new Map<string, { state: string; reason: string | null; reads: number }>();
  const requests: { method: string; path: string; body: string }[] = [];
  const handler = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const parts: Buffer[] = [];
    for await (const chunk of req) parts.push(Buffer.from(chunk as Uint8Array));
    const body = Buffer.concat(parts).toString();
    const method = req.method ?? "";
    const path = req.url ?? "";
    requests.push({ method, path, body });
    if (method === "POST" && path === "/customers") {
      const row = JSON.parse(body) as { creditLimit: { amount: unknown } };
      assert.equal(typeof row.creditLimit.amount, "string");
      const id = (nextId++).toString(); customers.set(id, decimalMoney(row.creditLimit.amount));
      json(res, `{"customerId":${id},"secret":"do-not-store"}`); return;
    }
    if (method === "GET" && path.startsWith("/customers/")) {
      const id = path.slice("/customers/".length);
      const credit = customers.get(id);
      if (!credit) { json(res, '{"secret":"do-not-store"}', mode === "unknown-forbidden" ? 403 : 404); return; }
      json(res, `{"customerId":${id},"name":"do-not-store","creditLimit":{"amount":${credit}},"secret":"do-not-store"}`); return;
    }
    if (method === "POST" && path === "/orders") {
      const row = parseExactJson(body) as { customerId: string; orderTotal: { amount: string } };
      // 检验发送的是未舍入 Long 数字 token，金额仍是带两位小数的字符串。
      assert(body.includes(`"customerId":${row.customerId},`));
      assert.equal(typeof (JSON.parse(body) as { orderTotal: { amount: unknown } }).orderTotal.amount, "string");
      const credit = customers.get(row.customerId);
      const reason = !credit ? "UNKNOWN_CUSTOMER" : row.orderTotal.amount === "120.00" ? "INSUFFICIENT_CREDIT" : null;
      const id = (nextId++).toString();
      orders.set(id, { state: mode === "wrong-terminal" ? "REJECTED" : reason ? "REJECTED" : "APPROVED", reason, reads: 0 });
      json(res, `{"orderId":${id}}`); return;
    }
    if (method === "GET" && path.startsWith("/orders/")) {
      const id = path.slice("/orders/".length);
      const order = orders.get(id); assert(order);
      const pending = mode === "pending" || order.reads++ === 0;
      json(res, `{"orderId":${id},"orderState":"${pending ? "PENDING" : order.state}","rejectionReason":${JSON.stringify(pending ? null : order.reason)}}`); return;
    }
    json(res, "{}", 404);
  };
  return { handler, customers, orders, requests };
}

test("four smoke cases verify terminal state/reason, isolated customers and confirmed unknown customer", async () => {
  const sut = fakeSut();
  const observations: ApiObservation[] = [];
  await serverTest(sut.handler, async (url) => {
    let guards = 0;
    const result = await runBaselineSmoke({ environment: environment(url), guard: async () => { guards++; },
      evidenceSink: async (row) => { observations.push(row); }, orderTimeoutMs: 1_000, pollIntervalMs: 5 });
    assert.equal(result.verdict, "PASS");
    assert.equal(result.business_oracle, "not-performed");
    assert.equal(result.evidence_scope, "calibration-only");
    assert.equal(result.cases.length, 4);
    assert.equal(sut.customers.size, 3);
    assert.equal(sut.orders.size, 4);
    assert.equal(new Set(result.cases.map((row) => row.customer_id)).size, 4);
    assert.equal(guards, sut.requests.length);
    assert.equal(observations.length, sut.requests.length);
    const unknown = result.cases.find((row) => row.case_id === "unknown-customer")!;
    const confirmation = observations.findIndex((row) => row.operation === "get-customer" && row.request.customer_id === unknown.customer_id);
    const creation = observations.findIndex((row) => row.operation === "create-order" && row.request.customer_id === unknown.customer_id);
    assert(confirmation >= 0 && creation > confirmation);
    assert.equal(observations[confirmation]?.service, "customer-service");
    assert.equal(observations[confirmation]?.http_status, 404);
    assert(observations.filter((row) => row.operation === "create-order").every((row) => row.service === "api-gateway-service"));
    assert(!JSON.stringify(observations).includes("do-not-store"));
    assert(!JSON.stringify(observations).includes("egap-smoke-"));
  });
});

test("successful POST plus perpetual PENDING remains INCONCLUSIVE at a bounded deadline", async () => {
  const sut = fakeSut("pending");
  await serverTest(sut.handler, async (url) => {
    const started = performance.now();
    const result = await runBaselineSmoke({ environment: environment(url), guard: async () => {},
      evidenceSink: async () => {}, orderTimeoutMs: 70, pollIntervalMs: 10 });
    assert.equal(result.verdict, "INCONCLUSIVE");
    assert(result.cases.every((row) => row.verdict === "INCONCLUSIVE" && row.observed_state === "PENDING"));
    assert(result.cases.every((row) => row.order_id !== null));
    assert(performance.now() - started < 2_000);
  });
});

test("unexpected terminal is FAIL; a 403 unknown-customer probe cannot authorize order creation", async () => {
  for (const mode of ["wrong-terminal", "unknown-forbidden"] as const) {
    const sut = fakeSut(mode);
    await serverTest(sut.handler, async (url) => {
      const result = await runBaselineSmoke({ environment: environment(url), guard: async () => {},
        evidenceSink: async () => {}, orderTimeoutMs: 1_000, pollIntervalMs: 5 });
      if (mode === "wrong-terminal") {
        assert.equal(result.verdict, "FAIL");
        assert.equal(result.cases[0]?.reason, "UNEXPECTED_TERMINAL_STATE");
      } else {
        const unknown = result.cases.find((row) => row.case_id === "unknown-customer")!;
        assert.equal(unknown.verdict, "INCONCLUSIVE");
        assert.equal(unknown.reason, "HTTP_ERROR");
        assert.equal(unknown.order_id, null);
        assert.equal(sut.orders.size, 3);
      }
    });
  }
});

test("runner fails closed when the evidence sink fails", async () => {
  const sut = fakeSut();
  const evidenceSink: EvidenceSink = async () => { throw new Error("persistence failed"); };
  await serverTest(sut.handler, async (url) => {
    await assert.rejects(runBaselineSmoke({ environment: environment(url), guard: async () => {}, evidenceSink }), errorCode("EVIDENCE_FAILED"));
    assert.equal(sut.customers.size, 1);
    assert.equal(sut.orders.size, 0);
  });
});
