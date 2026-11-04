import assert from "node:assert/strict";
import test from "node:test";
import { parseConfig } from "./config.js";
import { createServer } from "./server.js";

const config = parseConfig({ EGAP_MODE: "local-dev", EGAP_PORT: "4317", EGAP_LOG_LEVEL: "silent" });

test("健康接口只报告本阶段就绪；装配和inject均不监听端口", async (t) => {
  const app = createServer(config);
  t.after(() => app.close());
  assert.equal(app.server.listening, false);
  assert.deepEqual((await app.inject("/health/live")).json(), { status: "alive", mode: "local-dev" });
  const response = await app.inject("/health/ready");
  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.json(), {
    status: "ready", mode: "local-dev", scope: "control-plane-skeleton",
    checks: { configuration: "passed", http_routes: "registered" },
    required_external_dependencies: [], business_verification: "not_implemented", database_connection: "not_implemented",
  });
  assert.equal(app.server.listening, false);
});

test("未实现的Run接口不接受客户端伪造身份或返回模拟成功", async (t) => {
  const app = createServer(config);
  t.after(() => app.close());
  const response = await app.inject({ method: "POST", url: "/runs", payload: { actor: "admin", tenant_id: "other-tenant" } });
  assert.equal(response.statusCode, 404);
  assert.equal(response.json().error_code, "ROUTE_NOT_FOUND");
  assert.equal(response.body.includes("admin"), false);
});

test("非法请求体不在错误响应中回显输入内容", async (t) => {
  const app = createServer(config);
  t.after(() => app.close());
  const response = await app.inject({ method: "POST", url: "/runs", headers: { "content-type": "application/json" }, payload: '{"password":"secret-value"' });
  assert.equal(response.statusCode, 400);
  assert.equal(response.json().error_code, "INVALID_REQUEST");
  assert.equal(response.body.includes("secret-value"), false);
});
