import type { DecisionProposal, ReviewDecisionProposal, ReviewRequest } from "@egap/contracts";
import { assessCheckQuality, type CheckQualityAssessment, type CheckQualityIssue } from "./check-quality.js";
import { assessCheckBinding, type CheckBindingAssessment } from "./check-binding.js";
import type { Json, ReleaseBundle } from "./types.js";

type DraftProposal = Extract<ReviewDecisionProposal, { action_kind: "PLAN_DRAFT" }>;
type CheckProposal = Extract<DecisionProposal, { action_kind: "CHECK_INTENT" }>;

export interface ReviewBasisGap {
  readonly code: string;
  /** 空数组表示影响整个请求；非空数组只限制对应要求，不阻断无关探索。 */
  readonly requirement_refs: readonly string[];
  readonly detail: string;
}
export interface ReviewBasisAssessment {
  readonly gaps: readonly ReviewBasisGap[];
  readonly eligible_requirement_refs: readonly string[];
  readonly local_planning_ready: boolean;
  readonly execution_authorized: false;
}
export interface PlanDraftCompilation {
  readonly status: "NEEDS_REVISION" | "BOUND_PLAN";
  /** 仅机械展开的传统检查提案，供现有质量/绑定规则审查；不是新获准操作。 */
  readonly proposal: CheckProposal | null;
  readonly issues: readonly CheckQualityIssue[];
  readonly quality?: CheckQualityAssessment;
  readonly binding?: CheckBindingAssessment;
  readonly execution_ready: false;
  readonly semantic_correctness: "NOT_ESTABLISHED";
  readonly dispatch_qualified: false;
}

const object = (value: unknown): value is Json => value !== null && typeof value === "object" && !Array.isArray(value);
const rows = (value: unknown): Json[] => Array.isArray(value) ? value.filter(object) : [];

/**
 * acceptRun/contextView/submit → assessReviewBasis：把来源缺项投影为局部规划限制。
 * 身份和摘要须先由 validateBundle 验证；这里不接受模型变更规则，不授予公司批准或执行权。
 * CONFIRMED_LOCAL 仅是本地操作员的校准声明，所有结果仍属于 LOCAL_SYNTHETIC_PLANNING。
 */
export function assessReviewBasis(bundle: ReleaseBundle): ReviewBasisAssessment {
  const request = (bundle as ReleaseBundle & { review_request?: ReviewRequest }).review_request;
  const gaps: ReviewBasisGap[] = [];
  const blocked = new Set<string>();
  let globalBlock = false;
  const add = (code: string, refs: readonly string[], detail: string): void => {
    gaps.push({ code, requirement_refs: [...refs], detail });
    if (!refs.length) globalBlock = true;
    for (const ref of refs) blocked.add(ref);
  };
  if (!request) {
    add("REVIEW_REQUEST_REQUIRED", [], "本轮没有正式审查请求，不能推定变更意图或本地确认的要求。");
  } else {
    if (!request.requirements.length) add("REVIEW_REQUIREMENTS_MISSING", [], "尚未提供新增或保留要求；可读材料或澄清，不能自行制定验收标准。");
    const catalog = rows(bundle.catalog["requirements"]);
    for (const requirement of request.requirements) {
      const refs = [requirement.id];
      if (requirement.authority !== "LOCAL_CALIBRATION")
        add("REQUIREMENT_AUTHORITY_UNVERIFIED", refs, "要求的来源尚未确认，不能将参考信息当作验收依据。");
      if (requirement.status !== "CONFIRMED_LOCAL")
        add(requirement.status === "CONFLICT" ? "REQUIREMENT_CONFLICT" : "REQUIREMENT_CONFIRMATION_PENDING", refs,
          requirement.status === "CONFLICT" ? "本项要求存在已登记冲突，需要负责人澄清。" : "本项要求尚未获得本地确认。");
      const registered = catalog.filter(row => row["id"] === requirement.id);
      if (!bundle.requirement_refs.includes(requirement.id) || registered.length !== 1 ||
          registered[0]!["material_ref"] !== requirement.material_ref)
        add("REQUIREMENT_CATALOG_BINDING_MISSING", refs, "要求必须唯一绑定冻结目录中的同一规则材料，不能通过请求改指其他依据。");
      const materials = bundle.materials.filter(row => row.material_ref === requirement.material_ref);
      if (materials.length !== 1 || materials[0]!.kind !== "REGISTERED_RULE_EXCERPT" || !materials[0]!.content.trim())
        add("REQUIREMENT_MATERIAL_MISSING", refs, "缺少本项要求对应的完整登记规则材料；标题、发布说明和候选源码不能代替规则。");
    }
    const requirementIds = new Set(request.requirements.map(row => row.id));
    for (const gap of request.known_gaps) {
      // scope 拼错不能等同于“不影响任何已知要求”；即便绕过请求解析也保守关闭规划。
      if (gap.requirement_refs.some(ref => !requirementIds.has(ref)))
        add("INPUT_GAP_SCOPE_UNKNOWN", [], "已登记缺项引用了本请求以外的要求，须先确认影响范围。");
      add("DECLARED_INPUT_GAP", gap.requirement_refs, gap.description);
    }
  }
  const eligible = globalBlock ? [] : request!.requirements.filter(row => !blocked.has(row.id)).map(row => row.id);
  return { gaps, eligible_requirement_refs: eligible, local_planning_ready: eligible.length > 0, execution_authorized: false };
}

/** 反馈必须指向模型真正提交的草案字段；原始编译回执仍在 quality/binding 中保存。 */
function draftPath(path: string): string {
  const prefix = "/payload/check_spec";
  const mapping: [string, string][] = [
    ["/control/inputs", "control_inputs"], ["/control/expected_relation", "control_relation"], ["/control/reason", "control_reason"],
    ["/observations/0/field", "observation_field"], ["/expectations/0/value", "expected_value"],
    ["/expectations/0/operator", "operator"], ["/expectations/0/requirement_ref", "requirement_ref"],
    ["/change_relation/reason", "change_reason"], ["/change_relation/input_refs", "inputs"],
    ["/change_relation/output_refs", "observation_field"], ["/inputs", "inputs"],
  ];
  if (path.startsWith(prefix + "/change_relation/")) {
    const suffix = path.slice((prefix + "/change_relation/").length);
    if (!["reason", "input_refs", "output_refs"].some(key => suffix === key || suffix.startsWith(key + "/")))
      return "/payload/draft/" + suffix;
  }
  for (const [from, to] of mapping) if (path === prefix + from || path.startsWith(prefix + from + "/"))
    return "/payload/draft/" + to + path.slice((prefix + from).length);
  return path.startsWith(prefix) ? "/payload/draft" : path;
}

/**
 * Runtime 的 PLAN_DRAFT → 本函数 → assessCheckQuality + assessCheckBinding。
 * 模型决定业务取值、因素对照、断言与 B/C 预测；编译器只填固定阶段/来源/观察身份。
 * 草案不完整时一次返回全部字段缺项；绝不由 diff、fixture 默认值或公开样本推导正确答案。
 * 本版明确采用因素对照：主/对照至少一个同名输入不同；B/C 是同一主输入的另一个比较维度。
 */
export function compilePlanDraft(bundle: ReleaseBundle, proposal: DraftProposal, authorityContract: Json): PlanDraftCompilation {
  const draft = proposal.payload.draft;
  const issues: CheckQualityIssue[] = [];
  const add = (code: string, field: string, message: string): void => { issues.push({ code, path: "/payload/draft/" + field, message }); };
  const finish = (compiled: CheckProposal | null = null, quality?: CheckQualityAssessment, binding?: CheckBindingAssessment): PlanDraftCompilation => ({
    status: issues.length ? "NEEDS_REVISION" : "BOUND_PLAN", proposal: compiled, issues,
    ...(quality ? { quality } : {}), ...(binding ? { binding } : {}),
    execution_ready: false, semantic_correctness: "NOT_ESTABLISHED", dispatch_qualified: false,
  });
  const required = ["requirement_ref", "diff_ref", "changed_fields", "claim", "inputs", "control_inputs", "observation_field",
    "operator", "expected_value", "expected_baseline", "expected_candidate", "control_relation", "control_reason", "change_reason"] as const;
  for (const field of required) if (draft[field] === undefined)
    add("PLAN_DRAFT_FIELD_REQUIRED", field, `补充 ${field}；该业务选择必须由提案明确给出。`);
  if (draft.requirement_ref !== undefined) {
    if (!assessReviewBasis(bundle).eligible_requirement_refs.includes(draft.requirement_ref))
      add("REQUIREMENT_NOT_PLANNING_ELIGIBLE", "requirement_ref", "该项要求仍有来源、材料或冲突缺项，先阅读或澄清再规划。");
    if (proposal.requirement_refs.length !== 1 || proposal.requirement_refs[0] !== draft.requirement_ref)
      add("DRAFT_REQUIREMENT_SCOPE_MISMATCH", "requirement_ref", "当前草案只形成一项要求的检查；提案 requirement_refs 必须准确对应，不能夹带未检查义务。");
  }
  const fixture = authorityContract["fixture"];
  if (!["id", "capability_ref", "scenario_ref", "stage_ref", "source_ref"].every(key =>
    typeof authorityContract[key] === "string" && (authorityContract[key] as string).length > 0) ||
    !object(fixture) || typeof fixture["id"] !== "string" || !fixture["id"].length)
    add("AUTHORITY_CONTRACT_INVALID", "binding", "受保护能力合同缺失，不能从模型草案补出执行接口。");
  if (issues.length) return finish();

  // 缺项上面统一检查后才展开；仅机械标识由 authority 提供，其余字段原样拷贝。
  const check: CheckProposal = {
    schema_version: 1, proposal_id: proposal.proposal_id, based_on_decision_revision: proposal.based_on_decision_revision,
    candidate_epoch: proposal.candidate_epoch, context_view_ref: proposal.context_view_ref, action_kind: "CHECK_INTENT",
    requirement_refs: [...proposal.requirement_refs], hypothesis_refs: [...proposal.hypothesis_refs], evidence_refs: [...proposal.evidence_refs],
    expected_information: proposal.expected_information, rationale: proposal.rationale, hypothesis_updates: structuredClone(proposal.hypothesis_updates),
    payload: {
      capability_ref: String(authorityContract["capability_ref"]), scenario_ref: String(authorityContract["scenario_ref"]),
      objective: draft.objective, observation_requirements: ["读取 " + draft.observation_field!],
      check_spec: { schema_version: 1,
        binding: { contract_ref: String(authorityContract["id"]), fixture_ref: String((fixture as Json)["id"]) },
        stage_ref: String(authorityContract["stage_ref"]), inputs: structuredClone(draft.inputs!),
        observations: [{ id: "primary", source_ref: String(authorityContract["source_ref"]), field: draft.observation_field! }],
        expectations: [{ observation_ref: "primary", operator: draft.operator!, value: draft.expected_value!, requirement_ref: draft.requirement_ref! }],
        control: { scenario_ref: String(authorityContract["scenario_ref"]), stage_ref: String(authorityContract["stage_ref"]),
          inputs: structuredClone(draft.control_inputs!), observation_ref: "primary", expected_relation: draft.control_relation!, reason: draft.control_reason! },
        change_relation: { diff_ref: draft.diff_ref!, changed_fields: [...draft.changed_fields!], input_refs: draft.inputs!.map(input => input.name),
          output_refs: ["primary"], requirement_ref: draft.requirement_ref!, claim: draft.claim!, expected_baseline: draft.expected_baseline!,
          expected_candidate: draft.expected_candidate!, reason: draft.change_reason! },
      },
    },
  };
  const quality = assessCheckQuality(bundle, check);
  const binding = assessCheckBinding(bundle, check, authorityContract);
  for (const issue of [...quality.issues, ...binding.issues]) issues.push({ ...issue, path: draftPath(issue.path) });
  return finish(check, quality, binding);
}
