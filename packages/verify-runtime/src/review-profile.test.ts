import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { Ajv2020 } from "ajv/dist/2020.js";
import { parseReviewDecisionProposal, reviewDecisionProposalSchema, type ReviewDecisionProposal } from "@egap/contracts";
import { contextRef } from "./context.js";
import { allowedReviewActions, reviewModelSchema, reviewObservationBasis, reviewPlanBasis, reviewProposalError } from "./review-profile.js";
import { digest, sha, type Json, type ReleaseBundle, type RunState } from "./types.js";

const authority = JSON.parse(readFileSync(new URL("../../../protocols/verify-agent/capabilities/commercial-decision.v1.json", import.meta.url), "utf8")) as Json;
function bundle(): ReleaseBundle {
  const rule = "规则正文第一行\n规则正文第二行";
  const diff = "- old_condition\n+ new_condition";
  return {
    schema_version: 2, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING", input_provenance: "CALIBRATION_SYNTHETIC",
    goal: "测试主图决策边界", baseline_sha: "a".repeat(40), candidate_sha: "b".repeat(40),
    catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40), requirement_refs: ["R4"],
    changes: [{ path: "source.py", diff_material_ref: "diff:change" }],
    materials: [
      { material_ref: "rule:R4", kind: "REGISTERED_RULE_EXCERPT", title: "本地规则", git_commit: "d".repeat(40), path: "rules.md", content: rule, sha256: sha(rule) },
      { material_ref: "diff:change", kind: "UNTRUSTED_DIFF", title: "差异", git_commit: "b".repeat(40), path: "source.py", content: diff, sha256: sha(diff) },
    ],
    catalog: { status: "DRAFT", dispatch_authorized: false, requirements: [{ id: "R4", material_ref: "rule:R4" }],
      capabilities: [], scenarios: [], sources: [] }, planning_contracts: [authority], gaps: [],
    current_candidate_coverage: [], evaluation_status: "NOT_RUN",
    review_request: { schema_version: 1, request_id: "review:profile", revision: 1, requester_ref: "operator:local", owner_ref: "owner:local",
      goal: "测试主图决策边界", non_goals: [], baseline: { commit: "a".repeat(40), reason: "本地基线", source_ref: "git:baseline" },
      candidate: { commit: "b".repeat(40), source_ref: "git:candidate" },
      requirements: [{ id: "R4", kind: "PRESERVE", statement: "保持已确认行为", material_ref: "rule:R4", authority: "LOCAL_CALIBRATION", status: "CONFIRMED_LOCAL", owner_ref: "owner:local" }],
      known_gaps: [], predecessor_run_id: null },
  };
}
function state(started = false): RunState {
  const result: RunState = { run_id: "11111111-1111-4111-8111-111111111111", status: "ACTIVE", candidate_epoch: 1,
    decision_basis_revision: 1, event_seq: 0, config_digest: "config:test", limits: { model_calls: 6, reads: 4, no_progress: 2, plan_repairs: 1 },
    model_calls_used: 0, reads_used: 0, no_progress: 0, hypotheses: started ? [{ id: "hypothesis:change", statement: "变更可能影响业务条件", status: "OPEN", evidence_refs: ["diff:change"] }] : [],
    read_receipts: [], latest_feedback: null, stop_reason: null,
    review_cycle: { directions: [], decisions_completed: started ? 1 : 0, closure_calls_used: 0 } };
  if (started) result.review_cycle!.directions.push({ proposal_id: "first:impact", focus: "业务条件", evidence_refs: ["diff:change"],
    observation_basis_digest: reviewObservationBasis(bundle(), result) });
  return result;
}
function proposal(action_kind: ReviewDecisionProposal["action_kind"], payload: Json): ReviewDecisionProposal {
  return parseReviewDecisionProposal({ schema_version: 3, proposal_id: "6605004c-6a27-4c25-a6ca-f508d6eeb5f1",
    based_on_decision_revision: 1, candidate_epoch: 1, context_view_ref: "context:test", action_kind,
    requirement_refs: ["R4"], hypothesis_refs: ["hypothesis:change"], evidence_refs: ["diff:change"],
    expected_information: "查清当前条件是否改变业务行为", rationale: "根据未决问题选择有界动作", hypothesis_updates: [],
    decision_frame: { unresolved_questions: [{ id: "question:change", question: "条件变化是否改变既定行为？", why_unresolved: "仅看到差异，尚无执行观察。", evidence_refs: ["diff:change"] }],
      selected_question_id: "question:change", outcome_branches: [
        { observation: "规则适用且资料完整", implication: "可形成实验草案", next_action: "PLAN_DRAFT" },
        { observation: "规则冲突或资料不足", implication: "继续读取或澄清", next_action: "CLARIFY" },
      ] }, payload });
}
function body(value: ReviewDecisionProposal): Json {
  const transport = new Set(["schema_version", "proposal_id", "based_on_decision_revision", "candidate_epoch", "context_view_ref"]);
  return Object.fromEntries(Object.entries(value).filter(([key]) => !transport.has(key)));
}
function validator(b: ReleaseBundle, s: RunState, grounded: readonly string[] = []) {
  return new Ajv2020({ strict: true, allErrors: true }).compile(reviewModelSchema(b, s, grounded));
}
const observed = new Set(["diff:change"]);

test("生成Schema按阶段收窄动作，不能改动共享协议或第一轮直接规划", () => {
  const b = bundle(), s = state(); const before = digest(reviewDecisionProposalSchema);
  const validate = validator(b, s);
  const direction = proposal("UPDATE_DIRECTION", { focus: "追踪变更条件" });
  assert.equal(validate(body(direction)), true);
  assert.equal(validate(body(proposal("PLAN_DRAFT", { draft: { objective: "提前规划" } }))), false);
  assert.deepEqual(allowedReviewActions(b, s), ["UPDATE_DIRECTION", "CLARIFY", "STOP"]);
  assert.equal(digest(reviewDecisionProposalSchema), before, "动态枚举不能污染全局协议");
});

test("共享字段压缩后仍拒绝未知引用、额外权限字段和缺失的审慎决策框架", () => {
  const b = bundle(), s = state(true);
  for (let i = 0; i < 7; i++) s.read_receipts.push({ receipt_ref: "read:source-" + i,
    material_ref: "material:source-" + i, start_line: 1, end_line: 2 });
  const schema = reviewModelSchema(b, s), validate = validator(b, s);
  const good = body(proposal("READ", { material_ref: "rule:R4", start_line: 1, end_line: 2 }));
  assert.equal(validate(good), true);
  for (const change of [
    (v: Json) => { v.evidence_refs = ["read:unobserved"]; },
    (v: Json) => { delete v.decision_frame; },
    (v: Json) => { v.release_authorized = true; },
    (v: Json) => { (v.decision_frame as Json).unresolved_questions = []; },
  ]) { const invalid = structuredClone(good); change(invalid); assert.equal(validate(invalid), false); }
  // 与原来的逐分支内联表示比较，检查压缩的是重复 Schema，而非约束或材料。
  const expanded = structuredClone(schema);
  for (const branch of expanded.oneOf as Json[]) {
    const properties = branch.properties as Json;
    for (const [key, value] of Object.entries(properties)) {
      const ref = (value as Json).$ref;
      if (typeof ref === "string" && ref.startsWith("#/$defs/reviewDecisionField_"))
        properties[key] = (expanded.$defs as Json)[ref.slice("#/$defs/".length)];
    }
  }
  for (const key of Object.keys(expanded.$defs as Json))
    if (key.startsWith("reviewDecisionField_")) delete (expanded.$defs as Json)[key];
  const expandedValidate = new Ajv2020({ strict: true }).compile(expanded);
  assert.equal(expandedValidate(good), true);
  assert.ok(Buffer.byteLength(JSON.stringify(schema)) < Buffer.byteLength(JSON.stringify(expanded)) * 0.8);
});

test("没有可读材料时只移除READ，不误删初始STOP", () => {
  const b = bundle(); b.materials = [];
  const validate = validator(b, state());
  const stop = proposal("STOP", { reason: "材料不足，记录未完成" });
  // 无材料意味着没有已观察引用；本例只验证 STOP 可用，不借虚构 diff 绕过证据枚举。
  const noEvidence = { ...stop, evidence_refs: [], hypothesis_refs: [], hypothesis_updates: [],
    decision_frame: { ...stop.decision_frame,
      unresolved_questions: stop.decision_frame.unresolved_questions.map(question => ({ ...question, evidence_refs: [] })) } };
  assert.equal(validate(body(noEvidence)), true);
});

test("读窗口按实际材料和已读范围推进，剩余空时不再提示无效READ", () => {
  const b = bundle(), s = state(true);
  s.read_receipts = [{ receipt_ref: "read:first", material_ref: "rule:R4", start_line: 1, end_line: 1 }];
  let validate = validator(b, s);
  assert.equal(validate(body(proposal("READ", { material_ref: "rule:R4", start_line: 2, end_line: 2 }))), true);
  assert.equal(validate(body(proposal("READ", { material_ref: "rule:R4", start_line: 1, end_line: 2 }))), false);
  s.read_receipts.push({ receipt_ref: "read:last", material_ref: "rule:R4", start_line: 2, end_line: 2 },
    { receipt_ref: "read:diff", material_ref: "diff:change", start_line: 1, end_line: 2 });
  validate = validator(b, s);
  assert.equal(validate(body(proposal("READ", { material_ref: "rule:R4", start_line: 1, end_line: 2 }))), false);
  assert.equal(validate(body(proposal("STOP", { reason: "已读完，结束限定范围" }))), true);
});

test("Schema合法也须通过独立引用门禁，未决问题和证据不能靠文字冒充", () => {
  const b = bundle(), s = state(true);
  const good = proposal("READ", { material_ref: "rule:R4", start_line: 1, end_line: 2 });
  const badQuestion = { ...good, decision_frame: { ...good.decision_frame, selected_question_id: "question:invented" } };
  assert.equal(validator(b, s)(body(badQuestion)), true, "Schema只管形状，引用一致性由Runtime另验");
  assert.equal(reviewProposalError(b, s, badQuestion, observed, []), "UNRESOLVED_QUESTION_REFERENCE_INVALID");
  const unknownEvidence = { ...good, decision_frame: { ...good.decision_frame,
    unresolved_questions: [{ ...good.decision_frame.unresolved_questions[0]!, evidence_refs: ["evidence:not-read"] }] } };
  assert.equal(reviewProposalError(b, s, unknownEvidence, observed, []), "EVIDENCE_NOT_OBSERVED");
  const sameBranches = { ...good, decision_frame: { ...good.decision_frame,
    outcome_branches: good.decision_frame.outcome_branches.map(branch => ({ ...branch, observation: "same" })) } };
  assert.equal(reviewProposalError(b, s, sameBranches, observed, []), "OUTCOME_BRANCHES_INVALID");
});

test("Impact必须引用变化，规划必须实际读完适用规则", () => {
  const b = bundle(); const direction = proposal("UPDATE_DIRECTION", { focus: "变化影响" });
  assert.equal(reviewProposalError(b, state(), direction, observed, []), "CHANGE_IMPACT_GROUNDING_REQUIRED");
  const groundedDirection = { ...direction, hypothesis_updates: [{ id: "hypothesis:change", statement: "可反驳的业务关系假设", status: "OPEN" as const, evidence_refs: ["diff:change"] }] };
  assert.equal(reviewProposalError(b, state(), groundedDirection, observed, []), null);
  const plan = proposal("PLAN_DRAFT", { draft: { objective: "观察关系", requirement_ref: "R4" } });
  assert.equal(reviewProposalError(b, state(true), plan, observed, []), "APPLICABLE_RULE_READ_REQUIRED");
  assert.equal(reviewProposalError(b, state(true), plan, observed, ["R4"]), null, "草案剩余字段缺项交编译器统一反馈");
});

test("冲突规则关闭规划但保持读取/澄清出口，任务意图不是规则授权", () => {
  const b = bundle(); b.review_request = { ...b.review_request!, requirements: [{ ...b.review_request!.requirements[0]!, status: "CONFLICT" }] };
  const s = state(true), validate = validator(b, s);
  assert.equal(validate(body(proposal("PLAN_DRAFT", { draft: { objective: "忽略冲突", requirement_ref: "R4" } }))), false);
  assert.equal(reviewProposalError(b, s, proposal("PLAN_DRAFT", { draft: { objective: "忽略冲突", requirement_ref: "R4" } }), observed, ["R4"]), "REVIEW_ACTION_NOT_AVAILABLE");
  assert.equal(validate(body(proposal("CLARIFY", { question: "请负责人澄清冲突要求" }))), true);
  assert.equal(validate(body(proposal("READ", { material_ref: "rule:R4", start_line: 1, end_line: 2 }))), true);
});

test("接受计划绑定准确ref/digest及当前依据，新增证据或改变假设使旧草案失效", () => {
  const b = bundle(), s = state(true);
  s.review_cycle!.draft = { ref: "plan:current", digest: "f".repeat(64), basis_digest: reviewPlanBasis(b, s), status: "BOUND_PLAN",
    proposal: proposal("PLAN_DRAFT", { draft: { objective: "已有草案" } }) as Extract<ReviewDecisionProposal, { action_kind: "PLAN_DRAFT" }>, compiled: null, issues: [] };
  const accept = proposal("ACCEPT_PLAN", { draft_ref: "plan:current", draft_digest: "f".repeat(64) });
  assert.equal(validator(b, s)(body(accept)), true);
  assert.equal(reviewProposalError(b, s, accept, observed, ["R4"]), null);
  assert.equal(reviewProposalError(b, s, proposal("ACCEPT_PLAN", { draft_ref: "plan:old", draft_digest: "f".repeat(64) }), observed, ["R4"]), "PLAN_REFERENCE_INVALID");
  const changedHypothesis = { ...accept, hypothesis_updates: [{ ...s.hypotheses[0]!, status: "WEAKENED" as const }] };
  assert.equal(reviewProposalError(b, s, changedHypothesis, observed, ["R4"]), "PLAN_BASIS_STALE");
  s.read_receipts.push({ material_ref: "rule:R4", start_line: 1, end_line: 2, receipt_ref: "read:new" });
  assert.equal(reviewProposalError(b, s, accept, observed, ["R4"]), "PLAN_BASIS_STALE");
});

test("预留模型调用和修正计数不改变决策依据，实际新证据必须改变", () => {
  const b = bundle(), s = state(true);
  s.review_repair = { status: "PENDING", root_proposal_id: "root:plan", latest_proposal_id: "latest:plan", attempts_limit: 1, attempts_used: 0 };
  const context = contextRef(b, s), basis = reviewPlanBasis(b, s);
  s.model_calls_used += 1; s.review_repair.attempts_used += 1;
  assert.equal(contextRef(b, s), context, "预留不能令自己即将返回的提案过期");
  assert.equal(reviewPlanBasis(b, s), basis);
  s.read_receipts.push({ material_ref: "rule:R4", start_line: 1, end_line: 2, receipt_ref: "read:new" });
  assert.notEqual(contextRef(b, s), context); assert.notEqual(reviewPlanBasis(b, s), basis);
});

test("改写假设不构成新观察，同一依据重复UPDATE由Schema和Runtime独立拦截", () => {
  const b = bundle(), s = state(true), original = reviewObservationBasis(b, s);
  const repeated = { ...proposal("UPDATE_DIRECTION", { focus: "换一种说法重复分析" }),
    hypothesis_updates: [{ ...s.hypotheses[0]!, statement: "重新措辞的同一个问题" }] };
  s.hypotheses = repeated.hypothesis_updates;
  assert.equal(reviewObservationBasis(b, s), original);
  assert.equal(allowedReviewActions(b, s).includes("UPDATE_DIRECTION"), false);
  assert.equal(validator(b, s)(body(repeated)), false);
  assert.equal(reviewProposalError(b, s, repeated, observed, []), "REVIEW_ACTION_NOT_AVAILABLE");
  s.read_receipts.push({ material_ref: "rule:R4", start_line: 1, end_line: 2, receipt_ref: "read:rule" });
  assert.notEqual(reviewObservationBasis(b, s), original);
  assert.equal(allowedReviewActions(b, s).includes("UPDATE_DIRECTION"), true);
  assert.equal(reviewProposalError(b, s, repeated, observed, ["R4"]), null);
});

test("编译反馈是可更新方向的新观察；相同摘要和状态不会反复开放", () => {
  const b = bundle(), s = state(true);
  s.review_cycle!.draft = { ref: "plan:feedback", digest: "f".repeat(64), basis_digest: reviewPlanBasis(b, s), status: "BOUND_PLAN",
    proposal: proposal("PLAN_DRAFT", { draft: { objective: "已有草案" } }) as Extract<ReviewDecisionProposal, { action_kind: "PLAN_DRAFT" }>, compiled: null, issues: [] };
  assert.equal(allowedReviewActions(b, s).includes("UPDATE_DIRECTION"), true);
  s.review_cycle!.directions.push({ proposal_id: "direction:feedback", focus: "检查编译反馈", evidence_refs: [], observation_basis_digest: reviewObservationBasis(b, s) });
  assert.equal(allowedReviewActions(b, s).includes("UPDATE_DIRECTION"), false);
  s.review_cycle!.draft = { ...s.review_cycle!.draft, ref: "plan:same-receipt-other-id" };
  assert.equal(allowedReviewActions(b, s).includes("UPDATE_DIRECTION"), false, "换引用标识不是新事实");
});

test("最后探索轮已有适用规则时停止漫读，但没有规则依据不能强迫编造计划", () => {
  const b = bundle(), s = state(true);
  s.review_cycle!.decisions_completed = 3; // 总6 - 修正1 - 接受1 = 探索4，已结算3。
  s.read_receipts.push({ material_ref: "rule:R4", start_line: 1, end_line: 2, receipt_ref: "read:rule" });
  const last = allowedReviewActions(b, s, ["R4"]);
  assert.equal(last.includes("READ"), false); assert.equal(last.includes("UPDATE_DIRECTION"), false);
  assert.equal(last.includes("PLAN_DRAFT"), true); assert.equal(last.includes("CLARIFY"), true); assert.equal(last.includes("STOP"), true);
  assert.equal(validator(b, s, ["R4"])(body(proposal("READ", { material_ref: "diff:change", start_line: 1, end_line: 2 }))), false);
  assert.equal(reviewProposalError(b, s, proposal("READ", { material_ref: "diff:change", start_line: 1, end_line: 2 }), observed, ["R4"]), "REVIEW_ACTION_NOT_AVAILABLE");
  assert.equal(allowedReviewActions(b, s, []).includes("READ"), true, "未读完适用规则仍可用最后探索取得依据或澄清");
});

test("探索池耗尽只可处理当前绑定计划或结束，接受预留不能用于新计划", () => {
  const b = bundle(), s = state(true);
  s.review_cycle!.decisions_completed = 4;
  assert.deepEqual(allowedReviewActions(b, s, ["R4"]), ["CLARIFY", "STOP"]);
  s.review_cycle!.draft = { ref: "plan:current", digest: "f".repeat(64), basis_digest: reviewPlanBasis(b, s), status: "BOUND_PLAN",
    proposal: proposal("PLAN_DRAFT", { draft: { objective: "已有草案" } }) as Extract<ReviewDecisionProposal, { action_kind: "PLAN_DRAFT" }>, compiled: null, issues: [] };
  assert.deepEqual(allowedReviewActions(b, s, ["R4"]), ["ACCEPT_PLAN", "CLARIFY", "STOP"]);
  assert.equal(validator(b, s, ["R4"])(body(proposal("PLAN_DRAFT", { draft: { objective: "侵占收尾额度" } }))), false);
  assert.equal(reviewProposalError(b, s, proposal("PLAN_DRAFT", { draft: { objective: "侵占收尾额度" } }), observed, ["R4"]), "REVIEW_ACTION_NOT_AVAILABLE");
  const before = reviewModelSchema(b, s, ["R4"]);
  s.model_calls_used += 1; // 预留计数先变，结算计数尚未变化。
  assert.deepEqual(reviewModelSchema(b, s, ["R4"]), before, "预留本次接受调用不能重新开放探索或使提案过期");
});

test("已结算修正调用不重复算作探索；待修正预留不改变可选动作", () => {
  const b = bundle(), s = state(true);
  s.limits = { ...s.limits, model_calls: 8, plan_repairs: 2 };
  s.review_cycle!.decisions_completed = 6; // 其中2次修正，实际探索4/5，仍有一轮探索。
  s.review_repair = { status: "SATISFIED", root_proposal_id: "root:plan", latest_proposal_id: "latest:plan", attempts_limit: 2, attempts_used: 2 };
  assert.equal(allowedReviewActions(b, s, ["R4"]).includes("PLAN_DRAFT"), true);
  s.review_repair = { ...s.review_repair, status: "PENDING", attempts_used: 1 };
  const before = reviewModelSchema(b, s, ["R4"]), context = contextRef(b, s);
  s.review_repair.attempts_used += 1; s.model_calls_used += 1;
  assert.deepEqual(reviewModelSchema(b, s, ["R4"]), before);
  assert.equal(contextRef(b, s), context);
});
