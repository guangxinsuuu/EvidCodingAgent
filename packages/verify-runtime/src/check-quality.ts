import type { DecisionProposal } from "@egap/contracts";
import type { Json, ReleaseBundle } from "./types.js";

type CheckProposal = Extract<DecisionProposal, { action_kind: "CHECK_INTENT" }>;
type Input = { readonly name: string; readonly value: string };
export interface CheckQualityIssue { readonly code: string; readonly path: string; readonly message: string }
export interface CheckQualityAssessment {
  readonly policy_version: "check-specificity-v1";
  readonly status: "NEEDS_REVISION" | "STRUCTURALLY_COMPLETE";
  readonly issues: readonly CheckQualityIssue[];
  readonly semantic_correctness: "NOT_ESTABLISHED";
  readonly dispatch_qualified: false;
}

// 这些类别提供运行中产品/业务事实。代码、规则、历史报告以及实验环境本身不是业务观察。
// PRODUCT_EXECUTION 等来源仍可能是自报信息；允许规划引用不等于已取得独立可信证据。
const OBSERVABLE_SOURCE_KINDS = new Set([
  "BUSINESS_FACT", "IDENTITY_AUTHORITY", "PRODUCT_EXECUTION", "MODEL_EXECUTION",
]);
// UNKNOWN/null 可以是真实输出枚举/空值；由能力字段类型判断，不能一概当作占位词。
const PLACEHOLDER = /^(?:todo|tbd|fixme|undefined|待定|待补充|待填写|待确认|占位|\.\.\.|…|\?+)$/i;
const NUMERIC_LITERAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
const ROOT = "/payload/check_spec";

function rows(value: unknown): Json[] {
  return Array.isArray(value) ? value.filter((item): item is Json =>
    item !== null && typeof item === "object" && !Array.isArray(item)) : [];
}

/**
 * Runtime.submit → assessCheckQuality：仅处理已经通过 parseDecisionProposal 的 CHECK。
 * 该纯函数用冻结发布包的目录核对结构、引用及可区分对照，一次返回全部可发现缺项。
 * 不调用模型、不读取/改动文件、不执行 SUT，也不以文案长度或关键词数量推断语义正确。
 * Runtime 负责持久化结果、给予有限修正机会及记账；本函数不能增加预算或授予派发资格。
 */
export function assessCheckQuality(bundle: ReleaseBundle, proposal: CheckProposal): CheckQualityAssessment {
  const issues: CheckQualityIssue[] = [];
  const add = (code: string, path: string, message: string): void => { issues.push({ code, path, message }); };
  const spec = proposal.payload.check_spec;
  const scenarios = rows(bundle.catalog["scenarios"]);
  const sources = rows(bundle.catalog["sources"]);
  const registeredRequirements = new Set(bundle.requirement_refs);
  const ruleIds = new Set([...registeredRequirements,
    ...rows(bundle.catalog["requirements"]).map(row => String(row["id"] ?? ""))]);
  // 只识别显然没有内容的标记。其他自由文本即使通过，也仍需后续语义审核和真实执行。
  const concrete = (value: string): boolean => value.trim().length > 0 && !PLACEHOLDER.test(value.trim());
  const concreteFact = (value: string): boolean => concrete(value) && !ruleIds.has(value.trim());

  // 兼容字段仍是提案的一部分；不能靠另添完整 spec 掩盖旧字段中的裸规则编号。
  for (const [index, requirement] of proposal.payload.observation_requirements.entries()) {
    if (!concreteFact(requirement)) add("OBSERVATION_DETAILS_REQUIRED", `/payload/observation_requirements/${index}`,
      "observation_requirements 要说明需要取得的事实，不能只填规则编号或占位词。");
  }

  function checkStage(scenarioRef: string, stageRef: string | undefined, path: string): void {
    const scenario = scenarios.find(row => row["id"] === scenarioRef);
    if (!scenario) add("UNKNOWN_SCENARIO", path === `${ROOT}/stage_ref` ? "/payload/scenario_ref" : path.replace(/stage_ref$/, "scenario_ref"),
      "选择目录中已登记的场景；未登记场景不能提供可恢复阶段身份。");
    if (stageRef === undefined || !concrete(stageRef)) {
      add("STAGE_REQUIRED", path, "指定本次观察发生的已登记 stage_ref，不能只描述整个场景。");
    } else if (scenario && !rows(scenario["stages"]).some(stage => stage["stage_ref"] === stageRef)) {
      add("STAGE_NOT_IN_SCENARIO", path, "stage_ref 必须属于所选 scenario_ref；请从该场景登记阶段中选择。");
    }
  }

  function checkInputs(inputs: readonly Input[] | undefined, path: string): Map<string, string> {
    const values = new Map<string, string>();
    const names = new Set<string>();
    if (!inputs?.length) add("INPUTS_REQUIRED", path, "列出具体输入参数和取值；只写实验目标不能形成可比较的实验。");
    for (const [index, input] of (inputs ?? []).entries()) {
      const name = input.name.trim();
      if (!concrete(input.name)) add("INPUT_NAME_REQUIRED", `${path}/${index}/name`, "输入参数名不能为空或使用占位词。");
      if (!concreteFact(input.value)) add("INPUT_VALUE_REQUIRED", `${path}/${index}/value`, "输入取值必须具体，不能是空值、占位词或裸规则编号。");
      if (names.has(name)) add("DUPLICATE_INPUT_NAME", `${path}/${index}/name`, "同一组实验输入的参数名必须唯一。");
      else if (concrete(name) && concreteFact(input.value)) values.set(name, input.value.trim());
      names.add(name);
    }
    return values;
  }

  if (!spec) add("CHECK_SPEC_REQUIRED", ROOT, "补充 schema_version=1 的 check_spec；旧 observation_requirements 文字不能代替实验规格。");
  checkStage(proposal.payload.scenario_ref, spec?.stage_ref, `${ROOT}/stage_ref`);
  const primaryInputs = checkInputs(spec?.inputs, `${ROOT}/inputs`);
  const observations = spec?.observations ?? [];
  const observationIds = new Set<string>();
  if (!observations.length) add("OBSERVATIONS_REQUIRED", `${ROOT}/observations`, "至少声明一个观察：唯一 id、登记事实来源 source_ref 和具体 field。");
  for (const [index, observation] of observations.entries()) {
    const path = `${ROOT}/observations/${index}`;
    if (!concrete(observation.id)) add("OBSERVATION_ID_REQUIRED", `${path}/id`, "观察 id 不能为空或使用占位词。");
    if (observationIds.has(observation.id)) add("DUPLICATE_OBSERVATION_ID", `${path}/id`, "观察 id 必须唯一，保证期望和对照指向确定观察。");
    observationIds.add(observation.id);
    const source = sources.find(row => row["id"] === observation.source_ref);
    if (!source) add("UNKNOWN_OBSERVATION_SOURCE", `${path}/source_ref`, "观察必须引用目录 sources 中已登记的来源。");
    else if (!OBSERVABLE_SOURCE_KINDS.has(String(source["kind"]))) add("SOURCE_NOT_OBSERVABLE", `${path}/source_ref`,
      "选择业务事实、身份决策、产品或模型执行来源；规则、参考文档、代码和历史结果不能代替当前观察。");
    if (!concreteFact(observation.field)) add("OBSERVATION_FIELD_REQUIRED", `${path}/field`, "指定实际观察字段或字段路径，不能以规则编号、空值或占位词替代。");
  }

  const expectations = spec?.expectations ?? [];
  if (!expectations.length) add("EXPECTATIONS_REQUIRED", `${ROOT}/expectations`, "写出观察与预期值的关系 operator/value，并绑定适用 requirement_ref。");
  for (const [index, expectation] of expectations.entries()) {
    const path = `${ROOT}/expectations/${index}`;
    if (!observationIds.has(expectation.observation_ref)) add("UNKNOWN_EXPECTATION_OBSERVATION", `${path}/observation_ref`, "期望必须引用本 check_spec 已声明的观察 id。");
    if (!registeredRequirements.has(expectation.requirement_ref) || !proposal.requirement_refs.includes(expectation.requirement_ref))
      add("EXPECTATION_REQUIREMENT_NOT_BOUND", `${path}/requirement_ref`, "期望必须引用发布包登记且列入本提案 requirement_refs 的要求。");
    if (["PRESENT", "ABSENT"].includes(expectation.operator)) {
      if (expectation.value !== "") add("PRESENCE_VALUE_MUST_BE_EMPTY", `${path}/value`, "PRESENT / ABSENT 只检查存在性，value 必须使用空字符串。");
    } else if (!concreteFact(expectation.value)) {
      add("EXPECTED_VALUE_REQUIRED", `${path}/value`, "给出具体预期值；裸规则编号或占位词不是可核对的业务结果。");
    } else if (["GREATER_THAN", "LESS_THAN"].includes(expectation.operator) &&
      (!NUMERIC_LITERAL.test(expectation.value.trim()) || !Number.isFinite(Number(expectation.value)))) {
      add("NUMERIC_EXPECTED_VALUE_REQUIRED", `${path}/value`, "数值关系的 value 必须是有限数字字符串，例如 10 或 0.5。");
    }
  }

  const control = spec?.control;
  if (!control) add("CONTROL_REQUIRED", `${ROOT}/control`, "补充对照的场景、阶段、输入、观察引用及 SAME / DIFFERENT 预期关系与原因。");
  else {
    checkStage(control.scenario_ref, control.stage_ref, `${ROOT}/control/stage_ref`);
    const controlInputs = checkInputs(control.inputs, `${ROOT}/control/inputs`);
    // 场景名不同或额外添一个参数并不足以形成可区分对照；至少有同名因素取值不同。
    // 这仍不证明其他因素已控制，更不证明因果关系，后续执行层须审核真实环境与观察。
    if (![...primaryInputs].some(([name, value]) => controlInputs.has(name) && controlInputs.get(name) !== value))
      add("CONTROL_NOT_DISTINGUISHABLE", `${ROOT}/control/inputs`, "主实验与对照至少需要一个同名输入取不同值；只换场景名或重复相同输入不够。");
    if (!observationIds.has(control.observation_ref)) add("UNKNOWN_CONTROL_OBSERVATION", `${ROOT}/control/observation_ref`, "对照关系必须引用本 check_spec 已声明的观察 id。");
    if (!concreteFact(control.reason)) add("CONTROL_REASON_REQUIRED", `${ROOT}/control/reason`, "解释对照为何预期 SAME 或 DIFFERENT，不能使用空值、占位词或裸规则编号。");
  }

  return {
    policy_version: "check-specificity-v1", status: issues.length ? "NEEDS_REVISION" : "STRUCTURALLY_COMPLETE",
    issues, semantic_correctness: "NOT_ESTABLISHED", dispatch_qualified: false,
  };
}
