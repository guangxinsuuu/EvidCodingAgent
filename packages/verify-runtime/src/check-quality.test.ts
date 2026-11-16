import assert from "node:assert/strict";
import { test } from "node:test";
import { parseDecisionProposal, type DecisionProposal } from "@egap/contracts";
import { assessCheckQuality } from "./check-quality.js";
import type { ReleaseBundle } from "./types.js";

type CheckProposal = Extract<DecisionProposal, { action_kind: "CHECK_INTENT" }>;
type Spec = NonNullable<CheckProposal["payload"]["check_spec"]>;

/** 合成商品计量案例仅测通用结构规则；不复制客服公司合同、公开问题或其答案。 */
function bundle(): ReleaseBundle {
  return { schema_version: 1, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING",
    input_provenance: "CALIBRATION_SYNTHETIC", goal: "观察合成商品版本变化",
    baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40), catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40),
    requirement_refs: ["REQ-17", "REQ-18"], changes: [], materials: [], gaps: [],
    current_candidate_coverage: [], evaluation_status: "NOT_RUN",
    catalog: { status: "DRAFT", dispatch_authorized: false,
      requirements: [{ id: "REQ-17" }, { id: "REQ-18" }],
      scenarios: [{ id: "meter", stages: [{ stage_ref: "meter.before" }, { stage_ref: "meter.after" }] },
        { id: "catalog", stages: [{ stage_ref: "catalog.initial" }] }],
      sources: [{ id: "inventory", kind: "BUSINESS_FACT" }, { id: "policy", kind: "BUSINESS_RULE" },
        { id: "reference", kind: "REFERENCE_KNOWLEDGE" }, { id: "product", kind: "PRODUCT_EXECUTION" }] } };
}

function fullSpec(): Spec {
  return { schema_version: 1, stage_ref: "meter.after",
    inputs: [{ name: "units", value: "10" }],
    observations: [{ id: "stock", source_ref: "inventory", field: "stock.available" }],
    expectations: [{ observation_ref: "stock", operator: "EQUALS", value: "90", requirement_ref: "REQ-17" }],
    control: { scenario_ref: "meter", stage_ref: "meter.before", inputs: [{ name: "units", value: "0" }],
      observation_ref: "stock", expected_relation: "DIFFERENT", reason: "相同商品改变消费数量时，核对余量变化。" } };
}

function proposal(spec?: Spec): CheckProposal {
  const parsed = parseDecisionProposal({ schema_version: 1,
    proposal_id: "b700a836-eb63-4b59-a9f8-bc37f3011275", based_on_decision_revision: 0, candidate_epoch: 1,
    context_view_ref: "context:fixture", action_kind: "CHECK_INTENT", requirement_refs: ["REQ-17"],
    hypothesis_refs: [], evidence_refs: ["read:fixture"], expected_information: "核对商品余量变化", rationale: "变化触及数量传递",
    hypothesis_updates: [], payload: { capability_ref: "check:fixture", scenario_ref: "meter", objective: "商品消耗后余量一致",
      observation_requirements: ["读取库存余量"], ...(spec === undefined ? {} : { check_spec: spec }) } });
  assert.equal(parsed.action_kind, "CHECK_INTENT");
  return parsed as CheckProposal;
}

const codes = (spec?: Spec): string[] => assessCheckQuality(bundle(), proposal(spec)).issues.map(issue => issue.code);

test("旧 CHECK 仍可解析，但一次返回阶段、输入、观察、关系和对照所有缺项", () => {
  assert.deepEqual(codes(), ["CHECK_SPEC_REQUIRED", "STAGE_REQUIRED", "INPUTS_REQUIRED", "OBSERVATIONS_REQUIRED", "EXPECTATIONS_REQUIRED", "CONTROL_REQUIRED"]);
  assert.deepEqual(codes({ schema_version: 1 }), codes().slice(1));
});

test("完整规格只表示结构完整；不宣称语义正确或允许执行，且不修改输入", () => {
  const b = bundle(); const p = proposal(fullSpec()); const before = JSON.stringify({ b, p });
  assert.deepEqual(assessCheckQuality(b, p), { policy_version: "check-specificity-v1", status: "STRUCTURALLY_COMPLETE",
    issues: [], semantic_correctness: "NOT_ESTABLISHED", dispatch_qualified: false });
  assert.equal(JSON.stringify({ b, p }), before);
  // 一字符的合法值无需补成冗长文案；无意义的长故事也不会被当成语义证明。
  assert.deepEqual(codes({ ...fullSpec(), expectations: [{ ...fullSpec().expectations![0]!, value: "0" }] }), []);
});

test("完整 check_spec 不能掩盖旧 observation_requirements 的裸规则编号", () => {
  const p = proposal(fullSpec());
  const invalid = { ...p, payload: { ...p.payload, observation_requirements: ["REQ-17"] } };
  assert.equal(assessCheckQuality(bundle(), invalid).status, "NEEDS_REVISION");
  assert.ok(assessCheckQuality(bundle(), invalid).issues.some(issue => issue.code === "OBSERVATION_DETAILS_REQUIRED"));
  const generic = proposal();
  const incomplete = assessCheckQuality(bundle(), { ...generic, payload: { ...generic.payload, observation_requirements: ["REQ-17"] } });
  assert.ok(incomplete.issues.length > 1);
  assert.ok(incomplete.issues.some(issue => issue.code === "OBSERVATION_DETAILS_REQUIRED"));
  assert.ok(incomplete.issues.some(issue => issue.code === "STAGE_REQUIRED"));
});

test("阶段必须属于自己的场景；主实验和对照错误都保留", () => {
  const spec = fullSpec();
  const result = codes({ ...spec, stage_ref: "catalog.initial", control: { ...spec.control!, stage_ref: "catalog.initial" } });
  assert.equal(result.filter(code => code === "STAGE_NOT_IN_SCENARIO").length, 2);
  const p = proposal(spec);
  const unknown = assessCheckQuality(bundle(), { ...p, payload: { ...p.payload, scenario_ref: "unregistered" } });
  assert.ok(unknown.issues.some(issue => issue.code === "UNKNOWN_SCENARIO" && issue.path === "/payload/scenario_ref"));
});

test("缺输入值、重复参数名和无区分对照可同时定位，包括空值重复名", () => {
  const spec = fullSpec();
  const result = codes({ ...spec, inputs: [{ name: "units", value: " " }, { name: " units ", value: "TODO" }],
    control: { ...spec.control!, inputs: [{ name: "units", value: "10" }] } });
  assert.equal(result.filter(code => code === "INPUT_VALUE_REQUIRED").length, 2);
  assert.ok(result.includes("DUPLICATE_INPUT_NAME")); assert.ok(result.includes("CONTROL_NOT_DISTINGUISHABLE"));
});

test("只有同名参数不同取值才构成最低对照，换名或相同值不够", () => {
  const spec = fullSpec();
  for (const inputs of [[{ name: "other", value: "0" }], [{ name: "units", value: "10" }]]) {
    assert.ok(codes({ ...spec, control: { ...spec.control!, inputs } }).includes("CONTROL_NOT_DISTINGUISHABLE"));
  }
  assert.deepEqual(codes({ ...spec, control: { ...spec.control!, expected_relation: "SAME" } }), []);
});

test("观察只接受登记的事实/运行来源；规则、参考、源码、历史记录与环境不能代替业务观察", () => {
  const spec = fullSpec(); const b = bundle();
  for (const kind of ["BUSINESS_RULE", "REFERENCE_KNOWLEDGE", "CANDIDATE_CHANGE", "TEST_AND_RECIPE_IMPLEMENTATION", "HISTORICAL_EVIDENCE", "EXPERIMENT_ENVIRONMENT", "UNKNOWN_NEW_KIND"]) {
    b.catalog["sources"] = [{ id: "inventory", kind }];
    assert.ok(assessCheckQuality(b, proposal(spec)).issues.some(issue => issue.code === "SOURCE_NOT_OBSERVABLE"), kind);
  }
  for (const kind of ["BUSINESS_FACT", "IDENTITY_AUTHORITY", "PRODUCT_EXECUTION", "MODEL_EXECUTION"]) {
    b.catalog["sources"] = [{ id: "inventory", kind }];
    assert.equal(assessCheckQuality(b, proposal(spec)).status, "STRUCTURALLY_COMPLETE", kind);
  }
  b.catalog["sources"] = [];
  assert.ok(assessCheckQuality(b, proposal(spec)).issues.some(issue => issue.code === "UNKNOWN_OBSERVATION_SOURCE"));
});

test("观察标识、字段与重复 id 均有独立缺项；规则编号不是观察字段", () => {
  const spec = fullSpec();
  const result = codes({ ...spec, observations: [{ id: "stock", source_ref: "inventory", field: "REQ-17" },
    { id: "stock", source_ref: "missing", field: "TODO" }, { id: "", source_ref: "product", field: "" }] });
  for (const code of ["OBSERVATION_FIELD_REQUIRED", "DUPLICATE_OBSERVATION_ID", "UNKNOWN_OBSERVATION_SOURCE", "OBSERVATION_ID_REQUIRED"])
    assert.ok(result.includes(code), code);
});

test("预期必须绑定当前声明的观察和提案规则，裸规则编号不能充当预期值", () => {
  const spec = fullSpec();
  const result = codes({ ...spec, expectations: [{ observation_ref: "not-declared", operator: "EQUALS", value: "REQ-17", requirement_ref: "REQ-18" }] });
  for (const code of ["UNKNOWN_EXPECTATION_OBSERVATION", "EXPECTATION_REQUIREMENT_NOT_BOUND", "EXPECTED_VALUE_REQUIRED"])
    assert.ok(result.includes(code), code);
});

test("PRESENT/ABSENT 明确使用空值，其余关系拒绝空值或占位词", () => {
  const spec = fullSpec(); const base = spec.expectations![0]!;
  for (const operator of ["PRESENT", "ABSENT"] as const) {
    assert.deepEqual(codes({ ...spec, expectations: [{ ...base, operator, value: "" }] }), []);
    assert.ok(codes({ ...spec, expectations: [{ ...base, operator, value: "true" }] }).includes("PRESENCE_VALUE_MUST_BE_EMPTY"));
  }
  for (const operator of ["EQUALS", "NOT_EQUALS", "CONTAINS", "UNCHANGED", "GREATER_THAN", "LESS_THAN"] as const) {
    assert.ok(codes({ ...spec, expectations: [{ ...base, operator, value: "" }] }).includes("EXPECTED_VALUE_REQUIRED"), operator);
  }
});

test("数值关系不接受自然语言、非有限值、十六进制或隐式类型转换", () => {
  const spec = fullSpec(); const base = spec.expectations![0]!;
  for (const value of ["ten", "Infinity", "NaN", "0xff", "1e999"]) {
    assert.ok(codes({ ...spec, expectations: [{ ...base, operator: "GREATER_THAN", value }] }).includes("NUMERIC_EXPECTED_VALUE_REQUIRED"), value);
  }
  assert.deepEqual(codes({ ...spec, expectations: [{ ...base, operator: "LESS_THAN", value: "-0.5" }] }), []);
});

test("对照需要登记观察和具体原因；所有反馈带可修正路径且不回显提供的字符串", () => {
  const spec = fullSpec();
  const result = assessCheckQuality(bundle(), proposal({ ...spec,
    control: { ...spec.control!, observation_ref: "private-sentinel-not-registered", reason: "TODO" } }));
  assert.ok(result.issues.some(issue => issue.code === "UNKNOWN_CONTROL_OBSERVATION"));
  assert.ok(result.issues.some(issue => issue.code === "CONTROL_REASON_REQUIRED"));
  assert.ok(result.issues.every(issue => issue.path.startsWith("/payload/check_spec/") && issue.message.length > 0));
  assert.equal(JSON.stringify(result).includes("private-sentinel"), false);
});
