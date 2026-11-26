import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { Runtime } from "./runtime.js";
import { SimulatedSourceReader, SourceReaderSet, type SourceBinding } from "./source-authority.js";
import { sha, validateBundle, type Json, type ReleaseBundle } from "./types.js";

/** 合成来源 HTTP 验证身份/撤权协议；真实部署的权限验收另由 source-platform/host.py 完成。 */
async function fixture(t: TestContext) {
  const owner = "test:" + randomUUID();
  const issue = { source_kind: "SIMULATED_JIRA_ISSUE", issue_key: "EGAPLAB-100", revision: 1,
    summary: "验证固定来源任务", requester_ref: owner, owner_ref: owner, non_goals: [],
    requirements: [{ id: "R5", kind: "PRESERVE", statement: "不能把不可用当空事实", owner_ref: owner }] };
  const bodies = { "jira:EGAPLAB-100": JSON.stringify(issue), "rules:refund-rules": "## R5\n不能把不可用当空事实。\n" };
  const sources: SourceBinding[] = Object.entries(bodies).map(([ref, content]) => {
    const jira = ref.startsWith("jira:");
    return { ref, namespace: jira ? "jira" : "rules", object_id: jira ? "EGAPLAB-100" : "refund-rules",
      instance_id: "evid-sources-123456789abc", tenant_id: "company-a", version: jira ? "1" : "lab-v1-extracted",
      publication_id: (jira ? "a" : "b").repeat(32), publisher_id: jira ? "lab:requirements-publisher" : "lab:business-publisher",
      content_sha256: sha(content), metadata: { authority_scope: "SYNTHETIC_LOCAL_CALIBRATION", approval_state: jira ? "DRAFT" : "PENDING_USER_REVIEW",
        content_type: jira ? "application/json" : "text/markdown", profile: jira ? "INDEPENDENT_PRODUCT_DEPLOYMENT" : "COMMERCIAL_CROSS_SYSTEM_SOURCE_V1",
        source: { repository_url: `https://github.com/guangxinsuuu/${jira ? "EvidEnterpriseSources" : "EvidBusinessRules"}.git`,
          commit: "e".repeat(40), blob: "f".repeat(40), path: jira ? "seed/jira/EGAPLAB-100.v1.json" : "policies/rules.md" } } };
  });
  const bundle: ReleaseBundle = { schema_version: 2, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING",
    input_provenance: "CALIBRATION_SYNTHETIC", goal: issue.summary, baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40),
    catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40), requirement_refs: ["R5"],
    changes: [{ path: "app.py", diff_material_ref: "diff:app" }], external_sources: sources,
    materials: [...sources.map(s => { const content = bodies[s.ref as keyof typeof bodies]; return {
      material_ref: s.namespace === "jira" ? s.ref : "rule:R5", kind: s.namespace === "jira" ? "UPSTREAM_REQUIREMENTS" : "REGISTERED_RULE_EXCERPT",
      title: "固定来源", git_commit: s.metadata.source.commit, path: s.metadata.source.path, content, sha256: sha(content),
      external_source_ref: s.ref, source_byte_start: 0, source_byte_end: Buffer.byteLength(content) }; }),
      { material_ref: "diff:app", kind: "UNTRUSTED_DIFF", title: "变化", git_commit: "b".repeat(40), path: "app.py", content: "-old\n+new\n", sha256: sha("-old\n+new\n") }],
    catalog: { status: "DRAFT", dispatch_authorized: false, requirements: [{ id: "R5", material_ref: "rule:R5" }], capabilities: [], scenarios: [], sources: [] },
    gaps: [], current_candidate_coverage: [], evaluation_status: "NOT_RUN",
    review_request: { schema_version: 1, request_id: "jira:EGAPLAB-100", revision: 1, requester_ref: owner, owner_ref: owner,
      goal: issue.summary, non_goals: [], baseline: { commit: "a".repeat(40), reason: "指定比较", source_ref: "git:baseline" },
      candidate: { commit: "b".repeat(40), source_ref: "git:candidate" },
      requirements: [{ ...issue.requirements[0]!, kind: "PRESERVE", material_ref: "rule:R5", authority: "LOCAL_CALIBRATION", status: "PENDING" }],
      known_gaps: [], predecessor_run_id: null } };
  const state = { available: true, tamper: "", reads: 0 };
  const server = createServer((req, res) => {
    state.reads++; res.setHeader("content-type", "application/json");
    if (!state.available) { res.statusCode = 503; res.end('{"error_code":"ACL_UNAVAILABLE"}'); return; }
    const source = sources.find(s => req.url === `/v1/objects/${s.namespace}/${s.object_id}/versions/${s.version}`)!;
    const body = bodies[source.ref as keyof typeof bodies];
    res.end(JSON.stringify({ ...source, schema_version: 1, source_kind: "SIMULATED_ENTERPRISE_SOURCE",
      content_base64: Buffer.from(state.tamper || body).toString("base64"), observed_at: new Date().toISOString(),
      acl_decision: { principal_id: "lab:planner-reader", permission: "READ", scope: "EXACT_OBJECT_AND_TENANT", cache_authorization_seconds: 0, revision: 1 } }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>(resolve => { server.closeAllConnections(); server.close(() => resolve()); }));
  const reader = new SimulatedSourceReader({ base_url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    token: "x".repeat(40), principal_id: "lab:planner-reader", tenant_id: "company-a", instance_id: sources[0]!.instance_id, source_kind: "SIMULATED_ENTERPRISE_SOURCE" });
  return { bundle, reader, state };
}

test("来源：原件摘要、片段与 Jira 任务都绑定；撤权后新鲜缓存也不可用", async t => {
  const h = await fixture(t); validateBundle(h.bundle);
  assert.equal((await h.reader.authorize(h.bundle)).length, 2);
  h.state.available = false;
  await assert.rejects(h.reader.authorize(h.bundle), /SOURCE_ACCESS_UNAVAILABLE/);
  h.state.available = true; h.state.tamper = "changed";
  await assert.rejects(h.reader.authorize(h.bundle), /SOURCE_CONTENT_DIGEST_MISMATCH/);
  h.state.tamper = "";
  const modified = structuredClone(h.bundle);
  modified.review_request = { ...modified.review_request!, requirements: [{ ...modified.review_request!.requirements[0]!, statement: "篡改要求" }] };
  await assert.rejects(h.reader.authorize(modified), /JIRA_REQUIREMENTS_CHANGED/);
  const fragment = structuredClone(h.bundle); fragment.materials[1]!.content = "伪造片段"; fragment.materials[1]!.sha256 = sha("伪造片段");
  await assert.rejects(h.reader.authorize(fragment), /SOURCE_PROJECTION_MISMATCH/);
});
test("来源：旧规则回退、随包地址、跨实例与批准升级拒绝", async t => {
  const h = await fixture(t);
  const edits: [string, (b: ReleaseBundle) => void][] = [
    ["LOCAL_RULE_FALLBACK_FORBIDDEN", b => { delete b.materials[1]!.external_source_ref; }],
    ["SOURCE_BINDING_FIELDS_INVALID", b => { (b.external_sources![0] as unknown as Json).endpoint = "http://example.invalid"; }],
    ["SOURCE_REGISTRATION_MISMATCH", b => { b.external_sources![0]!.metadata.approval_state = "APPROVED"; }],
    ["SOURCE_INSTANCE_MISMATCH", b => { b.external_sources![0]!.instance_id = "evid-sources-123456789abd"; }],
  ];
  for (const [code, edit] of edits) { const b = structuredClone(h.bundle); edit(b); assert.throws(() => validateBundle(b), new RegExp(code)); }
});

test("来源：独立 QA 实例准确路由，缺 reader 或撤权不能使用旧包", async t => {
  const h = await fixture(t);
  const content = JSON.stringify({ schema_version: 1, contract_id: "qa.commercial-phase-review.v1", version: "phase-v1",
    effect: "NO_BUSINESS_WRITES_NO_MODEL_CALLS", operation: "REVIEW_SUPPLIED_OBSERVATIONS", release_authorized: false,
    protected_final: false, authority_scope: "SYNTHETIC_LOCAL_CALIBRATION" });
  const source: SourceBinding = { ref: "acceptance:phase-review-contract", namespace: "acceptance", object_id: "phase-review-contract",
    version: "phase-v1", instance_id: "evid-sources-abcdef123456", tenant_id: "company-a", publication_id: "c".repeat(32),
    publisher_id: "lab:qa-publisher", content_sha256: sha(content), metadata: { authority_scope: "SYNTHETIC_LOCAL_CALIBRATION",
      approval_state: "DRAFT", profile: "PHASE_REVIEW_CONTRACT", content_type: "application/json", source: {
        repository_url: "https://github.com/guangxinsuuu/EvidAcceptanceAssets.git", commit: "d".repeat(40), blob: "e".repeat(40),
        path: "oracles/phase-contract.v1.json" } } };
  let available = true;
  const server = createServer((req, res) => {
    res.setHeader("content-type", "application/json");
    if (!available) { res.statusCode = 503; res.end("{}"); return; }
    assert.equal(req.headers.authorization, "Bearer " + "q".repeat(40));
    res.end(JSON.stringify({ ...source, schema_version: 1, source_kind: "SIMULATED_ENTERPRISE_SOURCE",
      content_base64: Buffer.from(content).toString("base64"), acl_decision: { principal_id: "lab:planner-reader", permission: "READ",
        scope: "EXACT_OBJECT_AND_TENANT", cache_authorization_seconds: 0 } }));
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>(resolve => { server.closeAllConnections(); server.close(() => resolve()); }));
  const qa = new SimulatedSourceReader({ base_url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    token: "q".repeat(40), principal_id: "lab:planner-reader", tenant_id: "company-a", instance_id: source.instance_id,
    source_kind: "SIMULATED_ENTERPRISE_SOURCE" });
  h.bundle.external_sources!.push(source);
  h.bundle.materials.push({ material_ref: source.ref, external_source_ref: source.ref, kind: "DECLARED_QA_CONTRACT", title: "QA contract",
    git_commit: source.metadata.source.commit, path: source.metadata.source.path, sha256: sha(content), content,
    source_byte_start: 0, source_byte_end: Buffer.byteLength(content) });
  validateBundle(h.bundle);
  await assert.rejects(new SourceReaderSet([h.reader]).authorize(h.bundle), /SOURCE_INSTANCE_READER_REQUIRED/);
  assert.throws(() => new SourceReaderSet([qa, qa]), /SOURCE_READER_SET_INVALID/);
  const readers = new SourceReaderSet([h.reader, qa]);
  assert.equal((await readers.authorize(h.bundle)).length, 3);
  available = false; await assert.rejects(readers.authorize(h.bundle), /SOURCE_ACCESS_UNAVAILABLE/);
});
test("Runtime：冻结包仍需来源 reader；权限丢失阻止投影、缓存操作和模型预算消费", {
  skip: !process.env.EGAP_VERIFY_TEST_DATABASE_URL, timeout: 20000,
}, async t => {
  const h = await fixture(t);
  const config = { databaseUrl: process.env.EGAP_VERIFY_TEST_DATABASE_URL!, plannerPrompt: "not invoked",
    model: { endpoint: "http://127.0.0.1:1", name: "NOT_INVOKED", digest: "1".repeat(64), timeoutMs: 1000, maxOutputTokens: 1000 } };
  const denied = new Runtime(config); t.after(() => denied.close());
  await assert.rejects(denied.acceptRun(h.bundle, "t".repeat(40)), /SOURCE_READER_REQUIRED/);
  const runtime = new Runtime({ ...config, sourceReader: h.reader }); t.after(() => runtime.close());
  await runtime.store.migrate(); const id = await runtime.acceptRun(h.bundle, "t".repeat(40));
  const before = await runtime.projection(id); assert.equal(before.model_calls_used, 0);
  h.state.available = false;
  await assert.rejects(runtime.projection(id), /SOURCE_ACCESS_UNAVAILABLE/);
  await assert.rejects(runtime.decide(id, { command_id: String(before.next_command_id), based_on_decision_revision: 1 }), /SOURCE_ACCESS_UNAVAILABLE/);
  await assert.rejects(runtime.operation(id, randomUUID()), /SOURCE_ACCESS_UNAVAILABLE/);
  await assert.rejects(runtime.submit(id, {}), /SOURCE_ACCESS_UNAVAILABLE/);
  const cancelled = await runtime.cancel(id);
  assert.equal(cancelled.status, "CANCELLED"); assert.equal(cancelled.source_access, "UNAVAILABLE");
  assert.equal(cancelled.read_receipts, undefined);
  h.state.available = true;
  assert.equal((await runtime.projection(id)).model_calls_used, 0);
  await runtime.cancel(id);
});

test("Runtime：方向更新→受控 READ 留来源回执；已读原件撤权后不能从操作缓存再次获得", {
  skip: !process.env.EGAP_VERIFY_TEST_DATABASE_URL, timeout: 20000,
}, async t => {
  const h = await fixture(t); let calls = 0;
  const model = createServer(async (req, res) => {
    res.setHeader("content-type", "application/json");
    if (req.url === "/api/tags") { res.end(JSON.stringify({ models: [{ name: "source-protocol-stub", digest: "1".repeat(64) }] })); return; }
    for await (const _chunk of req) { /* 消费协议输入；固定响应只检验 Runtime，不证明模型选测质量。 */ }
    const first = calls++ === 0;
    const body = { action_kind: first ? "UPDATE_DIRECTION" : "READ", requirement_refs: ["R5"], hypothesis_refs: ["H:failure"],
      evidence_refs: ["diff:app"], expected_information: "核对失败语义是否被变化影响", rationale: "先确认适用规则",
      payload: first ? { focus: "核对差异涉及的失败分支" } : { material_ref: "rule:R5", start_line: 1, end_line: 2 },
      hypothesis_updates: first ? [{ id: "H:failure", statement: "变化可能影响上游失败的处理", status: "OPEN", evidence_refs: ["diff:app"] }] : [],
      decision_frame: { unresolved_questions: [{ id: "q:failure", question: "当前变化是否保留失败语义", why_unresolved: "尚未读取适用规则", evidence_refs: ["diff:app"] }],
        selected_question_id: "q:failure", outcome_branches: [
          { observation: "规则支持假设", implication: "补充实验前提", next_action: "READ" },
          { observation: "依据不足", implication: "请负责人澄清", next_action: "CLARIFY" }] } };
    res.end(JSON.stringify({ model: "source-protocol-stub", done: true, message: { content: JSON.stringify(body) }, prompt_eval_count: 10, eval_count: 20 }));
  });
  await new Promise<void>(resolve => model.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>(resolve => { model.closeAllConnections(); model.close(() => resolve()); }));
  const runtime = new Runtime({ databaseUrl: process.env.EGAP_VERIFY_TEST_DATABASE_URL!, plannerPrompt: "unused", sourceReader: h.reader,
    model: { endpoint: `http://127.0.0.1:${(model.address() as AddressInfo).port}`, name: "source-protocol-stub", digest: "1".repeat(64), timeoutMs: 2000, maxOutputTokens: 1000 } });
  t.after(() => runtime.close()); await runtime.store.migrate();
  const id = await runtime.acceptRun(h.bundle, "t".repeat(40));
  let operation: Json = {};
  for (let i = 0; i < 2; i++) {
    const state = await runtime.projection(id);
    const call = await runtime.decide(id, { command_id: String(state.next_command_id), based_on_decision_revision: Number(state.decision_basis_revision) });
    assert.equal(call.status, "COMPLETED");
    operation = await runtime.submit(id, call.proposal);
    assert.equal(operation.status, "COMPLETED");
  }
  const receipt = operation.result as Json;
  assert.equal(receipt.transport, "SOURCE_HTTP_REAUTHORIZED_SNAPSHOT");
  assert.equal((receipt.source_authorization as Json[]).length, 2);
  assert.equal(receipt.content, "## R5\n不能把不可用当空事实。");
  h.state.available = false;
  await assert.rejects(runtime.operation(id, String(operation.operation_id)), /SOURCE_ACCESS_UNAVAILABLE/);
  await assert.rejects(runtime.projection(id), /SOURCE_ACCESS_UNAVAILABLE/);
  assert.equal(calls, 2);
  h.state.available = true; await runtime.cancel(id);
});
