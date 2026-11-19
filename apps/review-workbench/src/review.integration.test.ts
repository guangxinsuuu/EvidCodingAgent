import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test, { type TestContext } from "node:test";
import Fastify from "fastify";
import { registerReviewRoutes } from "./routes.js";
import { ReviewStore, digest, parseCommand } from "./store.js";
import { ReviewError, type Actor, type DecisionCommand, type ImportCase, type ReviewRole } from "./types.js";

const DATABASE_URL = process.env["EGAP_REVIEW_TEST_DATABASE_URL"];
const pgTest = (name: string, fn: (t: TestContext) => Promise<void>): void => { test(name, { skip: !DATABASE_URL, timeout: 20_000 }, fn); };
const actor = (role: ReviewRole = "QA", tenant_id = "review-test"): Actor => ({ id: `test-${role}`, display_name: `实验 ${role}`, role, tenant_id });
const errorCode = (code: string) => (error: unknown): boolean => error instanceof ReviewError && error.code === code;

/** 协议测试专用材料，不是实际客服实验；每个测试独立 UUID，清理只删除自身创建的行。 */
function fixture(): ImportCase {
  const id = `review-test-${randomUUID()}`;
  const content = "实验观察：当前无记录不等于窗口完整。<script>untrusted()</script>";
  return { id, title: "审查事务测试", tenant_id: "review-test", candidate_sha: "a".repeat(40), baseline_sha: null,
    execution_mode: "MANUAL_FIXED_PLAN_INTEGRATION_ONLY", source_ref: "test-fixture", source_sha256: digest(content),
    machine_result: { scope: "TEST_ONLY", agent_autonomous_success: false, protected_final: false }, limitations: ["TEST_ONLY"],
    evidence: [{ id: `${id}:observation`, title: "观察", kind: "EXCERPT", content, sha256: digest(content), source_ref: "test-fixture", source_sha256: digest(content), limitations: ["TEST_ONLY"] }],
    obligations: (["BUSINESS", "SOURCE", "QA"] as const).map(role => ({ role, title: `${role} 材料核对`, instruction: "只确认已读取的范围，不能代表发布批准。" })) };
}
async function setup(t: TestContext) {
  const store = new ReviewStore(DATABASE_URL!);
  await store.migrate();
  const material = fixture();
  const imported = await store.importCase(material);
  t.after(async () => {
    for (const table of ["events", "commands", "evidence", "obligations"]) await store.pool.query(`DELETE FROM egap_review.${table} WHERE case_id=$1`, [material.id]);
    await store.pool.query("DELETE FROM egap_review.cases WHERE id=$1", [material.id]);
    await store.close();
  });
  const command = (revision = 1): DecisionCommand => ({ idempotency_key: randomUUID(), expected_revision: revision,
    candidate_sha: material.candidate_sha, bundle_digest: imported.bundle_digest, reason: "核对当前版本的实验观察。",
    evidence_refs: [material.evidence[0]!.id], decision: "ACKNOWLEDGE" });
  return { store, material, imported, command };
}

test("审查命令拒绝伪造身份、额外字段、空原因及无引用确认", () => {
  const valid = { idempotency_key: randomUUID(), expected_revision: 1, candidate_sha: "a".repeat(40), bundle_digest: "b".repeat(64), reason: "确认范围", evidence_refs: ["test:e1"], decision: "ACKNOWLEDGE" };
  assert.deepEqual(parseCommand(valid, "DECISION"), valid);
  for (const mutation of [{ actor: "admin" }, { tenant_id: "tenant-b" }, { reason: " " }, { expected_revision: 0 }, { decision: "APPROVE_RELEASE" }]) assert.throws(() => parseCommand({ ...valid, ...mutation }, "DECISION"), errorCode("INVALID_REQUEST"));
  assert.throws(() => parseCommand({ ...valid, evidence_refs: [] }, "DECISION"), errorCode("EVIDENCE_REFERENCE_REQUIRED"));
});

pgTest("真实 PG：不可变导入可重放；三类人员看同一材料，GET 不推进状态", async t => {
  const { store, material, imported } = await setup(t);
  assert.equal((await store.importCase(material)).imported, false);
  await assert.rejects(store.importCase({ ...material, title: "不可覆盖" }), errorCode("IMMUTABLE_IMPORT_CONFLICT"));
  for (const role of ["BUSINESS", "SOURCE", "QA"] as const) {
    const tasks = await store.listTasks(actor(role));
    assert.equal(tasks.tasks.find(task => task.case_id === material.id)?.role, role);
    const detail = await store.getCase(material.id, actor(role));
    assert.equal(detail.revision, 1);
    assert.equal(detail.bundle_digest, imported.bundle_digest);
    assert.equal((detail.actor_obligation as { role: string }).role, role);
    assert.equal((detail.evidence as Record<string, unknown>[])[0]?.content, undefined);
  }
  const first = await store.getCase(material.id, actor());
  await store.getEvidence(material.evidence[0]!.id, actor());
  assert.deepEqual(await store.getCase(material.id, actor()), first);
  assert.equal((first.events as unknown[]).length, 1);
  assert.equal((first.decisions as unknown[]).length, 0);
});

pgTest("真实 PG：对象 ACL 隔离租户与证据，跨候选引用和错版本被拒绝", async t => {
  const { store, material, command } = await setup(t);
  const outsider = actor("QA", "other-tenant");
  assert.deepEqual(await store.listTasks(outsider), { tasks: [] });
  await assert.rejects(store.getCase(material.id, outsider), errorCode("NOT_FOUND"));
  await assert.rejects(store.getEvidence(material.evidence[0]!.id, outsider), errorCode("NOT_FOUND"));
  await assert.rejects(store.decide(material.id, outsider, command()), errorCode("NOT_FOUND"));
  await assert.rejects(store.decide(material.id, actor(), { ...command(), candidate_sha: "c".repeat(40) }), errorCode("STALE_MATERIAL"));
  await assert.rejects(store.decide(material.id, actor(), { ...command(), bundle_digest: "d".repeat(64) }), errorCode("STALE_MATERIAL"));
  await assert.rejects(store.decide(material.id, actor(), { ...command(), evidence_refs: ["other-case:evidence"] }), errorCode("INVALID_EVIDENCE_REFERENCE"));
  assert.equal((await store.getCase(material.id, actor())).revision, 1);
});

pgTest("真实 PG：命令提交后重建 Store 仍幂等；同 key 改内容/换人拒绝", async t => {
  const { store, material, command } = await setup(t);
  const payload = command();
  const receipt = await store.decide(material.id, actor(), payload);
  const restarted = new ReviewStore(DATABASE_URL!);
  try {
    assert.deepEqual(await restarted.decide(material.id, actor(), payload), receipt);
    await assert.rejects(restarted.decide(material.id, actor(), { ...payload, reason: "修改原命令" }), errorCode("IDEMPOTENCY_CONFLICT"));
    await assert.rejects(restarted.decide(material.id, actor("SOURCE"), payload), errorCode("IDEMPOTENCY_CONFLICT"));
    await assert.rejects(restarted.decide(material.id, actor(), command()), errorCode("STALE_REVISION"));
    const detail = await restarted.getCase(material.id, actor());
    assert.equal(detail.revision, 2);
    assert.equal((detail.decisions as unknown[]).length, 1);
    assert.equal((detail.events as unknown[]).length, 2);
  } finally { await restarted.close(); }
});

pgTest("真实 PG：并发同版本只有一个提交；同 key 并发得到同一回执", async t => {
  const { store, material, command } = await setup(t);
  const results = await Promise.allSettled([store.decide(material.id, actor(), command()), store.decide(material.id, actor("BUSINESS"), command())]);
  assert.equal(results.filter(result => result.status === "fulfilled").length, 1);
  const rejected = results.find(result => result.status === "rejected");
  assert.ok(rejected?.status === "rejected" && errorCode("STALE_REVISION")(rejected.reason));
  const same = command(2);
  const [a, b] = await Promise.all([store.decide(material.id, actor(), same), store.decide(material.id, actor(), same)]);
  assert.deepEqual(a, b);
  assert.equal((await store.getCase(material.id, actor())).revision, 3);
});

pgTest("真实 PG：补证持久化为 WAITING_CAPABILITY，人员确认不能消除缺证或签发 PASS", async t => {
  const { store, material, command } = await setup(t);
  const { decision: _decision, ...basis } = command();
  const request = await store.requestEvidence(material.id, actor(), { ...basis, question: "请检查最终私有备注是否保留缺证说明。" });
  assert.equal(request.status, "WAITING_CAPABILITY");
  let revision = 2;
  for (const role of ["BUSINESS", "SOURCE", "QA"] as const) await store.decide(material.id, actor(role), command(revision++));
  const detail = await store.getCase(material.id, actor());
  assert.equal(detail.review_status, "WAITING_EVIDENCE");
  assert.equal((detail.evidence_requests as { status: string }[])[0]?.status, "WAITING_CAPABILITY");
  assert.equal((detail.actor_obligation as { status: string }).status, "WAITING_EVIDENCE");
  assert.equal((detail.decisions as { status: string }[]).at(-1)?.status, "ACKNOWLEDGED");
  assert.deepEqual(detail.machine_result, material.machine_result);
  assert.equal(detail.baseline_sha, null);
  assert.equal((detail.decisions as { actor_id: string }[]).at(-1)?.actor_id, actor().id);
});

pgTest("真实 PG：仅所有角色确认才汇总 ACKNOWLEDGED；机器结果保持原样", async t => {
  const { store, material, command } = await setup(t);
  await store.decide(material.id, actor("BUSINESS"), command());
  assert.equal((await store.getCase(material.id, actor())).review_status, "REQUIRED");
  await store.decide(material.id, actor("SOURCE"), { ...command(2), decision: "REQUEST_CHANGES" });
  assert.equal((await store.getCase(material.id, actor())).review_status, "CHANGES_REQUESTED");
  await store.decide(material.id, actor("SOURCE"), command(3));
  await store.decide(material.id, actor("QA"), command(4));
  const detail = await store.getCase(material.id, actor());
  assert.equal(detail.review_status, "ACKNOWLEDGED");
  assert.deepEqual(detail.machine_result, material.machine_result);
});

pgTest("真实 PG：证据摘要不符停止展示；路由不回显 DB 异常或伪造 actor", async t => {
  const { store, material, command } = await setup(t);
  const app = Fastify({ logger: false });
  app.decorateRequest("reviewActor", null);
  let authenticated = false;
  app.addHook("preHandler", async request => { request.reviewActor = authenticated ? actor() : null; });
  registerReviewRoutes(app, store);
  t.after(() => app.close());
  assert.equal((await app.inject("/api/v1/me")).statusCode, 401);
  authenticated = true;
  assert.deepEqual((await app.inject("/api/v1/me")).json(), { actor: actor() });
  const forged = await app.inject({ method: "POST", url: `/api/v1/review-cases/${material.id}/decisions`, payload: { ...command(), actor: "admin-secret" } });
  assert.equal(forged.statusCode, 400);
  assert.equal(forged.body.includes("admin-secret"), false);
  await store.pool.query("UPDATE egap_review.evidence SET material=jsonb_set(material,'{content}','\"changed\"') WHERE id=$1", [material.evidence[0]!.id]);
  const response = await app.inject(`/api/v1/evidence/${material.evidence[0]!.id}`);
  assert.equal(response.statusCode, 409);
  assert.equal(response.json().error_code, "EVIDENCE_INTEGRITY_ERROR");
});

pgTest("真实 PG：镜像同时改内容/hash或来源仍被拒绝；决定不能引用损坏镜像", async t => {
  const { store, material, command } = await setup(t);
  const original = material.evidence[0]!;
  const rewritten = { ...original, content: "篡改后与自己的 hash 一致", sha256: digest("篡改后与自己的 hash 一致") };
  for (const corrupted of [rewritten, { ...original, source_ref: "unrelated-source" }]) {
    await store.pool.query("UPDATE egap_review.evidence SET material=$2 WHERE id=$1", [original.id, corrupted]);
    await assert.rejects(store.getEvidence(original.id, actor()), errorCode("EVIDENCE_INTEGRITY_ERROR"));
    await assert.rejects(store.decide(material.id, actor(), command()), errorCode("EVIDENCE_INTEGRITY_ERROR"));
    assert.equal((await store.getCase(material.id, actor())).revision, 1);
  }
});

pgTest("真实 PG：父 bundle 被改动后，详情、任务、证据和命令全部拒绝", async t => {
  const { store, material, command } = await setup(t);
  await store.pool.query("UPDATE egap_review.cases SET bundle=jsonb_set(bundle,'{title}','\"改动了冻结材料\"') WHERE id=$1", [material.id]);
  await assert.rejects(store.getCase(material.id, actor()), errorCode("BUNDLE_INTEGRITY_ERROR"));
  await assert.rejects(store.listTasks(actor()), errorCode("BUNDLE_INTEGRITY_ERROR"));
  await assert.rejects(store.getEvidence(material.evidence[0]!.id, actor()), errorCode("BUNDLE_INTEGRITY_ERROR"));
  await assert.rejects(store.decide(material.id, actor(), command()), errorCode("BUNDLE_INTEGRITY_ERROR"));
  await assert.rejects(store.importCase(material), errorCode("BUNDLE_INTEGRITY_ERROR"));
});
