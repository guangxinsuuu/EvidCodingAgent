import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import test from "node:test";
import type { TestContext } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { inflateSync } from "node:zlib";
import type { EnvironmentInstance } from "@egap/contracts";
import { MlInferenceApi, MlInferenceApiError } from "./api.js";
import type { AdapterOptions, ApiObservation } from "./api.js";
import { syntheticFixture } from "./fixtures.js";
import { runBaselineSmoke } from "./smoke.js";

const JOB_A = "a".repeat(32);
const JOB_B = "b".repeat(32);
const predictions = Array.from({ length: 5 }, (_, i) => ({ label: `synthetic-${i}`, score: 0.5 - i * 0.05 }));

function json(response: ServerResponse, status: number, body: unknown): void {
  response.writeHead(status, { "content-type": "application/json" }); response.end(JSON.stringify(body));
}
/** 本测试服务器模拟上游协议，不运行Redis/ONNX，不能成为真实ML基线证据。 */
async function harness(t: TestContext, handler: (request: IncomingMessage, response: ServerResponse) => void | Promise<void>): Promise<{
  environment: EnvironmentInstance; observations: ApiObservation[]; options: AdapterOptions;
}> {
  const server = createServer((request, response) => { void Promise.resolve(handler(request, response)).catch(() => response.destroy()); });
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  t.after(async () => { server.closeAllConnections(); await new Promise<void>((resolve) => server.close(() => resolve())); });
  const address = server.address(); assert.ok(address && typeof address === "object");
  const environment: EnvironmentInstance = {
    environment_instance_id: `environment_${randomUUID()}`, authority_mode: "file-lab", evidence_scope: "calibration-only",
    environment_config_digest: "1".repeat(64), environment_profile_digest: null,
    source_commit: "c200bcb4721c4428a9bfe808ca8a61648a2b16f6", build_revision: "http-test-double",
    fixture_reset_id: `fixture_${randomUUID()}`, lease_epoch: 7,
    endpoints: { "inference-gateway": `http://127.0.0.1:${address.port}` }, image_digests: { gateway: "2".repeat(64) },
    observed_topology_ref: "artifacts/test/topology.json", schema_mapping_ref: null,
    resource_manifest_ref: "artifacts/test/resources.json", expires_at: new Date(Date.now() + 60_000).toISOString(),
  };
  const observations: ApiObservation[] = [];
  return { environment, observations, options: { environment, expectedEnvironmentId: environment.environment_instance_id,
    expectedLeaseEpoch: environment.lease_epoch, guard: async () => {}, evidenceSink: async (value) => { observations.push(value); } } };
}
function errorCode(code: string): (value: unknown) => boolean {
  return (value) => value instanceof MlInferenceApiError && value.code === code;
}

test("合成PNG可解压为16×16 RGB扫描行，输入没有真实图片或类别真值", () => {
  const image = syntheticFixture("valid-png");
  assert.equal(image.contentType, "image/png");
  assert.deepEqual(image.bytes.subarray(0, 8), Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  assert.equal(image.bytes.readUInt32BE(16), 16); assert.equal(image.bytes.readUInt32BE(20), 16);
  assert.deepEqual([...image.bytes.subarray(24, 29)], [8, 2, 0, 0, 0]);
  let cursor = 8; const chunks: Buffer[] = []; const names: string[] = [];
  while (cursor < image.bytes.length) {
    const length = image.bytes.readUInt32BE(cursor); const type = image.bytes.toString("ascii", cursor + 4, cursor + 8);
    names.push(type); if (type === "IDAT") chunks.push(image.bytes.subarray(cursor + 8, cursor + 8 + length)); cursor += length + 12;
  }
  assert.deepEqual(names, ["IHDR", "IDAT", "IEND"]); assert.equal(cursor, image.bytes.length);
  const pixels = inflateSync(Buffer.concat(chunks)); assert.equal(pixels.length, 784);
  for (let row = 0; row < 16; row++) {
    assert.equal(pixels[row * 49], 0);
    for (let col = 0; col < 16; col++) assert.deepEqual([...pixels.subarray(row * 49 + 1 + col * 3, row * 49 + 4 + col * 3)], [128, 0, 0]);
  }
  assert.equal(syntheticFixture("valid-png").sha256, image.sha256);
  assert.throws(() => syntheticFixture("user-file" as "valid-png"));
});

test("POST只确认202+queued，multipart与证据均绑定合成fixture；不冒充done", async (t) => {
  let posted = ""; let guards = 0;
  const h = await harness(t, async (req, res) => {
    assert.equal(req.url, "/predictions"); assert.equal(req.method, "POST");
    assert.match(req.headers["content-type"]!, /^multipart\/form-data; boundary=/);
    const chunks: Buffer[] = []; for await (const part of req) chunks.push(part); posted = Buffer.concat(chunks).toString("latin1");
    json(res, 202, { id: JOB_A, status: "queued" });
  });
  const api = new MlInferenceApi({ ...h.options, guard: async () => { guards++; } });
  assert.deepEqual(await api.submitFixture("valid-png"), { accepted: true, id: JOB_A, status: "queued" });
  assert.match(posted, /name="file"; filename="egap-synthetic.png"/); assert.match(posted, /Content-Type: image\/png/);
  assert.equal(guards, 1); assert.equal(h.observations[0]?.response.status, "queued");
  assert.equal(h.observations[0]?.request.payload_sha256, syntheticFixture("valid-png").sha256);
  assert.equal(JSON.stringify(h.observations).includes(posted), false);
});

test("GET必须匹配job身份/已知状态与有限概率；原始错误文本不进入结果或证据", async (t) => {
  let body: unknown = { id: JOB_A, status: "failed", predictions: null, error: "secret-worker-path", duration_ms: null };
  const h = await harness(t, (_req, res) => json(res, 200, body)); const api = new MlInferenceApi(h.options);
  const result = await api.getPrediction(JOB_A);
  assert.equal(result?.error_present, true); assert.equal(result?.model_identity, "not-observed");
  assert.equal(JSON.stringify([result, h.observations]).includes("secret-worker-path"), false);
  for (const invalid of [
    { id: JOB_B, status: "done" }, { id: JOB_A, status: "SUCCEEDED" },
    { id: JOB_A, status: "done", predictions: [{ label: "x", score: 1.1 }] },
  ]) { body = invalid; await assert.rejects(() => api.getPrediction(JOB_A), errorCode("INVALID_RESPONSE")); }
});

test("POST协议漂移或过大的响应不能被宽松解析当成成功", async (t) => {
  let status = 202; let body: unknown = { id: JOB_A, status: "done" };
  const h = await harness(t, (_req, res) => json(res, status, body)); const api = new MlInferenceApi(h.options);
  await assert.rejects(() => api.submitFixture("valid-png"), errorCode("INVALID_RESPONSE"));
  status = 200; body = { id: JOB_A, status: "queued" };
  await assert.rejects(() => api.submitFixture("valid-png"), errorCode("HTTP_ERROR"));
  status = 202; body = { id: JOB_A, status: "queued", padding: "x".repeat(70_000) };
  await assert.rejects(() => api.submitFixture("valid-png"), errorCode("INVALID_RESPONSE"));
});

test("只有404表示任务缺失；503与302不能伪装成not-found且不跟随重定向", async (t) => {
  let status = 404; let calls = 0;
  const h = await harness(t, (_req, res) => { calls++; res.setHeader("location", "/elsewhere"); json(res, status, { detail: "not retained" }); });
  const api = new MlInferenceApi(h.options);
  assert.equal(await api.getPrediction(JOB_A), null);
  for (const next of [503, 302]) { status = next; await assert.rejects(() => api.getPrediction(JOB_A), errorCode("HTTP_ERROR")); }
  assert.equal(calls, 3);
});

test("错误epoch、过期环境和外部地址在HTTP发出前拒绝", async (t) => {
  let calls = 0; const h = await harness(t, (_req, res) => { calls++; json(res, 200, {}); });
  assert.throws(() => new MlInferenceApi({ ...h.options, expectedLeaseEpoch: 6 }), errorCode("INVALID_ENVIRONMENT"));
  assert.throws(() => new MlInferenceApi({ ...h.options, environment: { ...h.environment, expires_at: new Date(0).toISOString() } }), errorCode("ENVIRONMENT_EXPIRED"));
  assert.throws(() => new MlInferenceApi({ ...h.options, environment: { ...h.environment, endpoints: { "inference-gateway": "https://example.com:8443" } } }), errorCode("INVALID_ENVIRONMENT"));
  assert.equal(calls, 0);
});

test("guard拒绝/超时后不发送请求；迟到的guard也无副作用", async (t) => {
  let calls = 0; const h = await harness(t, (_req, res) => { calls++; json(res, 202, { id: JOB_A, status: "queued" }); });
  await assert.rejects(() => new MlInferenceApi({ ...h.options, guard: async () => { throw new Error("guard detail"); } }).submitFixture("valid-png"), errorCode("ENVIRONMENT_REJECTED"));
  await assert.rejects(() => new MlInferenceApi({ ...h.options, requestTimeoutMs: 20, guard: async () => { await delay(60); } }).submitFixture("valid-png"), errorCode("GUARD_TIMEOUT"));
  await delay(75); assert.equal(calls, 0);
});

test("已发送POST断链不自动重试，错误不回显底层细节", async (t) => {
  let calls = 0; const h = await harness(t, (req) => { calls++; req.socket.destroy(); });
  const api = new MlInferenceApi(h.options);
  await assert.rejects(() => api.submitFixture("valid-png"), errorCode("TRANSPORT_ERROR"));
  await delay(10); assert.equal(calls, 1); assert.equal(h.observations[0]?.outcome, "ERROR");
});

test("HTTP响应及响应体读取均有界，不能等待无期限返回", async (t) => {
  const h = await harness(t, (_req, res) => { res.writeHead(200, { "content-type": "application/json" }); res.write('{"id":'); });
  await assert.rejects(() => new MlInferenceApi({ ...h.options, requestTimeoutMs: 30 }).getPrediction(JOB_A), errorCode("REQUEST_TIMEOUT"));
});

test("证据写入失败立即停止，即使远端返回202也不交付成功凭证", async (t) => {
  let calls = 0; const h = await harness(t, (_req, res) => { calls++; json(res, 202, { id: JOB_A, status: "queued" }); });
  await assert.rejects(() => runBaselineSmoke({ environment: h.environment, guard: h.options.guard,
    evidenceSink: async () => { throw new Error("disk failure"); } }), errorCode("EVIDENCE_FAILED"));
  assert.equal(calls, 1);
});

test("四项smoke走真实HTTP协议mock，PASS明确仅校准且不推断模型身份/质量", async (t) => {
  let posts = 0; const seen = new Map<string, number>();
  const h = await harness(t, async (req, res) => {
    if (req.method === "POST") {
      posts++; const parts: Buffer[] = []; for await (const block of req) parts.push(block);
      const body = Buffer.concat(parts).toString("latin1");
      if (body.includes("Content-Type: text/plain")) { json(res, 415, { detail: "unsupported" }); return; }
      json(res, 202, { id: posts === 1 ? JOB_A : JOB_B, status: "queued" }); return;
    }
    const id = req.url!.split("/").at(-1)!;
    if (id !== JOB_A && id !== JOB_B) { json(res, 404, {}); return; }
    seen.set(id, (seen.get(id) ?? 0) + 1);
    if (seen.get(id) === 1) { json(res, 200, { id, status: "processing", predictions: null, error: null, duration_ms: null }); return; }
    json(res, 200, id === JOB_A ? { id, status: "done", predictions, error: null, duration_ms: 2.1 } :
      { id, status: "failed", predictions: null, error: "synthetic invalid image", duration_ms: null });
  });
  const result = await runBaselineSmoke({ environment: h.environment, guard: h.options.guard, evidenceSink: h.options.evidenceSink, pollIntervalMs: 1 });
  assert.equal(result.verdict, "PASS"); assert.equal(result.cases.length, 4); assert.equal(posts, 3);
  assert.equal(result.business_oracle, "not-performed"); assert.equal(result.model_identity, "not-observed");
  assert.equal(result.evidence_scope, "calibration-only"); assert.equal(result.cases[0]?.observed_state, "done");
  assert.equal(h.observations.every((x) => x.environment_instance_id === h.environment.environment_instance_id && x.lease_epoch === 7), true);
});

test("长期processing为INCONCLUSIVE并保留事实，停止后续提交而不造failed", async (t) => {
  let posts = 0; const h = await harness(t, (req, res) => {
    if (req.method === "POST") { posts++; json(res, 202, { id: JOB_A, status: "queued" }); }
    else json(res, 200, { id: JOB_A, status: "processing", predictions: null, error: null, duration_ms: null });
  });
  const result = await runBaselineSmoke({ environment: h.environment, guard: h.options.guard, evidenceSink: h.options.evidenceSink, jobTimeoutMs: 250, pollIntervalMs: 5 });
  assert.equal(result.verdict, "INCONCLUSIVE"); assert.equal(result.cases[0]?.observed_state, "processing");
  assert.equal(result.cases.length, 1); assert.equal(posts, 1);
});

test("done但缺少实际预测结果不会成为smoke PASS", async (t) => {
  const h = await harness(t, (req, res) => {
    if (req.method === "POST") json(res, 202, { id: JOB_A, status: "queued" });
    else json(res, 200, { id: req.url!.split("/").at(-1), status: "done", predictions: null, error: null, duration_ms: null });
  });
  const result = await runBaselineSmoke({ environment: h.environment, guard: h.options.guard, evidenceSink: h.options.evidenceSink });
  assert.equal(result.verdict, "FAIL"); assert.equal(result.cases[0]?.reason, "UNEXPECTED_TERMINAL_SHAPE");
});

test("health只报告Gateway/Redis，不推断ONNX Worker可用", async (t) => {
  const h = await harness(t, (req, res) => { assert.equal(req.url, "/health"); json(res, 200, { status: "ok", redis: true }); });
  await new MlInferenceApi(h.options).health();
  assert.deepEqual(h.observations[0]?.response, { gateway_redis: "reachable", worker_model: "not-observed" });
});
