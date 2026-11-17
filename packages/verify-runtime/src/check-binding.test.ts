import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parseDecisionProposal, type DecisionProposal } from "@egap/contracts";
import { assessCheckBinding } from "./check-binding.js";
import { sha, type Json, type ReleaseBundle } from "./types.js";

type Check = Extract<DecisionProposal, { action_kind: "CHECK_INTENT" }>;
type Spec = NonNullable<Check["payload"]["check_spec"]>;
const authority = JSON.parse(readFileSync(new URL("../../../protocols/verify-agent/capabilities/commercial-decision.v1.json", import.meta.url), "utf8")) as Json;
const entrypoint = authority["entrypoint"] as { path: string; symbol: string };
const source = readFileSync(new URL("../../../" + entrypoint.path, import.meta.url), "utf8");
const fixture = authority["fixture"] as { id: string; context: Json };

/** 真函数/真字段合同 + 合成 Git 身份；追加注释仅测引用编译，不宣称业务发生变化。 */
function bundle(): ReleaseBundle {
  const candidate = source + "\n# billing_paid is the observation under review.\n";
  const diff = "--- a/" + entrypoint.path + "\n+++ b/" + entrypoint.path + "\n@@ -1 +1 @@\n+# billing_paid is the observation under review.\n";
  return { schema_version: 1, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING",
    input_provenance: "CALIBRATION_SYNTHETIC", goal: "Test a strictly bound proposal without running the business.",
    baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40), catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40),
    requirement_refs: ["R4"], changes: [{ path: entrypoint.path, diff_material_ref: "diff:commercial.py" }],
    materials: [
      { material_ref: "baseline:commercial.py", kind: "BASELINE_SOURCE", title: "基线函数", git_commit: "a".repeat(40), path: entrypoint.path, content: source, sha256: sha(source) },
      { material_ref: "candidate:commercial.py", kind: "CANDIDATE_SOURCE", title: "合成注释变更", git_commit: "b".repeat(40), path: entrypoint.path, content: candidate, sha256: sha(candidate) },
      { material_ref: "diff:commercial.py", kind: "UNTRUSTED_DIFF", title: "合成差异", git_commit: "b".repeat(40), path: entrypoint.path, content: diff, sha256: sha(diff) },
    ], catalog: { status: "DRAFT", dispatch_authorized: false }, planning_contracts: [authority],
    gaps: [], current_candidate_coverage: [], evaluation_status: "NOT_RUN" };
}
function spec(): Spec {
  return { schema_version: 1, binding: { contract_ref: String(authority["id"]), fixture_ref: fixture.id },
    stage_ref: "commercial_unit.evaluate", inputs: [{ name: "commercial_source.payment.status", value: "UNPAID" }],
    observations: [{ id: "eligibility", source_ref: "sut_runtime", field: "decision.eligibility" }],
    expectations: [{ observation_ref: "eligibility", operator: "EQUALS", value: "NEEDS_MORE_EVIDENCE", requirement_ref: "R4" }],
    control: { scenario_ref: "commercial_unit", stage_ref: "commercial_unit.evaluate",
      inputs: [{ name: "commercial_source.payment.status", value: "PAID" }], observation_ref: "eligibility", expected_relation: "DIFFERENT",
      reason: "付款状态是主实验和对照唯一被覆写的因素。" },
    change_relation: { diff_ref: "diff:commercial.py", changed_fields: ["billing_paid"], input_refs: ["commercial_source.payment.status"],
      output_refs: ["eligibility"], requirement_ref: "R4", claim: "MAY_CHANGE", expected_baseline: "NEEDS_MORE_EVIDENCE",
      expected_candidate: "ELIGIBLE", reason: "这是待证伪预测；注释变化本身不能证明付款行为改变。" } };
}
function proposal(value: Spec = spec()): Check {
  return parseDecisionProposal({ schema_version: 1, proposal_id: "6605004c-6a27-4c25-a6ca-f508d6eeb5f1", based_on_decision_revision: 1,
    candidate_epoch: 1, context_view_ref: "context:binding-test", action_kind: "CHECK_INTENT", requirement_refs: ["R4"],
    hypothesis_refs: [], evidence_refs: ["diff:commercial.py"], expected_information: "区分付款状态变化与判断输出关系。",
    rationale: "编译可观察的字段绑定，再交给独立执行层核验。", hypothesis_updates: [],
    payload: { capability_ref: "probe.commercial_decision", scenario_ref: "commercial_unit", objective: "核验付款事实与资格判断关系",
      observation_requirements: ["取得函数返回的资格判断字段"], check_spec: value } }) as Check;
}
const result = (value: Spec, contract: unknown = authority) => {
  const b = bundle(); b.planning_contracts = [contract as Json];
  return assessCheckBinding(b, proposal(value), contract);
};
const codes = (value: Spec, contract: unknown = authority) => result(value, contract).issues.map(issue => issue.code);

test("绑定真实commercial_decision合同：从可信fixture编译typed输入，不改输入也不授予执行", () => {
  const b = bundle(), p = proposal(); const before = JSON.stringify({ b, p, authority });
  const assessed = assessCheckBinding(b, p, authority);
  assert.deepEqual(assessed.issues, []);
  assert.equal(assessed.status, "BOUND_PLAN");
  assert.equal(assessed.execution_ready, false); assert.equal(assessed.dispatch_qualified, false);
  assert.equal(assessed.semantic_correctness, "NOT_ESTABLISHED");
  assert.equal(JSON.stringify({ b, p, authority }), before);
  const plan = assessed.compiled_plan!;
  assert.deepEqual(plan["entrypoint"], entrypoint);
  assert.equal(((plan["primary_context"] as Json)["commercial_source"] as { payment: { status: string } }).payment.status, "UNPAID");
  assert.equal(((plan["control_context"] as Json)["commercial_source"] as { payment: { status: string } }).payment.status, "PAID");
  assert.deepEqual((plan["primary_context"] as Json)["billing"], fixture.context["billing"], "未覆写字段仅从fixture取默认值");
  assert.ok((plan["stage_prerequisites"] as Json[]).every(row => row["evaluation_status"] === "NOT_EVALUATED"));
  assert.equal(plan["execution_status"], "NOT_EXECUTED");
  assert.deepEqual(assessCheckBinding(b, p, authority), assessed, "编译结果必须确定性");
});

test("未知能力、伪造合同/fixture、漂移摘要均失败关闭", () => {
  const p = proposal();
  const unsupported = assessCheckBinding(bundle(), { ...p, payload: { ...p.payload, capability_ref: "check.business" } }, authority);
  assert.deepEqual(unsupported.issues.map(issue => issue.code), ["CAPABILITY_NOT_BOUND"]);
  assert.equal(unsupported.compiled_plan, undefined);
  assert.ok(codes({ ...spec(), binding: { contract_ref: "model-created", fixture_ref: "invented" } }).includes("CONTRACT_REF_MISMATCH"));
  assert.ok(codes({ ...spec(), binding: { contract_ref: "model-created", fixture_ref: "invented" } }).includes("FIXTURE_REF_MISMATCH"));
  const bad = structuredClone(authority); (bad["fixture"] as Json)["sha256"] = "f".repeat(64);
  assert.ok(codes(spec(), bad).includes("FIXTURE_DIGEST_MISMATCH"));
  assert.ok(codes(spec(), {}).includes("AUTHORITY_CONTRACT_INVALID"));
});

test("必须包含准确入口源码；基线变更后不能沿用旧合同", () => {
  const missing = bundle(); missing.materials = missing.materials.filter(row => row.kind !== "BASELINE_SOURCE");
  assert.ok(assessCheckBinding(missing, proposal(), authority).issues.some(issue => issue.code === "TARGET_SOURCE_MISSING"));
  const drift = bundle(); drift.materials[0]!.content += "# unrelated drift";
  drift.materials[0]!.sha256 = sha(drift.materials[0]!.content);
  assert.ok(assessCheckBinding(drift, proposal(), authority).issues.some(issue => issue.code === "BASELINE_CONTRACT_SOURCE_MISMATCH"));
  const legacy = bundle(); delete legacy.planning_contracts;
  assert.ok(assessCheckBinding(legacy, proposal(), authority).issues.some(issue => issue.code === "BINDING_CONTRACT_NOT_IN_BUNDLE"));
});

test("输入字段必须已绑定；原型字段和模型自造前置条件不成为fixture内容", () => {
  for (const name of ["billing.new_balance", "__proto__.polluted", "commercial_source.constructor.prototype.bad", "prerequisites.completed"]) {
    const evaluated = result({ ...spec(), inputs: [{ name, value: "true" }] });
    assert.ok(evaluated.issues.some(issue => issue.code === "INPUT_NOT_BOUND"), name);
    assert.equal(evaluated.compiled_plan, undefined);
  }
  assert.equal(({} as Json)["polluted"], undefined);
});

test("真实输入类型、枚举、日期和范围都严格验证；不会把false字符串当真值", () => {
  for (const [name, value] of [
    ["commercial_source.payment.status", "SETTLED"], ["billing.total_amount_cents", "1.0"],
    ["billing.total_amount_cents", "-1"], ["billing.total_amount_cents", "1000000001"],
    ["commercial_source.contracts.0.terms.special_terms", "False"], ["commercial_source.claim.claimed_at", "2026-02-30T00:00:00Z"],
    ["commercial_source.claim.claimed_at", "2026-11-09T00:00:00"], ["commercial_source.claim.claimed_at", "2026-11-09T24:00:00Z"],
    ["commercial_source.claim.claimed_at", "0000-01-01T00:00:00Z"],
  ]) assert.ok(codes({ ...spec(), inputs: [{ name: name!, value: value! }] }).includes("INPUT_VALUE_TYPE_INVALID"), name + ":" + value);
  const s = spec();
  const evaluated = result({ ...s, inputs: [{ name: "commercial_source.contracts.0.terms.special_terms", value: "true" }],
    control: { ...s.control!, inputs: [{ name: "commercial_source.contracts.0.terms.special_terms", value: "false" }] },
    change_relation: { ...s.change_relation!, input_refs: ["commercial_source.contracts.0.terms.special_terms"] } });
  assert.equal(evaluated.status, "BOUND_PLAN");
  assert.equal((((evaluated.compiled_plan!["control_context"] as Json)["commercial_source"] as { contracts: { terms: { special_terms: boolean } }[] }).contracts[0]!.terms.special_terms), false);
});

test("场景和阶段绑定不允许用其他已登记场景伪装已完成前置阶段", () => {
  const s = spec();
  assert.ok(codes({ ...s, stage_ref: "v2_boundary.final" }).includes("PRIMARY_STAGE_NOT_BOUND"));
  assert.ok(codes({ ...s, control: { ...s.control!, scenario_ref: "v1_history", stage_ref: "v1_history.initial" } }).includes("CONTROL_STAGE_NOT_BOUND"));
});

test("观察必须是函数真实输出和运行来源，不能把规则或自造字段作为输出", () => {
  const s = spec();
  const bad = codes({ ...s, observations: [{ id: "eligibility", source_ref: "business_rules", field: "decision.is_refundable" }] });
  assert.ok(bad.includes("OBSERVATION_SOURCE_NOT_BOUND")); assert.ok(bad.includes("OUTPUT_NOT_BOUND"));
  assert.ok(codes({ ...s, expectations: [] }).includes("OBSERVATION_WITHOUT_ASSERTION"));
  assert.ok(codes({ ...s, expectations: [] }).includes("REQUIREMENT_WITHOUT_ASSERTION"));
});

test("输出枚举/算子/数组成员/nullable正确区分；不把合法UNKNOWN枚举认成占位词", () => {
  const s = spec(), e = s.expectations![0]!;
  assert.ok(codes({ ...s, expectations: [{ ...e, value: "APPROVED" }] }).includes("ASSERTION_VALUE_TYPE_INVALID"));
  assert.ok(codes({ ...s, expectations: [{ ...e, operator: "GREATER_THAN", value: "10" }] }).includes("ASSERTION_OPERATOR_NOT_BOUND"));
  assert.ok(codes({ ...s, expectations: [{ ...e, operator: "PRESENT", value: "" }] }).includes("ASSERTION_VALUE_TYPE_INVALID"));
  const custom = structuredClone(authority);
  ((custom["outputs"] as Json[]).find(row => row["field"] === "decision.eligibility")!["enum"] as string[]).push("UNKNOWN");
  assert.equal(result({ ...s, expectations: [{ ...e, value: "UNKNOWN" }],
    change_relation: { ...s.change_relation!, expected_baseline: "UNKNOWN" } }, custom).status, "BOUND_PLAN");
  const arraySpec: Spec = { ...s, observations: [{ id: "eligibility", source_ref: "sut_runtime", field: "decision.reason_codes" }],
    expectations: [{ ...e, operator: "CONTAINS", value: "LOCAL_LAB_ADVISORY_ONLY" }],
    change_relation: { ...s.change_relation!, expected_baseline: '["A"]', expected_candidate: '["B"]' } };
  assert.equal(result(arraySpec).status, "BOUND_PLAN");
  const nullable: Spec = { ...s, observations: [{ id: "eligibility", source_ref: "sut_runtime", field: "decision.billing_paid" }],
    expectations: [{ ...e, operator: "ABSENT", value: "" }], change_relation: { ...s.change_relation!, expected_baseline: "null", expected_candidate: "true" } };
  assert.equal(result(nullable).status, "BOUND_PLAN");
  const numeric: Spec = { ...s, observations: [{ id: "eligibility", source_ref: "sut_runtime", field: "decision.elapsed_seconds" }],
    expectations: [{ ...e, operator: "GREATER_THAN", value: "null" }],
    change_relation: { ...s.change_relation!, expected_baseline: "10", expected_candidate: "20" } };
  assert.ok(codes(numeric).includes("ASSERTION_VALUE_TYPE_INVALID"), "可空输出允许EQUALS null，但不能比较大于null");
});

test("对照和变更关系必须引用实际输入/观察；符号必须位于真实diff改动行", () => {
  const s = spec();
  assert.ok(codes({ ...s, control: { ...s.control!, inputs: s.inputs! } }).includes("BOUND_CONTROL_NOT_DISTINGUISHABLE"));
  assert.ok(codes({ ...s, change_relation: { ...s.change_relation!, input_refs: ["guessed.input"], output_refs: ["guessed.output"], changed_fields: ["unmodified_symbol"] } }).includes("CHANGED_FIELD_NOT_IN_DIFF"));
  const bad = codes({ ...s, change_relation: { ...s.change_relation!, diff_ref: "diff:another-candidate", input_refs: ["missing.input"], output_refs: ["missing-output"] } });
  assert.ok(bad.includes("CHANGE_DIFF_NOT_BOUND")); assert.ok(bad.includes("CHANGE_INPUT_NOT_BOUND")); assert.ok(bad.includes("CHANGE_OUTPUT_NOT_BOUND"));
  const { change_relation: omitted, ...noRelation } = s; void omitted;
  assert.ok(codes(noRelation).includes("CHANGE_RELATION_REQUIRED"));
});

test("B/C预测按真实输出类型解析，MAY_CHANGE与SHOULD_PRESERVE不能自相矛盾", () => {
  const s = spec();
  assert.ok(codes({ ...s, change_relation: { ...s.change_relation!, expected_candidate: "not-an-enum" } }).includes("CHANGE_EXPECTATION_TYPE_INVALID"));
  assert.ok(codes({ ...s, change_relation: { ...s.change_relation!, expected_candidate: "NEEDS_MORE_EVIDENCE" } }).includes("CHANGE_EXPECTED_RELATION_INCONSISTENT"));
  assert.ok(codes({ ...s, change_relation: { ...s.change_relation!, claim: "SHOULD_PRESERVE" } }).includes("CHANGE_EXPECTED_RELATION_INCONSISTENT"));
  assert.equal(result({ ...s, change_relation: { ...s.change_relation!, claim: "SHOULD_PRESERVE", expected_candidate: "NEEDS_MORE_EVIDENCE" } }).status, "BOUND_PLAN");
});

test("多输出预测必须完整按观察id索引，不允许遗漏或附带模型自造字段", () => {
  const s = spec();
  const multiple: Spec = { ...s, observations: [...s.observations!, { id: "paid", source_ref: "sut_runtime", field: "decision.billing_paid" }],
    expectations: [...s.expectations!, { observation_ref: "paid", operator: "EQUALS", value: "null", requirement_ref: "R4" }],
    change_relation: { ...s.change_relation!, output_refs: ["eligibility", "paid"],
      expected_baseline: '{"eligibility":"NEEDS_MORE_EVIDENCE","paid":null}',
      expected_candidate: '{"eligibility":"ELIGIBLE","paid":true}' } };
  assert.equal(result(multiple).status, "BOUND_PLAN");
  assert.ok(codes({ ...multiple, change_relation: { ...multiple.change_relation!, expected_candidate: '{"eligibility":"ELIGIBLE"}' } }).includes("CHANGE_EXPECTATION_TYPE_INVALID"));
});
