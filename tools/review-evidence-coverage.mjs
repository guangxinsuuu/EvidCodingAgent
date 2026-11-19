/**
 * 4178 隔离工作台的 HTTP 覆盖验收；不能冒充浏览器点击或真实人员批准。
 * 先启动 review-evidence-browser.mjs，再由本工具补齐既有 Case 的四目标十二项原件。
 * HTTP 人员命令 → outbox → 同一真实 Graph/Runtime/MCP → 响应 → 实验 QA 限定接受。
 * 每条人员命令先写私有日志；中断重跑复用相同 payload/幂等键，模型消耗不重置。
 */
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { canonical } from "../apps/review-workbench/dist/store.js";
import { privateText } from "../apps/review-workbench/dist/config.js";

const id = "evidence-live-4a4640d4-36d4-4dcb-b849-4429dccb5d35";
const origin = "http://127.0.0.1:4178", folder = resolve("artifacts/review-workbench/browser-coverage", id);
const taskId = `${id}:evidence-linkage`;
const log = value => process.stdout.write(JSON.stringify(value) + "\n");
const sha = value => createHash("sha256").update(value).digest("hex");
const code = (await privateText(`${folder}/qa.code.private`)).trim();
const login = await fetch(`${origin}/api/v1/session`, { method: "POST", headers: { origin, "content-type": "application/json" }, body: JSON.stringify({ code }) });
assert.equal(login.status, 200);
const session = await login.json(); assert.equal(session.actor.id, `${id}:qa`);
const headers = { origin, "content-type": "application/json", "x-csrf-token": session.csrf_token, cookie: login.headers.get("set-cookie").split(";")[0] };
const api = async (path, body, version = "v2") => {
  const result = await fetch(`${origin}/api/${version}${path}`, { headers, ...(body ? { method: "POST", body: JSON.stringify(body) } : {}) });
  const value = await result.json(); assert.equal(result.status, 200, `HTTP_${result.status}_${value.error_code ?? "ERROR"}`); return value;
};
const detail = () => api(`/review-tasks/${taskId}`);
const basis = d => ({ idempotency_key: randomUUID(), expected_task_revision: d.task.task_revision,
  expected_assignment_revision: d.task.assignment_revision, expected_case_scope_revision: d.task.case_scope_revision,
  reviewed_bundle_id: d.task.bound_bundle_id, reviewed_bundle_digest: d.task.bound_bundle_digest, candidate_sha: d.task.candidate_sha });

/** 首次落盘的完整命令不可变；超时或崩溃后只重放原命令，不按新版本悄悄重绑。 */
async function command(key, path, body) {
  const file = resolve(folder, `http-${key}.command.private.json`);
  try { await writeFile(file, JSON.stringify({ path, body }), { flag: "wx", mode: 0o600 }); }
  catch (error) { if (error.code !== "EEXIST") throw error; }
  const frozen = JSON.parse(await privateText(file));
  assert.equal(frozen.path, path);
  const receipt = await api(frozen.path, frozen.body);
  await writeFile(resolve(folder, `http-${key}.receipt.private.json`), JSON.stringify(receipt), { mode: 0o600 });
  return receipt;
}

const summary = { schema_version: 1, case_id: id, transport: "HTTP_NOT_BROWSER", scope: "KNOWN_ARCHIVE_INTEGRATION",
  user_qa_approval: false, protected_final: false, release_authorized: false, targets: [], status: "RUNNING" };
const report = resolve(folder, "http-coverage.summary.private.json");
const save = async () => writeFile(report, JSON.stringify(summary, null, 2), { mode: 0o600 });
try {
  let d = await detail();
  assert.equal(d.case.candidate_sha, "45a0378585b8970fb2860897c9e49792e15a9df0");
  assert.equal(d.allowed_targets.length, 4); assert.equal(d.required_obligation_coverage.length, 12);
  summary.start_budget = d.case.budget_consumed;
  if (!d.task.claimed) await command("claim", `/review-tasks/${taskId}/claim`, basis(d));
  for (let index = 0; index < 4; index++) {
    d = await detail();
    const target = d.allowed_targets[index], key = canonical(target);
    const templates = d.item_templates.filter(t => ["SAVED_PRODUCT_DRAFT", "DELIVERED_PRIVATE_NOTE", "DELIVERY_RECEIPT"].includes(t.kind));
    assert.equal(templates.length, 3);
    let request = d.requests.find(r => !r.withdrawn && r.input.request_items.length === 3
      && templates.every(t => r.input.request_items.some(i => i.kind === t.kind && canonical(i.target) === key)));
    const prior = Boolean(request);
    if (!request) {
      const receipt = await command(`request-${index}`, `/review-tasks/${taskId}/evidence-requests`, { ...basis(d),
        goal: "逐项核对指定 Job 和阶段的已保存草稿、已交付私有备注及交付回执。读取确切原件并核对对象、阶段、版本与交付关联；保留业务语义未评估，不把当前历史归档结论扩展到新候选。",
        items: templates.map((t, i) => ({ item_id: `E${i + 1}`, kind: t.kind, target, required: true, acceptance_criteria: t.acceptance_criteria })) });
      request = (await detail()).requests.find(r => r.id === receipt.request_id);
      log({ event: "target_queued", index, request_id: request.id, target });
    }
    // 只读轮询不派发模型；执行由独立 Worker 驱动。到时停止验收，不创建替代请求。
    const deadline = Date.now() + 15 * 60_000;
    while (!request.current_response) {
      assert.ok(Date.now() < deadline, "EVIDENCE_RESPONSE_DEADLINE");
      assert.ok(!["BLOCKED", "CANCELLED"].includes(request.execution_state), `REQUEST_${request.execution_state}`);
      await new Promise(done => setTimeout(done, 2000));
      d = await detail(); request = d.requests.find(r => r.id === request.id);
    }
    assert.equal(request.execution_state, "FINISHED"); assert.equal(request.fulfillment_state, "COMPLETE");
    assert.equal(request.current_response.body.items.length, 3);
    assert.ok(request.current_response.body.items.every(i => i.validated_disposition === "PROVIDED"));
    const receipts = request.current_response.read_receipts;
    assert.equal(receipts.length, 3); assert.ok(receipts.every(r => r.transport === "MCP_ARCHIVE"));
    for (const item of request.current_response.body.items) {
      assert.equal(item.semantic_acceptance, "NOT_EVALUATED");
      const asked = request.input.request_items.find(i => i.item_id === item.item_id);
      assert.ok(asked);
      for (const ref of item.evidence_refs) {
        const material = await api(`/review-cases/${id}/materials/${encodeURIComponent(ref)}`);
        assert.equal(sha(material.content), material.sha256);
        assert.equal(material.kind, asked.kind);
        assert.equal(material.provenance.candidate_sha, d.case.candidate_sha);
        for (const [field, value] of Object.entries(asked.target)) assert.equal(material.provenance[field], value);
        assert.ok(receipts.some(r => r.material_ref === ref && r.source_sha256 === material.sha256 && item.read_receipt_refs.includes(r.receipt_ref)));
      }
    }
    if (request.review_state !== "ACCEPTED") {
      d = await detail();
      await command(`accept-${index}`, `/evidence-requests/${request.id}/accept`, { ...basis(d), request_spec_revision: request.request_spec_revision,
        response_id: request.current_response.id, response_digest: request.current_response.digest, review_policy_version: d.case.review_policy_version });
    }
    d = await detail();
    summary.targets.push({ index, target, reused_request: prior, request_id: request.id, response_id: request.current_response.id,
      dispositions: request.current_response.body.items.map(i => i.validated_disposition), read_receipts: receipts.length, task_status_after_accept: d.task.status });
    await save(); log({ event: "target_accepted", index, request_id: request.id, task_status: d.task.status });
  }
  d = await detail();
  const raw = d.requests.find(r => r.input.request_items.length === 1 && r.input.request_items[0].kind === "RAW_PROVIDER_RESPONSE");
  assert.ok(raw); assert.equal(raw.fulfillment_state, "UNAVAILABLE"); assert.equal(raw.review_state, "ACCEPTED");
  summary.before_withdraw = { task_status: d.task.status, raw_withdrawn: raw.withdrawn, raw_response_id: raw.current_response_id };
  if (!raw.withdrawn) {
    assert.equal(d.task.status, "WAITING_EVIDENCE");
    const receipt = await command("withdraw-raw", `/evidence-requests/${raw.id}/withdraw`, { ...basis(d),
      reason: "隔离验收仅闭合四个冻结目标的十二项核心材料关联；明确撤回非必需的 provider 原始响应扩展调查。原归档未采集该原件的事实、缺口响应和历史接受记录保留，不声称原始响应已取得。" });
    assert.equal(receipt.task_status, "COMPLETED"); assert.deepEqual(receipt.obligation_coverage, { provided: 12, required: 12 });
    summary.withdraw_receipt = receipt;
  }
  d = await detail();
  assert.equal(d.task.status, "COMPLETED");
  const withdrawnRaw = d.requests.find(r => r.id === raw.id);
  assert.equal(withdrawnRaw.withdrawn, true); assert.equal(withdrawnRaw.fulfillment_state, "UNAVAILABLE");
  assert.equal(withdrawnRaw.review_state, "ACCEPTED"); assert.equal(withdrawnRaw.current_response_id, raw.current_response_id);
  assert.equal(withdrawnRaw.current_response.digest, raw.current_response.digest);
  const semantic = (await api("/me/review-tasks")).tasks.find(t => t.case_id === id && t.obligation_id === "SEMANTIC_REVIEW");
  assert.notEqual(semantic.status, "COMPLETED");
  summary.final = { linkage_status: d.task.status, semantic_status: semantic.status, bundle_id: d.bundle.id, bundle_digest: d.bundle.digest,
    budget_consumed: d.case.budget_consumed, budget_reserved: d.case.budget_reserved, core_coverage: { provided: 12, required: 12 } };
  summary.status = "COMPLETED"; log({ event: "coverage_completed", final: summary.final });
} catch (error) {
  summary.status = "FAILED"; summary.error = error.message; process.exitCode = 1;
  log({ event: "coverage_failed", error: error.message });
} finally {
  await save();
  // 保留每次验收的起始预算与结果，避免复验覆盖首次运行的成本边界。
  await writeFile(resolve(folder, `${randomUUID()}.http-coverage.summary.private.json`), JSON.stringify(summary, null, 2), { flag: "wx", mode: 0o600 });
  await api("/logout", {}, "v1");
}
