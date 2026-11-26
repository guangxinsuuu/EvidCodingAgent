import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { Ajv2020 } from "ajv/dist/2020.js";
import { parseDecisionProposal, type DecisionProposal } from "@egap/contracts";
import { capabilityContract, planningContractView, validatePlanningAuthority, verifyCapabilitySource } from "./capability-authority.js";
import { assessCheckBinding } from "./check-binding.js";
import { contextRef, contextView, modelBodySchema } from "./context.js";
import { sha, type Json, type ReleaseBundle, type RunState } from "./types.js";

type Check = Extract<DecisionProposal, { action_kind: "CHECK_INTENT" }>;
const target = capabilityContract["entrypoint"] as { path: string };
const source = readFileSync(new URL("../../../" + target.path, import.meta.url), "utf8");
const fixture = capabilityContract["fixture"] as { id: string; context: Json };
const ajv = new Ajv2020({ strict: true, allErrors: true, coerceTypes: false, useDefaults: false, removeAdditional: false });

/** 使用真实能力合同与入口材料，只以注释改动测试绑定；没有执行或使用任何留出样本。 */
function setup(rule = "R4"): { bundle: ReleaseBundle; state: RunState } {
  const candidate = source + "\n# billing_paid interface review\n";
  const diff = "--- a/commercial.py\n+++ b/commercial.py\n@@ -1 +1 @@\n+# billing_paid interface review";
  const bundle: ReleaseBundle = {
    schema_version: 1, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING", input_provenance: "CALIBRATION_SYNTHETIC",
    goal: "检查计划与实际接口的绑定", baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40),
    catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40), requirement_refs: [rule],
    changes: [{ path: target.path, diff_material_ref: "diff:commercial" }],
    materials: [
      { material_ref: "baseline:commercial", kind: "BASELINE_SOURCE", title: "base", git_commit: "a".repeat(40), path: target.path, content: source, sha256: sha(source) },
      { material_ref: "candidate:commercial", kind: "CANDIDATE_SOURCE", title: "candidate", git_commit: "b".repeat(40), path: target.path, content: candidate, sha256: sha(candidate) },
      { material_ref: "diff:commercial", kind: "UNTRUSTED_DIFF", title: "diff", git_commit: "b".repeat(40), path: target.path, content: diff, sha256: sha(diff) },
      { material_ref: "rule:" + rule, kind: "REGISTERED_RULE_EXCERPT", title: rule, git_commit: "d".repeat(40), path: "rules.md", content: "fixture rule excerpt", sha256: sha("fixture rule excerpt") },
    ], catalog: { status: "DRAFT", dispatch_authorized: false,
      requirements: [{ id: rule, material_ref: "rule:" + rule }],
      capabilities: [{ id: "probe.commercial_decision", dispatch_enabled: false }],
      scenarios: [{ id: "commercial_unit", stages: [{ stage_ref: "commercial_unit.evaluate", depends_on: [] }] }],
      sources: [{ id: "sut_runtime", kind: "PRODUCT_EXECUTION" }] },
    planning_contracts: [structuredClone(capabilityContract)], gaps: [], current_candidate_coverage: [], evaluation_status: "NOT_RUN",
  };
  const state: RunState = { run_id: "6605004c-6a27-4c25-a6ca-f508d6eeb5f1", status: "ACTIVE", candidate_epoch: 1,
    decision_basis_revision: 2, event_seq: 4, config_digest: "e".repeat(64), limits: { model_calls: 5, reads: 4, no_progress: 1, plan_repairs: 1 },
    model_calls_used: 1, reads_used: 1, no_progress: 0, hypotheses: [],
    read_receipts: [{ material_ref: "rule:" + rule, receipt_ref: "read:rule", start_line: 1, end_line: 1 }],
    latest_feedback: null, stop_reason: null };
  return { bundle, state };
}

function decision(): Check {
  return parseDecisionProposal({ schema_version: 1, proposal_id: "6605004c-6a27-4c25-a6ca-f508d6eeb5f1", based_on_decision_revision: 2,
    candidate_epoch: 1, context_view_ref: "context:fixture", action_kind: "CHECK_INTENT", requirement_refs: ["R4"],
    hypothesis_refs: [], evidence_refs: ["diff:commercial"], expected_information: "对齐付款输入与产品输出", rationale: "只测试接口绑定，不证明行为",
    hypothesis_updates: [{ id: "H1", statement: "付款状态可能影响结果", status: "OPEN", evidence_refs: ["diff:commercial"] }],
    payload: { capability_ref: "probe.commercial_decision", scenario_ref: "commercial_unit", objective: "付款状态输出比较",
      observation_requirements: ["取得产品函数是否已付款的输出"], check_spec: { schema_version: 1,
        binding: { contract_ref: "commercial-decision.v1", fixture_ref: fixture.id }, stage_ref: "commercial_unit.evaluate",
        inputs: [{ name: "commercial_source.payment.status", value: "UNPAID" }],
        observations: [{ id: "paid", source_ref: "sut_runtime", field: "decision.billing_paid" }],
        expectations: [{ observation_ref: "paid", operator: "ABSENT", value: "", requirement_ref: "R4" }],
        control: { scenario_ref: "commercial_unit", stage_ref: "commercial_unit.evaluate", inputs: [{ name: "commercial_source.payment.status", value: "PAID" }],
          observation_ref: "paid", expected_relation: "DIFFERENT", reason: "明确对比有付款和未付款条件" },
        change_relation: { diff_ref: "diff:commercial", changed_fields: ["billing_paid"], input_refs: ["commercial_source.payment.status"],
          output_refs: ["paid"], requirement_ref: "R4", claim: "MAY_CHANGE", expected_baseline: "null", expected_candidate: "true",
          reason: "未验证的行为假设；注释变化本身不证明行为改变" },
      } } }) as Check;
}

function body(): Json {
  const value = structuredClone(decision()) as unknown as Json;
  for (const key of ["schema_version", "proposal_id", "based_on_decision_revision", "candidate_epoch", "context_view_ref"]) delete value[key];
  return value;
}

test("真实能力合同生成原生Schema：完整计划合法，猜测输入/输出/阶段和缺绑定被拒绝", () => {
  const { bundle, state } = setup();
  const validate = ajv.compile(modelBodySchema(bundle, state));
  assert.equal(validate(body()), true, JSON.stringify(validate.errors));
  for (const mutate of [
    (spec: Json) => { delete spec["binding"]; },
    (spec: Json) => { delete spec["change_relation"]; },
    (spec: Json) => { spec["stage_ref"] = "late_usage.after_seal"; },
    (spec: Json) => { (spec["inputs"] as Json[])[0]!["name"] = "model.invented_argument"; },
    (spec: Json) => { (spec["observations"] as Json[])[0]!["field"] = "decision.model_approved"; },
  ]) {
    const candidate = body(); mutate((candidate["payload"] as Json)["check_spec"] as Json);
    assert.equal(validate(candidate), false);
  }
});

test("只有读过能力支持的要求才生成CHECK；单读R6不能诱导不可绑定的商业函数检查", () => {
  const { bundle, state } = setup("R6");
  const branches = modelBodySchema(bundle, state)["oneOf"] as Json[];
  assert.equal(branches.some(branch => (((branch["properties"] as Json)["action_kind"] as Json)["const"] === "CHECK_INTENT")), false);
  assert.ok(branches.some(branch => (((branch["properties"] as Json)["action_kind"] as Json)["const"] === "NOVEL_CHECK")));
});

test("模型契约投影只给真实默认标量，不提前暴露完整fixture规则文档或产生执行资格", () => {
  const view = planningContractView(capabilityContract);
  const projected = view["fixture"] as Json;
  assert.equal(projected["context"], undefined);
  assert.equal(Object.keys(projected["defaults"] as Json).length, 20);
  assert.equal((projected["defaults"] as Json)["commercial_source.payment.amount_minor"], 100);
  assert.equal((projected["defaults"] as Json)["commercial_source.contracts.0.terms.special_terms"], false);
  assert.equal(JSON.stringify(view).includes("# Business Source Pack v1"), false);
  assert.equal(view["dispatch_enabled"], false);
  assert.equal(view["execution_mode"], "PURE_FIXTURE");
  const { bundle, state } = setup();
  const context = contextView(bundle, state);
  assert.deepEqual(context["current_candidate_coverage"], []);
  assert.equal((context["final_eligibility"] as Json)["eligible"], false);
});

test("自带伪造的接口、原始fixture或空合同数组不能改变Runtime本地authority", () => {
  assert.doesNotThrow(() => validatePlanningAuthority([structuredClone(capabilityContract)]));
  assert.doesNotThrow(() => validatePlanningAuthority(undefined), "旧包可以读，但没有绑定资格");
  for (const change of [
    (contract: Json) => { contract["capability_ref"] = "execute.anything"; },
    (contract: Json) => { (contract["fixture"] as { context: Json }).context["injected"] = true; },
    (contract: Json) => { ((contract["inputs"] as Json[])[0]!)!["maximum"] = 1e15; },
  ]) {
    const forged = structuredClone(capabilityContract); change(forged);
    assert.throws(() => validatePlanningAuthority([forged]), { message: "PLANNING_CONTRACT_AUTHORITY_MISMATCH" });
    const { bundle } = setup(); bundle.planning_contracts = [forged];
    assert.ok(assessCheckBinding(bundle, decision(), capabilityContract).issues.some(issue => issue.code === "BINDING_CONTRACT_NOT_IN_BUNDLE"));
  }
  assert.throws(() => validatePlanningAuthority([]));
});

test("绑定必须核对真正B/C入口及摘要；同路径但错误提交身份不能算目标代码", () => {
  assert.doesNotThrow(verifyCapabilitySource);
  for (const kind of ["BASELINE_SOURCE", "CANDIDATE_SOURCE"]) {
    const { bundle } = setup(); bundle.materials = bundle.materials.filter(item => item.kind !== kind);
    assert.ok(assessCheckBinding(bundle, decision(), capabilityContract).issues.some(issue => issue.code === "TARGET_SOURCE_MISSING"));
    const wrong = setup().bundle; wrong.materials.find(item => item.kind === kind)!.git_commit = "f".repeat(40);
    assert.ok(assessCheckBinding(wrong, decision(), capabilityContract).issues.some(issue => issue.code === "TARGET_SOURCE_MISSING"));
  }
  const { bundle } = setup(); bundle.materials.find(item => item.kind === "CANDIDATE_SOURCE")!.content += "drift";
  assert.ok(assessCheckBinding(bundle, decision(), capabilityContract).issues.some(issue => issue.code === "CANDIDATE_SOURCE_DIGEST_MISMATCH"));
});

test("绑定后新增接口上下文仍不因修正调用预留而自我过期", () => {
  const { bundle, state } = setup(); const p = decision();
  state.check_repair = { status: "PENDING", root_proposal_id: p.proposal_id, latest_proposal_id: p.proposal_id,
    attempts_used: 0, attempts_limit: 1, latest_proposal: p,
    quality: { status: "NEEDS_REVISION", issues: [{ code: "INPUT_NOT_BOUND", path: "/payload/check_spec/inputs/0/name", message: "选择真实输入" }] } };
  const before = contextRef(bundle, state);
  state.model_calls_used += 1; state.check_repair.attempts_used += 1;
  assert.equal(contextRef(bundle, state), before);
});

test("多来源 READ 上下文去重不丢正文、引用或不确定性，权威回执保持原样", () => {
  const { bundle, state } = setup();
  const content = "OPEN / 0 only means no observed usage; delivery completeness remains UNRESOLVED.\n".repeat(50);
  const authorizations = Array.from({ length: 7 }, (_, i) => ({ source_ref: "source:" + i,
    instance_id: "instance:" + i, publication_id: "p".repeat(32), content_sha256: "s".repeat(64),
    observed_at: "2026-11-26T00:00:00Z", acl_decision: { principal_id: "lab:planner-reader", permission: "READ",
      scope: "EXACT_OBJECT_AND_TENANT", cache_authorization_seconds: 0 } }));
  state.read_receipts = [{ status: "COMPLETED", receipt_ref: "read:rule", material_ref: "rule:R4",
    content, start_line: 1, end_line: 51, source_sha256: sha(content), fragment_sha256: sha(content),
    source_authorization: authorizations, proves_current_business_behavior: false }];
  state.latest_feedback = structuredClone(state.read_receipts[0]!);
  const original = structuredClone(state);
  const view = contextView(bundle, state), read = (view.read_receipts as Json[])[0]!;
  assert.equal(read.content, content); assert.equal(read.fragment_sha256, sha(content));
  assert.equal(read.start_line, 1); assert.equal(read.end_line, 51);
  assert.equal(read.proves_current_business_behavior, false);
  assert.deepEqual((read.source_authorization_summary as Json).source_refs, authorizations.map(a => a.source_ref));
  assert.equal(read.source_authorization, undefined);
  assert.equal((view.latest_feedback as Json).content, undefined);
  assert.equal((view.latest_feedback as Json).receipt_ref, read.receipt_ref);
  assert.equal((view.latest_feedback as Json).details_ref, "read_receipts");
  assert.ok((view.allowed_evidence_refs as string[]).includes("read:rule"));
  assert.deepEqual(state, original, "模型投影不能篡改持久化审计对象");
  const beforeBytes = Buffer.byteLength(JSON.stringify([state.read_receipts, state.latest_feedback]));
  const afterBytes = Buffer.byteLength(JSON.stringify([view.read_receipts, view.latest_feedback]));
  assert.ok(afterBytes < beforeBytes / 2, `${afterBytes}/${beforeBytes}`);
  const previous = contextRef(bundle, state);
  state.model_calls_used++;
  assert.equal(contextRef(bundle, state), previous, "调用记账不改变证据身份");
  state.latest_feedback = { ...state.latest_feedback, status: "REJECTED", error_code: "SOURCE_ACCESS_UNAVAILABLE" };
  assert.deepEqual(contextView(bundle, state).latest_feedback, state.latest_feedback, "不得去掉拒绝原因");
});
