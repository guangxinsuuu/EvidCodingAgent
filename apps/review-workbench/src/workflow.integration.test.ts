import assert from "node:assert/strict";
import { randomUUID, createHash } from "node:crypto";
import { test } from "node:test";
import { resolve } from "node:path";
import { loadHistoricalCase } from "./importer.js";
import { createReviewServer } from "./server.js";
import { ReviewStore } from "./store.js";
import { REVIEW_ROLES } from "./types.js";

const url = process.env["EGAP_REVIEW_TEST_DATABASE_URL"];

/** 实际报告的隔离副本通过完整会话/HTTP路由/PG链路；这些提交是实验身份验收，不是公司的人工批准。 */
test("three sessions review real historical excerpts through HTTP with restart and stale protection", { skip: !url, timeout: 20_000 }, async t => {
  const store = new ReviewStore(url!); await store.migrate();
  const bundle = await loadHistoricalCase(resolve("."));
  const suffix = randomUUID();
  bundle.id = `http-review-${suffix}`;
  bundle.title = `[隔离接口验收] ${bundle.title}`;
  for (const evidence of bundle.evidence) evidence.id = `${suffix}-${evidence.id}`;
  const imported = await store.importCase(bundle);
  const codes = REVIEW_ROLES.map(role => ({ role, code: randomUUID() }));
  const identities = codes.map(({ role, code }) => ({ actor: { id: `http-${role}`, display_name: `接口验收 ${role}`, role, tenant_id: bundle.tenant_id }, code_hash: createHash("sha256").update(code).digest("hex") }));
  const origin = "http://127.0.0.1:4174";
  let app = createReviewServer(store, { origin, identities });
  const headers = { host: "127.0.0.1:4174", origin, "content-type": "application/json" };
  t.after(async () => {
    await app.close();
    for (const table of ["events", "commands", "evidence", "obligations"]) await store.pool.query(`DELETE FROM egap_review.${table} WHERE case_id=$1`, [bundle.id]);
    await store.pool.query("DELETE FROM egap_review.cases WHERE id=$1", [bundle.id]); await store.close();
  });
  const sessions = [];
  for (const identity of codes) {
    const login = await app.inject({ method: "POST", url: "/api/v1/session", headers, payload: { code: identity.code } });
    assert.equal(login.statusCode, 200);
    const sessionHeaders = { ...headers, cookie: String(login.headers["set-cookie"]).split(";")[0]!, "x-csrf-token": String(login.json().csrf_token) };
    sessions.push(sessionHeaders);
    const list = await app.inject({ url: "/api/v1/me/review-tasks", headers: sessionHeaders });
    assert.equal(list.statusCode, 200);
    assert.equal(list.json().tasks.find((item: { case_id: string }) => item.case_id === bundle.id).role, identity.role);
    for (const item of bundle.evidence) {
      const evidence = await app.inject({ url: `/api/v1/evidence/${encodeURIComponent(item.id)}`, headers: sessionHeaders });
      assert.equal(evidence.statusCode, 200); assert.equal(evidence.json().content, item.content);
    }
  }
  const base = { candidate_sha: bundle.candidate_sha, bundle_digest: imported.bundle_digest, evidence_refs: [bundle.evidence[0]!.id], reason: "隔离接口验收：仅确认历史摘录身份，未签发业务或发布批准。" };
  const casePath = `/api/v1/review-cases/${bundle.id}`;
  const first = { ...base, idempotency_key: randomUUID(), expected_revision: 1, decision: "ACKNOWLEDGE" };
  const response = await app.inject({ method: "POST", url: casePath + "/decisions", headers: sessions[0]!, payload: first });
  assert.equal(response.statusCode, 200);
  const stale = await app.inject({ method: "POST", url: casePath + "/decisions", headers: sessions[1]!, payload: { ...first, idempotency_key: randomUUID() } });
  assert.equal(stale.statusCode, 409); assert.equal(stale.json().error_code, "STALE_REVISION");
  const second = await app.inject({ method: "POST", url: casePath + "/decisions", headers: sessions[1]!, payload: { ...first, idempotency_key: randomUUID(), expected_revision: 2 } });
  assert.equal(second.statusCode, 200);
  const request = { ...base, idempotency_key: randomUUID(), expected_revision: 3, question: "隔离验收：请补充最终备注全文及实际模型请求，以核查草稿与最终交付关系。" };
  const third = await app.inject({ method: "POST", url: casePath + "/evidence-requests", headers: sessions[2]!, payload: request });
  assert.equal(third.statusCode, 200); assert.equal(third.json().status, "WAITING_CAPABILITY");
  // HTTP服务重建使旧会话失效；审查事实与原命令回执仍可从同一PG库恢复。
  await app.close(); app = createReviewServer(store, { origin, identities });
  assert.equal((await app.inject({ url: casePath, headers: sessions[2]! })).statusCode, 401);
  const login = await app.inject({ method: "POST", url: "/api/v1/session", headers, payload: { code: codes[2]!.code } });
  const recovered = { ...headers, cookie: String(login.headers["set-cookie"]).split(";")[0]!, "x-csrf-token": String(login.json().csrf_token) };
  const replay = await app.inject({ method: "POST", url: casePath + "/evidence-requests", headers: recovered, payload: request });
  assert.deepEqual(replay.json(), third.json());
  const final = (await app.inject({ url: casePath, headers: recovered })).json();
  assert.equal(final.revision, 4); assert.equal(final.review_status, "WAITING_EVIDENCE");
  assert.equal(final.decisions.length, 2); assert.equal(final.evidence_requests.length, 1); assert.equal(final.events.length, 4);
  assert.deepEqual(final.machine_result, bundle.machine_result); assert.equal(final.baseline_sha, null);
});
