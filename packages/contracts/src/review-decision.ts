import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";
import { DecisionProposalError, type CheckExpectation, type CheckInput, type HypothesisUpdate } from "./verify.js";

export type ReviewAction = "READ" | "CLARIFY" | "NOVEL_CHECK" | "STOP" | "UPDATE_DIRECTION" | "PLAN_DRAFT" | "ACCEPT_PLAN";
/** 简短、可核验的决策工作产物；不是要求模型披露隐藏推理过程。 */
export interface DecisionFrame {
  readonly unresolved_questions: readonly {
    readonly id: string; readonly question: string; readonly why_unresolved: string; readonly evidence_refs: readonly string[];
  }[];
  readonly selected_question_id: string;
  readonly outcome_branches: readonly { readonly observation: string; readonly implication: string; readonly next_action: ReviewAction }[];
}
/**
 * Planner 提交业务选择，可信编译器填入固定能力/阶段/观察身份。
 * 部分草案合法，缺项由 Runtime 一次反馈；编译器不得替模型猜输入、断言和对照。
 */
export interface PlanDraft {
  readonly objective: string;
  readonly requirement_ref?: string;
  readonly diff_ref?: string;
  readonly changed_fields?: readonly string[];
  readonly claim?: "MAY_CHANGE" | "SHOULD_PRESERVE";
  readonly inputs?: readonly CheckInput[];
  readonly control_inputs?: readonly CheckInput[];
  readonly observation_field?: string;
  readonly operator?: CheckExpectation["operator"];
  readonly expected_value?: string;
  readonly expected_baseline?: string;
  readonly expected_candidate?: string;
  readonly control_relation?: "SAME" | "DIFFERENT";
  readonly control_reason?: string;
  readonly change_reason?: string;
}
interface ReviewDecisionBasis {
  readonly schema_version: 3;
  readonly proposal_id: string;
  readonly based_on_decision_revision: number;
  readonly candidate_epoch: number;
  readonly context_view_ref: string;
  readonly requirement_refs: readonly string[];
  readonly hypothesis_refs: readonly string[];
  readonly evidence_refs: readonly string[];
  readonly expected_information: string;
  readonly rationale: string;
  readonly hypothesis_updates: readonly HypothesisUpdate[];
  readonly decision_frame: DecisionFrame;
}
export type ReviewDecisionProposal = ReviewDecisionBasis & (
  | { readonly action_kind: "READ"; readonly payload: { readonly material_ref: string; readonly start_line: number; readonly end_line: number } }
  | { readonly action_kind: "CLARIFY"; readonly payload: { readonly question: string } }
  | { readonly action_kind: "NOVEL_CHECK"; readonly payload: { readonly objective: string; readonly capability_gap: string } }
  | { readonly action_kind: "STOP"; readonly payload: { readonly reason: string } }
  | { readonly action_kind: "UPDATE_DIRECTION"; readonly payload: { readonly focus: string } }
  | { readonly action_kind: "PLAN_DRAFT"; readonly payload: { readonly draft: PlanDraft } }
  | { readonly action_kind: "ACCEPT_PLAN"; readonly payload: { readonly draft_ref: string; readonly draft_digest: string } }
);
function freezeTree<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) freezeTree(child);
    Object.freeze(value);
  }
  return value;
}
export const reviewDecisionProposalSchema = freezeTree(JSON.parse(readFileSync(new URL(
  "../../../protocols/verify-agent/v3/decision-proposal.schema.json", import.meta.url,
), "utf8")) as Record<string, unknown>);
const ajv = new Ajv2020({ strict: true, allErrors: true, coerceTypes: false, useDefaults: false, removeAdditional: false });
const validate = ajv.compile<ReviewDecisionProposal>(reviewDecisionProposalSchema);

/** Graph/模型 → 共享协议 → Runtime 准入；引用、预算与草案接受资格仍由 Runtime 判定。 */
export function parseReviewDecisionProposal(value: unknown): ReviewDecisionProposal {
  if (!validate(value)) throw new DecisionProposalError((validate.errors ?? []).slice(0, 8)
    .map(error => ({ keyword: error.keyword, path: error.instancePath })));
  return freezeTree(structuredClone(value));
}
