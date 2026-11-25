import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import type { PlanDraft, ReviewDecisionProposal, ReviewRequest } from "@egap/contracts";
import { assessReviewBasis, compilePlanDraft } from "./review-planning.js";
import { sha, type Json, type ReleaseBundle } from "./types.js";

type ReviewBundle = ReleaseBundle & { review_request: ReviewRequest };
type DraftProposal = Extract<ReviewDecisionProposal, { action_kind: "PLAN_DRAFT" }>;
const authority = JSON.parse(readFileSync(new URL("../../../protocols/verify-agent/capabilities/commercial-decision.v1.json", import.meta.url), "utf8")) as Json;
const entrypoint = authority["entrypoint"] as { path: string };
const source = readFileSync(new URL("../../../" + entrypoint.path, import.meta.url), "utf8");

/** 使用真实接口源码及一个显式合成条件改动；这里只验证编译边界，不执行候选或计算正确率。 */
function bundle(): ReviewBundle {
  const candidate = source.replace('instant(acceptance["accepted_at"]) > renewed', 'instant(acceptance["accepted_at"]) > claimed');
  assert.notEqual(candidate, source, "基线源码变动应使测试明确失败，不能静默变成无变化样本");
  const diff = '--- a/' + entrypoint.path + '\n+++ b/' + entrypoint.path + '\n@@ -270 +270 @@\n-            or instant(acceptance["accepted_at"]) > renewed:\n+            or instant(acceptance["accepted_at"]) > claimed:\n';
  const rule = "合同接受必须满足本地校准规则，且付款与客户绑定保持不变。";
  return {
    schema_version: 2, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING", input_provenance: "CALIBRATION_SYNTHETIC",
    goal: "审查合同接受时间条件改变", baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40),
    catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40), requirement_refs: ["R4"],
    changes: [{ path: entrypoint.path, diff_material_ref: "diff:commercial.py" }],
    materials: [
      { material_ref: "baseline:commercial.py", kind: "BASELINE_SOURCE", title: "基线", git_commit: "a".repeat(40), path: entrypoint.path, content: source, sha256: sha(source) },
      { material_ref: "candidate:commercial.py", kind: "CANDIDATE_SOURCE", title: "合成候选", git_commit: "b".repeat(40), path: entrypoint.path, content: candidate, sha256: sha(candidate) },
      { material_ref: "diff:commercial.py", kind: "UNTRUSTED_DIFF", title: "合成条件变化", git_commit: "b".repeat(40), path: entrypoint.path, content: diff, sha256: sha(diff) },
      { material_ref: "rule:R4", kind: "REGISTERED_RULE_EXCERPT", title: "本地规则", git_commit: "d".repeat(40), path: "business-source-pack/v1/policies/rules.md", content: rule, sha256: sha(rule) },
    ],
    catalog: { status: "DRAFT", dispatch_authorized: false,
      requirements: [{ id: "R4", material_ref: "rule:R4" }],
      sources: [{ id: "sut_runtime", kind: "PRODUCT_EXECUTION" }],
      scenarios: [{ id: "commercial_unit", stages: [{ stage_ref: "commercial_unit.evaluate" }] }] },
    planning_contracts: [authority], gaps: [], current_candidate_coverage: [], evaluation_status: "NOT_RUN",
    review_request: {
      schema_version: 1, request_id: "review:compile-fixture", revision: 1, requester_ref: "operator:local", owner_ref: "owner:local",
      goal: "审查合同接受时间条件改变", non_goals: ["不执行实验"],
      baseline: { commit: "a".repeat(40), reason: "本地校准基线", source_ref: "git:baseline" },
      candidate: { commit: "b".repeat(40), source_ref: "git:candidate" },
      requirements: [{ id: "R4", kind: "PRESERVE", statement: "保持合同接受时点语义", material_ref: "rule:R4",
        authority: "LOCAL_CALIBRATION", status: "CONFIRMED_LOCAL", owner_ref: "owner:local" }],
      known_gaps: [], predecessor_run_id: null,
    },
  } as ReviewBundle;
}

function draft(): PlanDraft {
  return {
    objective: "区分续费后才接受合同的候选行为", requirement_ref: "R4", diff_ref: "diff:commercial.py", changed_fields: ["accepted_at"], claim: "MAY_CHANGE",
    inputs: [{ name: "commercial_source.contracts.0.acceptance.accepted_at", value: "2026-11-05T00:00:00Z" }],
    control_inputs: [{ name: "commercial_source.contracts.0.acceptance.accepted_at", value: "2026-11-01T00:00:00Z" }],
    observation_field: "decision.eligibility", operator: "EQUALS", expected_value: "NEEDS_MORE_EVIDENCE",
    expected_baseline: "NEEDS_MORE_EVIDENCE", expected_candidate: "ELIGIBLE", control_relation: "DIFFERENT",
    control_reason: "只改变合同接受时点；其他 fixture 前提不由编译器修改。",
    change_reason: "候选把接受时点上界改为申请时间，需要核对两者之间的接受记录。",
  };
}
function proposal(value: PlanDraft = draft()): DraftProposal {
  return { schema_version: 3, proposal_id: "6605004c-6a27-4c25-a6ca-f508d6eeb5f1", based_on_decision_revision: 3,
    candidate_epoch: 1, context_view_ref: "context:review-plan", action_kind: "PLAN_DRAFT", requirement_refs: ["R4"],
    hypothesis_refs: ["hypothesis:acceptance"], evidence_refs: ["diff:commercial.py", "rule:R4"], expected_information: "检验合同接受时点关系",
    rationale: "需要行为观察来核对边界条件", hypothesis_updates: [],
    decision_frame: { unresolved_questions: [{ id: "q:acceptance", question: "候选是否追认晚接受合同？", why_unresolved: "当前只有源码差异，没有执行结果。", evidence_refs: ["diff:commercial.py"] }],
      selected_question_id: "q:acceptance", outcome_branches: [
        { observation: "草案绑定通过", implication: "还需主图选择是否接受计划", next_action: "ACCEPT_PLAN" },
        { observation: "草案缺项", implication: "按回执修正并保留原提案", next_action: "PLAN_DRAFT" },
      ] }, payload: { draft: value } };
}

test("上游要求局部缺项只限制相关义务，不抹掉完整规则或授予执行", () => {
  const b = bundle(); const original = b.review_request.requirements[0]!;
  b.requirement_refs.push("R5");
  (b.catalog["requirements"] as Json[]).push({ id: "R5", material_ref: "rule:R5" });
  b.materials.push({ ...b.materials[3]!, material_ref: "rule:R5" });
  b.review_request = { ...b.review_request, requirements: [original, { ...original, id: "R5", material_ref: "rule:R5", status: "CONFLICT" }] };
  const before = JSON.stringify(b); const result = assessReviewBasis(b);
  assert.deepEqual(result.eligible_requirement_refs, ["R4"]);
  assert.equal(result.local_planning_ready, true); assert.equal(result.execution_authorized, false);
  assert.deepEqual(result.gaps.map(row => [row.code, row.requirement_refs]), [["REQUIREMENT_CONFLICT", ["R5"]]]);
  assert.equal(JSON.stringify(b), before);
});

test("未确认权威、错配规则、伪造规则类型与缺少内容均保留具体缺项", () => {
  const b = bundle();
  b.review_request = { ...b.review_request, requirements: [{ ...b.review_request.requirements[0]!, authority: "UNVERIFIED", status: "PENDING", material_ref: "candidate:commercial.py" }] };
  const result = assessReviewBasis(b);
  assert.deepEqual(result.eligible_requirement_refs, []);
  assert.deepEqual(result.gaps.map(row => row.code), ["REQUIREMENT_AUTHORITY_UNVERIFIED", "REQUIREMENT_CONFIRMATION_PENDING", "REQUIREMENT_CATALOG_BINDING_MISSING", "REQUIREMENT_MATERIAL_MISSING"]);
  const empty = bundle(); empty.materials[3]!.content = " ";
  assert.ok(assessReviewBasis(empty).gaps.some(row => row.code === "REQUIREMENT_MATERIAL_MISSING"));
});

test("全局缺项与无要求阻断检查规划，不能补默认R1-R6", () => {
  const b = bundle(); b.review_request = { ...b.review_request, known_gaps: [{ id: "gap:scope", requirement_refs: [], description: "待确认适用客户范围" }] };
  assert.equal(assessReviewBasis(b).local_planning_ready, false);
  const noRequirements = bundle(); noRequirements.review_request = { ...noRequirements.review_request, requirements: [] };
  assert.deepEqual(assessReviewBasis(noRequirements).eligible_requirement_refs, []);
  assert.equal(assessReviewBasis(noRequirements).gaps[0]!.code, "REVIEW_REQUIREMENTS_MISSING");
  const wrongScope = bundle(); wrongScope.review_request = { ...wrongScope.review_request,
    known_gaps: [{ id: "gap:typo", requirement_refs: ["R404"], description: "缺项作用域拼错" }] };
  assert.equal(assessReviewBasis(wrongScope).local_planning_ready, false);
  assert.ok(assessReviewBasis(wrongScope).gaps.some(row => row.code === "INPUT_GAP_SCOPE_UNKNOWN"));
});

test("草案字段一次列全且不代填业务值；对照/预测/断言仍由模型决定", () => {
  const p = proposal({ objective: "检查变化" }); const before = JSON.stringify(p);
  const result = compilePlanDraft(bundle(), p, authority);
  assert.equal(result.status, "NEEDS_REVISION"); assert.equal(result.proposal, null);
  assert.equal(result.issues.length, 14);
  assert.ok(result.issues.some(row => row.path === "/payload/draft/expected_value"));
  assert.ok(result.issues.some(row => row.path === "/payload/draft/control_inputs"));
  assert.equal(result.binding, undefined); assert.equal(JSON.stringify(p), before);
});

test("完整草案机械编译到真实接口并复用质量门禁；不改变模型选择和前提", () => {
  const b = bundle(), p = proposal(); const before = JSON.stringify({ b, p, authority });
  const result = compilePlanDraft(b, p, authority);
  assert.deepEqual(result.issues, []); assert.equal(result.status, "BOUND_PLAN");
  assert.equal(result.execution_ready, false); assert.equal(result.dispatch_qualified, false); assert.equal(result.semantic_correctness, "NOT_ESTABLISHED");
  assert.equal(JSON.stringify({ b, p, authority }), before);
  const spec = result.proposal!.payload.check_spec!;
  assert.equal(spec.stage_ref, "commercial_unit.evaluate");
  assert.deepEqual(spec.inputs, p.payload.draft.inputs);
  assert.deepEqual(spec.control!.inputs, p.payload.draft.control_inputs);
  assert.equal(spec.expectations![0]!.value, p.payload.draft.expected_value);
  assert.equal(spec.change_relation!.expected_candidate, p.payload.draft.expected_candidate);
  assert.deepEqual(spec.change_relation!.output_refs, ["primary"]);
  assert.equal(result.binding!.compiled_plan!["execution_status"], "NOT_EXECUTED");
  assert.deepEqual(compilePlanDraft(b, p, authority), result, "纯编译必须确定性");
});

test("输入非法和未触及diff会定位到草案字段，不能被结构完整掩盖", () => {
  const result = compilePlanDraft(bundle(), proposal({ ...draft(), inputs: [{ name: "commercial_source.contracts.0.acceptance.accepted_at", value: "2026-02-30T00:00:00Z" }], changed_fields: ["not_changed"] }), authority);
  assert.equal(result.status, "NEEDS_REVISION");
  assert.ok(result.issues.some(row => row.code === "INPUT_VALUE_TYPE_INVALID" && row.path === "/payload/draft/inputs/0/value"));
  assert.ok(result.issues.some(row => row.code === "CHANGED_FIELD_NOT_IN_DIFF" && row.path === "/payload/draft/changed_fields/0"));
  assert.equal(result.binding!.compiled_plan, undefined);
});

test("缺项要求与夹带额外义务不能编译成有资格计划", () => {
  const b = bundle(); b.review_request = { ...b.review_request, requirements: [{ ...b.review_request.requirements[0]!, status: "PENDING" }] };
  const result = compilePlanDraft(b, proposal(), authority);
  assert.equal(result.proposal, null); assert.ok(result.issues.some(row => row.code === "REQUIREMENT_NOT_PLANNING_ELIGIBLE"));
  const p = proposal(); const extra = compilePlanDraft(bundle(), { ...p, requirement_refs: ["R4", "R5"] }, authority);
  assert.ok(extra.issues.some(row => row.code === "DRAFT_REQUIREMENT_SCOPE_MISMATCH"));
});

test("同值因素对照被拒绝，SHOULD_PRESERVE可以预测B/C保持一致", () => {
  const sameControl = compilePlanDraft(bundle(), proposal({ ...draft(), control_inputs: draft().inputs! }), authority);
  assert.ok(sameControl.issues.some(row => row.code === "BOUND_CONTROL_NOT_DISTINGUISHABLE" && row.path === "/payload/draft/control_inputs"));
  const preserve = compilePlanDraft(bundle(), proposal({ ...draft(), claim: "SHOULD_PRESERVE", expected_candidate: "NEEDS_MORE_EVIDENCE" }), authority);
  assert.equal(preserve.status, "BOUND_PLAN", "保留行为不应强行预测B/C不同");
});

test("缺受保护能力与未观测输出不允许编译器自行创造接口", () => {
  const result = compilePlanDraft(bundle(), proposal(), {});
  assert.equal(result.proposal, null); assert.ok(result.issues.some(row => row.code === "AUTHORITY_CONTRACT_INVALID"));
  const invented = compilePlanDraft(bundle(), proposal({ ...draft(), observation_field: "decision.refund_approved" }), authority);
  assert.ok(invented.issues.some(row => row.code === "OUTPUT_NOT_BOUND" && row.path === "/payload/draft/observation_field"));
});
