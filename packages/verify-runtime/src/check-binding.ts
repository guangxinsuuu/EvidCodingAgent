import type { DecisionProposal } from "@egap/contracts";
import { digest, sha, type Json, type ReleaseBundle } from "./types.js";

type CheckProposal = Extract<DecisionProposal, { action_kind: "CHECK_INTENT" }>;
type Scalar = string | number | boolean | null;
type Value = Scalar | string[];
interface Field { name?: string; field?: string; type: string; enum?: Scalar[]; nullable?: boolean;
  format?: string; minimum?: number; maximum?: number; required?: boolean; operators?: string[] }
interface Contract {
  id: string; schema_version: number; capability_ref: string; parent_capability_ref: string;
  scenario_ref: string; stage_ref: string; source_ref: string; supported_requirements?: string[];
  fixture: { id: string; sha256: string; context: Json };
  inputs: Field[]; outputs: Field[]; entrypoint: { path: string; symbol: string };
  source_sha256: string; stage_prerequisites: Json[];
}
interface Relation {
  diff_ref: string; changed_fields: string[]; input_refs: string[]; output_refs: string[];
  requirement_ref: string; claim: "MAY_CHANGE" | "SHOULD_PRESERVE";
  expected_baseline: string; expected_candidate: string; reason: string;
}
interface BindingSpec {
  binding?: { contract_ref: string; fixture_ref: string };
  change_relation?: Relation;
}
export interface CheckBindingAssessment {
  policy_version: "capability-binding-v1";
  status: "BOUND_PLAN" | "NEEDS_REVISION";
  issues: { code: string; path: string; message: string }[];
  compiled_plan?: Json;
  execution_ready: false;
  semantic_correctness: "NOT_ESTABLISHED";
  dispatch_qualified: false;
}

const ROOT = "/payload/check_spec";
const OWN = (object: object, key: string): boolean => Object.prototype.hasOwnProperty.call(object, key);
const FORBIDDEN = new Set(["__proto__", "prototype", "constructor"]);
const object = (value: unknown): value is Json => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;

/** 与可信 fixture 的 Python canonical-json-sort-keys-utf8-v1 一致；不依赖地区排序。 */
function stable(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(stable).join(",") + "]";
  if (!object(value)) return JSON.stringify(value);
  const compare = (a: string, b: string): number => {
    const left = Array.from(a, c => c.codePointAt(0)!); const right = Array.from(b, c => c.codePointAt(0)!);
    for (let i = 0; i < Math.min(left.length, right.length); i++) if (left[i] !== right[i]) return left[i]! - right[i]!;
    return left.length - right.length;
  };
  return "{" + Object.keys(value).sort(compare).map(key => JSON.stringify(key) + ":" + stable(value[key])).join(",") + "}";
}

/** 日期必须带时区且日历有效；Date.parse 会自动修正 2 月 30 日，因此先核对年月日。 */
function dateTime(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return false;
  const [, year, month, day, hour, minute, second, offset] = match;
  const y = Number(year), m = Number(month), d = Number(day);
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return y >= 1 && m >= 1 && m <= 12 && d >= 1 && d <= days[m - 1]! && Number(hour) < 24 && Number(minute) < 60 &&
    Number(second) < 60 && (offset === "Z" || (Number(offset!.slice(1, 3)) < 24 && Number(offset!.slice(4)) < 60)) &&
    Number.isFinite(Date.parse(value));
}

/** 仅遍历 fixture 的自有路径；允许已存在的数组下标，拒绝原型链和新增字段。 */
function target(context: Json, path: string): { parent: Json | unknown[]; key: string } | null {
  const parts = path.split(".");
  if (!parts.length || parts.some(part => !/^[A-Za-z_][A-Za-z_0-9]*$|^(?:0|[1-9]\d*)$/.test(part) || FORBIDDEN.has(part))) return null;
  let cursor: unknown = context;
  for (const [index, part] of parts.entries()) {
    if ((!object(cursor) && !Array.isArray(cursor)) || !OWN(cursor, part)) return null;
    if (Array.isArray(cursor) && !/^(?:0|[1-9]\d*)$/.test(part)) return null;
    if (index === parts.length - 1) return { parent: cursor, key: part };
    cursor = (cursor as Json)[part];
  }
  return null;
}

function typed(value: unknown, field: Field): value is Value {
  if (value === null) return field.nullable === true;
  if (field.type === "string") {
    if (typeof value !== "string" || (field.format === "date-time" && !dateTime(value))) return false;
  } else if (field.type === "integer") {
    if (typeof value !== "number" || !Number.isSafeInteger(value)) return false;
  } else if (field.type === "number") {
    if (typeof value !== "number" || !Number.isFinite(value)) return false;
  } else if (field.type === "boolean") {
    if (typeof value !== "boolean") return false;
  } else if (field.type === "string[]") {
    if (!Array.isArray(value) || !value.every(item => typeof item === "string")) return false;
  } else return false;
  if (typeof value === "number" && ((field.minimum !== undefined && value < field.minimum) ||
    (field.maximum !== undefined && value > field.maximum))) return false;
  return !field.enum || field.enum.some(item => stable(item) === stable(value));
}

/** 模型传输使用字符串；只允许显式、无歧义的标量/JSON 数组，不使用 JS 隐式 coercion。 */
function parse(value: string, field: Field): { ok: true; value: Value } | { ok: false } {
  let result: unknown;
  if (field.nullable && value === "null") result = null;
  else if (field.type === "string") result = value;
  else {
    if (field.type === "integer" && !/^-?(?:0|[1-9]\d*)$/.test(value)) return { ok: false };
    if (field.type === "boolean" && !["true", "false"].includes(value)) return { ok: false };
    try { result = JSON.parse(value); } catch { return { ok: false }; }
  }
  return typed(result, field) ? { ok: true, value: result } : { ok: false };
}

/**
 * Runtime.submit → assessCheckQuality + assessCheckBinding → 仅保存待执行计划。
 * authorityContract 由受信启动配置加载，不能来自模型的 payload 或 bundle 文案。
 * 编译器将模型的字段引用/字符串值绑定到真实函数的受限输入输出，默认值只取可信 fixture。
 * 本函数不运行 SUT、不确认阶段前置条件、不计算业务裁决；BOUND_PLAN 仍不具备执行许可。
 */
export function assessCheckBinding(bundle: ReleaseBundle, proposal: CheckProposal, authorityContract: unknown): CheckBindingAssessment {
  const issues: CheckBindingAssessment["issues"] = [];
  const add = (code: string, path: string, message: string): void => { issues.push({ code, path, message }); };
  const finish = (compiled_plan?: Json): CheckBindingAssessment => ({ policy_version: "capability-binding-v1",
    status: issues.length ? "NEEDS_REVISION" : "BOUND_PLAN", issues,
    ...(issues.length || !compiled_plan ? {} : { compiled_plan }), execution_ready: false,
    semantic_correctness: "NOT_ESTABLISHED", dispatch_qualified: false });
  if (proposal.payload.capability_ref !== "probe.commercial_decision") {
    add("CAPABILITY_NOT_BOUND", "/payload/capability_ref", "本期仅 probe.commercial_decision 绑定了真实函数；其他能力不能编译或派发。");
    return finish();
  }
  const contract = authorityContract as Contract;
  if (!object(authorityContract) || contract.schema_version !== 1 || !text(contract.id) ||
    contract.capability_ref !== "probe.commercial_decision" || contract.parent_capability_ref !== "unit.sut_decision" ||
    !object(contract.fixture) || !object(contract.fixture.context) || !text(contract.fixture.id) ||
    !/^[0-9a-f]{64}$/.test(contract.fixture.sha256) || !Array.isArray(contract.inputs) || !Array.isArray(contract.outputs) ||
    contract.inputs.some(field => !object(field)) || contract.outputs.some(field => !object(field)) ||
    !text(contract.scenario_ref) || !text(contract.stage_ref) || !text(contract.source_ref) ||
    !object(contract.entrypoint) || contract.entrypoint.symbol !== "commercial_decision" ||
    !text(contract.entrypoint.path) || contract.entrypoint.path.split("/").some(part => ["..", ""].includes(part)) ||
    !/^[0-9a-f]{64}$/.test(contract.source_sha256) || !Array.isArray(contract.stage_prerequisites) ||
    contract.stage_prerequisites.some(row => !object(row) || !text(row["id"]) || row["status"] !== "REQUIRED_AT_EXECUTION")) {
    add("AUTHORITY_CONTRACT_INVALID", ROOT, "受信能力合同不可用或结构不完整；模型无权补写执行合同。"); return finish();
  }
  if (sha(stable(contract.fixture.context)) !== contract.fixture.sha256) {
    add("FIXTURE_DIGEST_MISMATCH", `${ROOT}/binding/fixture_ref`, "受信 fixture 内容摘要不匹配，不能用漂移输入编译实验。"); return finish();
  }
  if (!bundle.planning_contracts || bundle.planning_contracts.length !== 1 ||
    stable(bundle.planning_contracts[0]) !== stable(authorityContract)) add("BINDING_CONTRACT_NOT_IN_BUNDLE", `${ROOT}/binding/contract_ref`,
    "任务发布包必须冻结当前受信合同；旧包或自造合同不能被模型引用升级为绑定计划。");
  // 函数名相同不足以证明绑定合同仍适用。基线必须是合同审查的源码，候选必须
  // 有冻结材料；这里检查来源身份，不执行候选，也不假定它保留了旧函数行为。
  const baselineSource = bundle.materials.find(item => item.kind === "BASELINE_SOURCE" &&
    item.path === contract.entrypoint.path && item.git_commit === bundle.baseline_sha);
  const candidateSource = bundle.materials.find(item => item.kind === "CANDIDATE_SOURCE" &&
    item.path === contract.entrypoint.path && item.git_commit === bundle.candidate_sha);
  if (!baselineSource || !candidateSource) add("TARGET_SOURCE_MISSING", ROOT,
    "发布包必须包含能力入口的准确基线与候选源码材料，不能仅凭 capability 名称假定目标存在。");
  if (baselineSource && (baselineSource.sha256 !== contract.source_sha256 || sha(baselineSource.content) !== contract.source_sha256))
    add("BASELINE_CONTRACT_SOURCE_MISMATCH", ROOT, "基线入口源码与能力合同审查的 source_sha256 不一致，必须重新审查绑定。");
  if (candidateSource && sha(candidateSource.content) !== candidateSource.sha256)
    add("CANDIDATE_SOURCE_DIGEST_MISMATCH", ROOT, "候选源码内容与冻结摘要不一致。");
  const inputs = new Map(contract.inputs.map(field => [field.name, field]));
  const outputs = new Map(contract.outputs.map(field => [field.field, field]));
  if (inputs.size !== contract.inputs.length || outputs.size !== contract.outputs.length ||
    contract.inputs.some(field => !text(field.name) || !["string", "integer", "boolean"].includes(field.type) || !target(contract.fixture.context, field.name)) ||
    contract.outputs.some(field => !text(field.field) || !["string", "boolean", "number", "string[]"].includes(field.type) || !Array.isArray(field.operators))) {
    add("AUTHORITY_FIELDS_INVALID", ROOT, "能力合同字段重复、类型未支持或输入路径不在可信 fixture 内。"); return finish();
  }
  const spec = proposal.payload.check_spec;
  const extension = spec as (typeof spec & BindingSpec);
  if (!extension?.binding) add("BINDING_REQUIRED", `${ROOT}/binding`, "声明能力 contract_ref 与可信 fixture_ref。");
  else {
    if (extension.binding.contract_ref !== contract.id) add("CONTRACT_REF_MISMATCH", `${ROOT}/binding/contract_ref`, "必须精确引用当前受信能力合同。");
    if (extension.binding.fixture_ref !== contract.fixture.id) add("FIXTURE_REF_MISMATCH", `${ROOT}/binding/fixture_ref`, "必须精确引用合同的可信 fixture，不接受模型自造默认输入。");
  }
  if (proposal.payload.scenario_ref !== contract.scenario_ref || spec?.stage_ref !== contract.stage_ref)
    add("PRIMARY_STAGE_NOT_BOUND", `${ROOT}/stage_ref`, "主实验必须使用能力合同绑定的场景与阶段。");
  if (spec?.control?.scenario_ref !== contract.scenario_ref || spec?.control?.stage_ref !== contract.stage_ref)
    add("CONTROL_STAGE_NOT_BOUND", `${ROOT}/control/stage_ref`, "对照必须使用同一能力绑定阶段，不能伪称已完成其他阶段。");

  function compileInputs(values: readonly { name: string; value: string }[] | undefined, path: string) {
    const context = structuredClone(contract.fixture.context);
    const overrides = new Map<string, Value>();
    for (const [index, item] of (values ?? []).entries()) {
      const field = inputs.get(item.name);
      if (!field) { add("INPUT_NOT_BOUND", `${path}/${index}/name`, "输入名必须精确匹配能力合同的可控字段。"); continue; }
      if (overrides.has(item.name)) { add("DUPLICATE_BOUND_INPUT", `${path}/${index}/name`, "每组实验对同一字段只能赋值一次。"); continue; }
      const value = parse(item.value, field);
      if (!value.ok) { add("INPUT_VALUE_TYPE_INVALID", `${path}/${index}/value`, "取值不符合字段类型、枚举、日期或数值范围；不可隐式转换。"); continue; }
      const location = target(context, item.name)!;
      (location.parent as Json)[location.key] = value.value;
      overrides.set(item.name, value.value);
    }
    for (const field of contract.inputs) if (field.required === true && !overrides.has(field.name!))
      add("REQUIRED_BOUND_INPUT_MISSING", path, "能力合同要求显式提供的输入尚未赋值。");
    return { context, overrides };
  }
  const primary = compileInputs(spec?.inputs, `${ROOT}/inputs`);
  const control = compileInputs(spec?.control?.inputs, `${ROOT}/control/inputs`);
  if (![...primary.overrides].some(([name, value]) => control.overrides.has(name) && stable(control.overrides.get(name)) !== stable(value)))
    add("BOUND_CONTROL_NOT_DISTINGUISHABLE", `${ROOT}/control/inputs`, "主实验与对照至少要对同一绑定字段提供不同的有效值。");

  const observations = new Map<string, Field>();
  for (const [index, observation] of (spec?.observations ?? []).entries()) {
    const field = outputs.get(observation.field);
    if (observation.source_ref !== contract.source_ref) add("OBSERVATION_SOURCE_NOT_BOUND", `${ROOT}/observations/${index}/source_ref`, "观察必须由能力合同指定的运行来源产生。");
    if (!field) add("OUTPUT_NOT_BOUND", `${ROOT}/observations/${index}/field`, "观察字段必须是函数合同中实际存在的输出，不能自造属性。");
    else {
      if (observations.has(observation.id)) add("DUPLICATE_BOUND_OBSERVATION", `${ROOT}/observations/${index}/id`, "绑定观察 id 必须唯一。");
      observations.set(observation.id, field);
    }
  }
  if (!spec?.control || !observations.has(spec.control.observation_ref))
    add("CONTROL_OUTPUT_NOT_BOUND", `${ROOT}/control/observation_ref`, "对照必须引用真实且已绑定的输出观察。");
  const asserted = new Set<string>(); const boundRequirements = new Set<string>(); const assertions: Json[] = [];
  for (const [index, expectation] of (spec?.expectations ?? []).entries()) {
    const path = `${ROOT}/expectations/${index}`;
    const field = observations.get(expectation.observation_ref);
    if (!field) { add("ASSERTION_OUTPUT_NOT_BOUND", `${path}/observation_ref`, "断言必须指向已绑定的实际输出观察。"); continue; }
    asserted.add(expectation.observation_ref);
    if (!proposal.requirement_refs.includes(expectation.requirement_ref) || !bundle.requirement_refs.includes(expectation.requirement_ref) ||
      (contract.supported_requirements && !contract.supported_requirements.includes(expectation.requirement_ref)))
      add("ASSERTION_REQUIREMENT_NOT_SUPPORTED", `${path}/requirement_ref`, "断言规则必须同时属于提案、发布包与能力支持的要求。");
    else boundRequirements.add(expectation.requirement_ref);
    if (!field.operators!.includes(expectation.operator)) add("ASSERTION_OPERATOR_NOT_BOUND", `${path}/operator`, "该输出类型不支持此断言算子。");
    let value: { ok: true; value: Value } | { ok: false };
    if (["PRESENT", "ABSENT"].includes(expectation.operator)) value = field.nullable === true && expectation.value === "" ? { ok: true, value: null } : { ok: false };
    else if (["GREATER_THAN", "LESS_THAN"].includes(expectation.operator)) value = field.type === "number" ? parse(expectation.value, { ...field, nullable: false }) : { ok: false };
    else if (expectation.operator === "CONTAINS" && field.type === "string[]") value = text(expectation.value) ? { ok: true, value: expectation.value } : { ok: false };
    else if (expectation.operator === "CONTAINS") value = field.type === "string" ? parse(expectation.value, { ...field, nullable: false }) : { ok: false };
    else value = parse(expectation.value, field);
    if (!value.ok) add("ASSERTION_VALUE_TYPE_INVALID", `${path}/value`, "断言值不符合输出类型/枚举；存在性仅适用 nullable 输出，数组 CONTAINS 使用单个字符串成员。");
    else assertions.push({ observation_ref: expectation.observation_ref, field: field.field, operator: expectation.operator,
      value: value.value, requirement_ref: expectation.requirement_ref });
  }
  for (const id of observations.keys()) if (!asserted.has(id)) add("OBSERVATION_WITHOUT_ASSERTION", `${ROOT}/expectations`, "每个观察都必须有指向它的具体断言。");
  for (const requirement of proposal.requirement_refs) if (!boundRequirements.has(requirement))
    add("REQUIREMENT_WITHOUT_ASSERTION", `${ROOT}/expectations`, "提案中的每项要求都必须绑定到受支持的实际输出断言。");

  const relation = extension?.change_relation;
  let compiledRelation: Json | undefined;
  if (!relation) add("CHANGE_RELATION_REQUIRED", `${ROOT}/change_relation`, "说明本次真实 diff 如何关联输入、观察以及 B/C 预期差异。");
  else {
    const path = `${ROOT}/change_relation`;
    const material = bundle.materials.find(item => item.material_ref === relation.diff_ref);
    const visible = bundle.changes.some(change => change["diff_material_ref"] === relation.diff_ref);
    if (!material || !visible || material.kind !== "UNTRUSTED_DIFF" || material.git_commit !== bundle.candidate_sha ||
      !proposal.evidence_refs.includes(relation.diff_ref)) add("CHANGE_DIFF_NOT_BOUND", `${path}/diff_ref`, "差异必须是当前候选发布包登记且本提案已引用的 diff。");
    const changedLines = material?.content.split("\n").filter(line => /^[+-]/.test(line) && !/^(---|\+\+\+)/.test(line)).map(line => line.slice(1)).join("\n") ?? "";
    if (!relation.changed_fields.length) add("CHANGED_FIELDS_REQUIRED", `${path}/changed_fields`, "至少指出一个实际改动行中的字段或符号。");
    for (const [index, name] of relation.changed_fields.entries()) {
      const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      if (!text(name) || !new RegExp(`(^|[^A-Za-z_0-9])${escaped}($|[^A-Za-z_0-9])`).test(changedLines))
        add("CHANGED_FIELD_NOT_IN_DIFF", `${path}/changed_fields/${index}`, "所述字段或符号没有出现在实际新增/删除行；这只核对引用，不证明因果关系。");
    }
    if (!relation.input_refs.length || relation.input_refs.some(name => !primary.overrides.has(name)))
      add("CHANGE_INPUT_NOT_BOUND", `${path}/input_refs`, "变更关系必须引用主实验显式设置的有效输入。");
    if (!relation.output_refs.length || new Set(relation.output_refs).size !== relation.output_refs.length || relation.output_refs.some(id => !observations.has(id)))
      add("CHANGE_OUTPUT_NOT_BOUND", `${path}/output_refs`, "变更关系必须引用唯一、已绑定的观察 id。");
    if (!boundRequirements.has(relation.requirement_ref)) add("CHANGE_REQUIREMENT_NOT_BOUND", `${path}/requirement_ref`, "变更关系必须引用已绑定到断言的要求。");
    if (!text(relation.reason)) add("CHANGE_REASON_REQUIRED", `${path}/reason`, "说明所选输入和观察为何与变化有关；不能留空。");
    function expected(encoded: string, fieldPath: string): Json | null {
      if (!text(encoded) || !relation!.output_refs.length) { add("CHANGE_EXPECTATION_REQUIRED", fieldPath, "提供可按输出类型解析的 B/C 预期。"); return null; }
      const values: Json = Object.create(null) as Json;
      let decoded: unknown;
      if (relation!.output_refs.length > 1) {
        try { decoded = JSON.parse(encoded); } catch { decoded = null; }
        if (!object(decoded) || Object.keys(decoded).length !== relation!.output_refs.length ||
          relation!.output_refs.some(id => !OWN(decoded as Json, id))) {
          add("CHANGE_EXPECTATION_TYPE_INVALID", fieldPath, "多个输出的预期使用按观察 id 索引的 JSON 对象，必须完整且无额外字段。"); return null;
        }
      }
      for (const id of relation!.output_refs) {
        const field = observations.get(id);
        if (!field) return null;
        const value = relation!.output_refs.length === 1 ? parse(encoded, field) :
          typed((decoded as Json)[id], field) ? { ok: true as const, value: (decoded as Json)[id] as Value } : { ok: false as const };
        if (!value.ok) { add("CHANGE_EXPECTATION_TYPE_INVALID", fieldPath, "B/C 预期必须符合所引用真实输出的类型与枚举。"); return null; }
        values[id] = value.value;
      }
      return values;
    }
    const baseline = expected(relation.expected_baseline, `${path}/expected_baseline`);
    const candidate = expected(relation.expected_candidate, `${path}/expected_candidate`);
    if (baseline && candidate && ((relation.claim === "SHOULD_PRESERVE" && stable(baseline) !== stable(candidate)) ||
      (relation.claim === "MAY_CHANGE" && stable(baseline) === stable(candidate))))
      add("CHANGE_EXPECTED_RELATION_INCONSISTENT", `${path}/claim`, "SHOULD_PRESERVE 要求 B/C 预期一致；MAY_CHANGE 必须列出至少一个不同的输出预期。");
    compiledRelation = { ...relation, expected_baseline: baseline, expected_candidate: candidate,
      relation_semantics: "MODEL_HYPOTHESIS_NOT_VERIFIED" };
  }
  return finish({ schema_version: 1, capability_ref: contract.capability_ref, parent_capability_ref: contract.parent_capability_ref,
    execution_mode: "PURE_FIXTURE", baseline_sha: bundle.baseline_sha, candidate_sha: bundle.candidate_sha,
    candidate_source_sha256: candidateSource?.sha256,
    contract_ref: contract.id, contract_sha256: digest(contract), fixture_ref: contract.fixture.id, fixture_sha256: contract.fixture.sha256,
    entrypoint: structuredClone(contract.entrypoint), source_sha256: contract.source_sha256,
    scenario_ref: contract.scenario_ref, stage_ref: contract.stage_ref,
    primary_context: primary.context, control_context: control.context,
    primary_context_sha256: sha(stable(primary.context)), control_context_sha256: sha(stable(control.context)),
    assertions, control_relation: spec?.control ? { observation_ref: spec.control.observation_ref,
      expected_relation: spec.control.expected_relation, reason: spec.control.reason } : null,
    change_relation: compiledRelation ?? null,
    stage_prerequisites: contract.stage_prerequisites.map(item => ({ ...structuredClone(item), evaluation_status: "NOT_EVALUATED" })),
    execution_ready: false, execution_status: "NOT_EXECUTED", semantic_correctness: "NOT_ESTABLISHED", dispatch_qualified: false });
}
