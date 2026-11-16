import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";

/** 模型对假设的状态更新仍是待审提案；SUPPORTED 不是业务结果 PASS。 */
export interface HypothesisUpdate {
  readonly id: string;
  readonly statement: string;
  readonly status: "OPEN" | "SUPPORTED" | "WEAKENED";
  readonly evidence_refs: readonly string[];
}

/** CHECK 的声明式输入；只是待审实验参数，不是可直接执行的命令或环境配置。 */
export interface CheckInput { readonly name: string; readonly value: string }
export interface CheckObservation {
  readonly id: string; readonly source_ref: string; readonly field: string;
}
export interface CheckExpectation {
  readonly observation_ref: string;
  readonly operator: "EQUALS" | "NOT_EQUALS" | "CONTAINS" | "PRESENT" | "ABSENT" |
    "UNCHANGED" | "GREATER_THAN" | "LESS_THAN";
  /** PRESENT / ABSENT 使用空串；其余操作使用明确值，数值关系使用有限数字字符串。 */
  readonly value: string;
  readonly requirement_ref: string;
}
export interface CheckControl {
  readonly scenario_ref: string; readonly stage_ref: string;
  readonly inputs: readonly CheckInput[];
  readonly observation_ref: string;
  readonly expected_relation: "SAME" | "DIFFERENT";
  readonly reason: string;
}
/**
 * 外层解析器只保证结构；Runtime 的 assessCheckQuality 再检查完整性和登记引用。
 * 除版本外的顶层字段可缺省，使模型一次收到全部缺项，而不是只收到首个 Schema 错误。
 * 结构完整仍不证明业务断言正确，也不会授予实验执行或发布权限。
 */
export interface CheckSpec {
  readonly schema_version: 1;
  readonly stage_ref?: string;
  readonly inputs?: readonly CheckInput[];
  readonly observations?: readonly CheckObservation[];
  readonly expectations?: readonly CheckExpectation[];
  readonly control?: CheckControl;
}

interface DecisionBasis {
  readonly schema_version: 1;
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
}

/**
 * Python 图 → RuntimePort → 本解析器 → TS 准入。只解析 outer action envelope，
 * 不扩大冻结的内部 PlannerOutput 枚举；CHECK_INTENT 是尚未获执行许可的检查建议。
 * 判别联合使调用者在检查 action_kind 后，只能读取相应 payload 字段。
 */
export type DecisionProposal = DecisionBasis & (
  | { readonly action_kind: "READ"; readonly payload: {
      readonly material_ref: string; readonly start_line: number; readonly end_line: number;
    } }
  | { readonly action_kind: "CHECK_INTENT"; readonly payload: {
      readonly capability_ref: string; readonly scenario_ref: string; readonly objective: string;
      readonly observation_requirements: readonly string[];
      readonly check_spec?: CheckSpec;
    } }
  | { readonly action_kind: "NOVEL_CHECK"; readonly payload: {
      readonly objective: string; readonly capability_gap: string;
    } }
  | { readonly action_kind: "CLARIFY"; readonly payload: { readonly question: string } }
  | { readonly action_kind: "FINDING"; readonly payload: {
      readonly summary: string; readonly limitations: readonly string[];
    } }
  | { readonly action_kind: "REQUEST_FINAL"; readonly payload: Readonly<Record<string, never>> }
  | { readonly action_kind: "STOP"; readonly payload: { readonly reason: string } }
);

/** 不携带原始模型文本或 Ajv 详情，避免错误响应外泄上下文。 */
export class DecisionProposalError extends Error {
  readonly error_code = "INVALID_DECISION_PROPOSAL";
  constructor(readonly issues: readonly { keyword: string; path: string }[] = []) {
    super("验证动作提案不符合当前协议。");
    this.name = "DecisionProposalError";
  }
}

function freezeTree<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) freezeTree(child);
    Object.freeze(value);
  }
  return value;
}

// src 与 dist 位于相同目录层级，均读同一个受版本控制的 Schema。
// 传输协议不由 TS/Python 各复制一份，避免发布后语言间漂移。
export const decisionProposalSchema = freezeTree(JSON.parse(readFileSync(new URL(
  "../../../protocols/verify-agent/v1/decision-proposal.schema.json", import.meta.url,
), "utf8")) as Record<string, unknown>);

const ajv = new Ajv2020({
  strict: true,
  allErrors: true,
  coerceTypes: false,
  useDefaults: false,
  removeAdditional: false,
});
const validate = ajv.compile<DecisionProposal>(decisionProposalSchema);

/**
 * 只接受严格协议，返回递归冻结的独立副本；不隐式纠正模型的枚举、类型或字段。
 * 此函数不确认 material/evidence/capability 是否存在，也不比较决策 revision。
 * 范围、引用、租约、预算和 READ 行区间的语义校验必须由权威 Runtime 完成。
 */
export function parseDecisionProposal(value: unknown): DecisionProposal {
  if (!validate(value)) throw new DecisionProposalError((validate.errors ?? []).slice(0, 8)
    .map(error => ({ keyword: error.keyword, path: error.instancePath })));
  return freezeTree(structuredClone(value));
}
