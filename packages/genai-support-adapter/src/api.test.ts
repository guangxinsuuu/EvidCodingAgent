import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import test from "node:test";
import type { TestContext } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import type { EnvironmentInstance } from "@egap/contracts";
import { GenaiSupportApi, GenaiSupportApiError } from "./api.js";
import type { AdapterOptions, ApiObservation } from "./api.js";
import { SYNTHETIC_TICKET, syntheticSupportFixture } from "./fixtures.js";
import { runSupportSmoke } from "./smoke.js";

const JOB = "a".repeat(32);
const BEARER = "synthetic-test-session-not-a-provider-key";
const submission = { ...SYNTHETIC_TICKET, job_id: JOB, tenant_id: "tenant-a", status: "QUEUED" };
const draft = { resolution_type: "REFUND_ELIGIBILITY_REVIEW", ticket_id: "CS-1042", ticket_revision: 3,
  applicable_policy_ref: "refund-policy-2026-10", billing_record_ref: "invoice-1042", eligibility: "NEEDS_MORE_EVIDENCE",
  proposed_action: "HUMAN_REVIEW", evidence_refs: ["policy-clause"], customer_reply_draft: "synthetic-private-draft-text" };
const done = { ...submission, status: "DONE", draft, error_code: null };
function json(res: ServerResponse, code: number, value: unknown): void {
  res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(value));
}
/** 本服务器仅模拟新DRAFT协议，完全不运行LLM/Redis/政策检索，不生成业务通过证据。 */
async function harness(t: TestContext, handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>): Promise<{
  options: AdapterOptions; environment: EnvironmentInstance; observations: ApiObservation[];
}> {
  const server = createServer((req, res) => { void Promise.resolve(handler(req, res)).catch(() => res.destroy()); });
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  t.after(async () => { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); });
  const address = server.address(); assert.ok(address && typeof address === "object");
  const environment: EnvironmentInstance = {
    environment_instance_id: `environment_${randomUUID()}`, authority_mode: "file-lab", evidence_scope: "calibration-only",
    environment_config_digest: "1".repeat(64), environment_profile_digest: null, source_commit: "b".repeat(40),
    build_revision: "support-http-test-double", fixture_reset_id: `fixture_${randomUUID()}`, lease_epoch: 4,
    endpoints: { "support-gateway": `http://127.0.0.1:${address.port}` }, image_digests: { api: "2".repeat(64) },
    observed_topology_ref: "artifacts/test/topology.json", schema_mapping_ref: null,
    resource_manifest_ref: "artifacts/test/resources.json", expires_at: new Date(Date.now() + 60_000).toISOString(),
  };
  const observations: ApiObservation[] = [];
  return { environment, observations, options: { environment, expectedEnvironmentId: environment.environment_instance_id,
    expectedLeaseEpoch: 4, session: { expectedTenantId: "tenant-a", bearerToken: BEARER }, executionMode: "scripted-fake",
    guard: async () => {}, evidenceSink: async observation => { observations.push(observation); } } };
}
const code = (expected: string) => (value: unknown): boolean => value instanceof GenaiSupportApiError && value.code === expected;

test("合成请求没有tenant、政策答案或日期；POST只确认202+QUEUED并脱敏留证", async t => {
  let body = ""; let guards = 0;
  const h = await harness(t, async (req, res) => {
    assert.equal(req.url, "/support-resolution-jobs"); assert.equal(req.headers.authorization, `Bearer ${BEARER}`);
    for await (const block of req) body += block.toString(); json(res, 202, submission);
  });
  const api = new GenaiSupportApi({ ...h.options, guard: async () => { guards++; } });
  const result = await api.submitFixture("renewal-dispute"); assert.equal(result.accepted, true);
  assert.deepEqual(JSON.parse(body), SYNTHETIC_TICKET); assert.equal(guards, 1);
  assert.equal(h.observations[0]?.request.payload_sha256, syntheticSupportFixture("renewal-dispute").sha256);
  assert.equal(JSON.stringify(h.observations).includes(BEARER), false);
});
test("提交或查询跨tenant、错误job、工单或revision均拒绝", async t => {
  let body: unknown = submission; const h = await harness(t, (req, res) => json(res, req.method === "POST" ? 202 : 200, body));
  const api = new GenaiSupportApi(h.options);
  for (const wrong of [{ tenant_id: "tenant-b" }, { ticket_id: "CS-OTHER" }, { ticket_revision: 2 }]) {
    body = { ...submission, ...wrong }; await assert.rejects(() => api.submitFixture("renewal-dispute"), code("INVALID_RESPONSE"));
  }
  for (const wrong of [{ tenant_id: "tenant-b" }, { job_id: "b".repeat(32) }, { ticket_id: "CS-OTHER" }, { ticket_revision: 2 }]) {
    body = { ...done, ...wrong }; await assert.rejects(() => api.getJob(JOB, SYNTHETIC_TICKET), code("INVALID_RESPONSE"));
  }
});
test("回复草稿仅返回调用者；证据只记录摘要，FAILED只保留固定错误码", async t => {
  let body: unknown = done; const h = await harness(t, (_req, res) => json(res, 200, body)); const api = new GenaiSupportApi(h.options);
  assert.equal((await api.getJob(JOB, SYNTHETIC_TICKET))?.draft?.customer_reply_draft, draft.customer_reply_draft);
  assert.equal(JSON.stringify(h.observations).includes(draft.customer_reply_draft), false);
  assert.match(String(h.observations[0]?.response.draft_sha256), /^[a-f0-9]{64}$/);
  body = { ...submission, status: "FAILED", draft: null, error_code: "PROCESSING_FAILED" };
  assert.equal((await api.getJob(JOB, SYNTHETIC_TICKET))?.error_code, "PROCESSING_FAILED");
});
test("无效终态、未知属性、过大响应及错误content-type都不能当成功", async t => {
  let body: unknown = done; let mime = "application/json";
  const h = await harness(t, (_req, res) => { res.writeHead(200, { "content-type": mime }); res.end(JSON.stringify(body)); });
  const api = new GenaiSupportApi(h.options);
  for (const next of [{ ...done, draft: null }, { ...done, status: "RESOLVED" }, { ...done, debug: "extra" },
    { ...done, padding: "x".repeat(70_000) }]) { body = next; await assert.rejects(() => api.getJob(JOB, SYNTHETIC_TICKET), code("INVALID_RESPONSE")); }
  body = done; mime = "text/plain"; await assert.rejects(() => api.getJob(JOB, SYNTHETIC_TICKET), code("INVALID_RESPONSE"));
});
test("404才是任务不存在；403/503/重定向不改称缺失且不跟随", async t => {
  let status = 404; let calls = 0;
  const h = await harness(t, (_req, res) => { calls++; res.setHeader("location", "/redirect"); json(res, status, {}); });
  const api = new GenaiSupportApi(h.options); assert.equal(await api.getJob(JOB, SYNTHETIC_TICKET), null);
  for (status of [403, 503, 302]) await assert.rejects(() => api.getJob(JOB, SYNTHETIC_TICKET), code("HTTP_ERROR"));
  assert.equal(calls, 4);
});
test("422仅为批准负例的拒绝，正例422和负例403均不是成功", async t => {
  let status = 422; const h = await harness(t, (_req, res) => json(res, status, {})); const api = new GenaiSupportApi(h.options);
  assert.deepEqual(await api.submitFixture("invalid-ticket-revision"), { accepted: false, reason: "INVALID_INPUT" });
  await assert.rejects(() => api.submitFixture("renewal-dispute"), code("HTTP_ERROR")); status = 403;
  await assert.rejects(() => api.submitFixture("invalid-ticket-revision"), code("HTTP_ERROR"));
});
test("错误环境epoch、非本地endpoint、过期环境和注入header会话在发送前拒绝", async t => {
  let calls = 0; const h = await harness(t, (_req, res) => { calls++; json(res, 200, {}); });
  assert.throws(() => new GenaiSupportApi({ ...h.options, expectedLeaseEpoch: 3 }), code("INVALID_ENVIRONMENT"));
  assert.throws(() => new GenaiSupportApi({ ...h.options, environment: { ...h.environment, endpoints: { "support-gateway": "https://example.com:8443" } } }), code("INVALID_ENVIRONMENT"));
  assert.throws(() => new GenaiSupportApi({ ...h.options, environment: { ...h.environment, expires_at: new Date(0).toISOString() } }), code("ENVIRONMENT_EXPIRED"));
  assert.throws(() => new GenaiSupportApi({ ...h.options, session: { expectedTenantId: "tenant-a", bearerToken: "x\n" } }), code("INVALID_SESSION"));
  assert.equal(calls, 0);
});
test("guard拒绝和迟到不能派发HTTP，超时不泄漏底层错误", async t => {
  let calls = 0; const h = await harness(t, (_req, res) => { calls++; json(res, 202, submission); });
  await assert.rejects(() => new GenaiSupportApi({ ...h.options, guard: async () => { throw new Error("sensitive detail"); } }).submitFixture("renewal-dispute"), code("ENVIRONMENT_REJECTED"));
  await assert.rejects(() => new GenaiSupportApi({ ...h.options, requestTimeoutMs: 20, guard: async () => { await delay(60); } }).submitFixture("renewal-dispute"), code("GUARD_TIMEOUT"));
  await delay(70); assert.equal(calls, 0); assert.equal(JSON.stringify(h.observations).includes("sensitive detail"), false);
});
test("已发送POST断链不重试，保留未知动作供上层对账", async t => {
  let calls = 0; const h = await harness(t, req => { calls++; req.socket.destroy(); });
  await assert.rejects(() => new GenaiSupportApi(h.options).submitFixture("renewal-dispute"), code("TRANSPORT_ERROR"));
  assert.equal(calls, 1); assert.equal(h.observations[0]?.outcome, "ERROR");
});
test("缓慢响应体受同一次deadline约束", async t => {
  const h = await harness(t, (_req, res) => { res.writeHead(200, { "content-type": "application/json" }); res.write('{"schema_version":'); });
  await assert.rejects(() => new GenaiSupportApi({ ...h.options, requestTimeoutMs: 30 }).getJob(JOB, SYNTHETIC_TICKET), code("REQUEST_TIMEOUT"));
});
test("202后证据写入失败不返回成功、不会重发", async t => {
  let calls = 0; const h = await harness(t, (_req, res) => { calls++; json(res, 202, submission); });
  await assert.rejects(() => new GenaiSupportApi({ ...h.options, evidenceSink: async () => { throw new Error("disk full"); } }).submitFixture("renewal-dispute"), code("EVIDENCE_FAILED"));
  assert.equal(calls, 1);
});
test("四项HTTP smoke通过仍不声称policy、LLM、可信trace或语义质量通过", async t => {
  let gets = 0; let posts = 0;
  const h = await harness(t, async (req, res) => {
    if (req.url === "/health") { json(res, 200, { schema_version: 1, status: "ok" }); return; }
    if (req.method === "POST") {
      posts++; let body = ""; for await (const part of req) body += part.toString();
      json(res, JSON.parse(body).ticket_revision === 0 ? 422 : 202, submission); return;
    }
    if (req.url !== `/support-resolution-jobs/${JOB}`) { json(res, 404, {}); return; }
    gets++;
    // 故意给出错误但形状合法的政策：证明smoke没有假装做业务Oracle。
    json(res, 200, gets === 1 ? { ...submission, status: "PROCESSING", draft: null, error_code: null } :
      { ...done, draft: { ...draft, applicable_policy_ref: "wrong-policy" } });
  });
  const result = await runSupportSmoke({ ...h.options, pollIntervalMs: 1 });
  assert.equal(result.verdict, "PASS"); assert.equal(result.cases.length, 4); assert.equal(posts, 2);
  assert.equal(result.execution_mode, "scripted-fake"); assert.equal(result.business_oracle, "not-performed");
  assert.equal(result.trusted_tool_trace, "not-observed"); assert.equal(result.semantic_quality, "not-evaluated");
  assert.equal(h.observations.every(o => o.lease_epoch === 4 && o.evidence_scope === "calibration-only"), true);
});
test("持续PROCESSING停止后续提交，不伪造FAILED或PASS", async t => {
  let posts = 0;
  const h = await harness(t, (req, res) => {
    if (req.url === "/health") json(res, 200, { schema_version: 1, status: "ok" });
    else if (req.method === "POST") { posts++; json(res, 202, submission); }
    else json(res, 200, { ...submission, status: "PROCESSING", draft: null, error_code: null });
  });
  const result = await runSupportSmoke({ ...h.options, jobTimeoutMs: 100, pollIntervalMs: 5 });
  assert.equal(result.verdict, "INCONCLUSIVE"); assert.equal(result.cases.length, 2); assert.equal(posts, 1);
  assert.equal(result.cases[1]?.observed_state, "PROCESSING");
});
test("DONE缺草稿不会通过；健康只报告API，不推断模型或工具可用", async t => {
  const h = await harness(t, (req, res) => {
    if (req.url === "/health") json(res, 200, { schema_version: 1, status: "ok" });
    else if (req.method === "POST") json(res, 202, submission);
    else json(res, 200, { ...done, draft: null });
  });
  const result = await runSupportSmoke(h.options); assert.notEqual(result.verdict, "PASS");
  assert.deepEqual(h.observations[0]?.response, { api: "reachable", llm: "not-observed", tool_trace: "not-observed" });
});

test("负例202即使返回非法revision也明确FAIL；任务身份未知须对账并停止后续请求", async t => {
  let posts = 0; let calls = 0;
  const h = await harness(t, (req, res) => {
    calls++;
    if (req.url === "/health") json(res, 200, { schema_version: 1, status: "ok" });
    else if (req.method === "POST") { posts++; json(res, 202, { ...submission, ticket_revision: posts === 1 ? 3 : 0 }); }
    else json(res, 200, done);
  });
  const result = await runSupportSmoke(h.options);
  assert.equal(result.verdict, "FAIL"); assert.equal(result.cases.length, 3); assert.equal(calls, 4);
  assert.equal(result.cases[2]?.reason, "UNEXPECTED_ACCEPTANCE");
  assert.deepEqual(h.observations.at(-1)?.response, { job_identity: "UNKNOWN", reconciliation_required: true });
  assert.equal(h.observations.at(-1)?.http_status, 202);
});
