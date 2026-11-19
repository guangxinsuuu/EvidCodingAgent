import { evidenceContext, evidenceModelSchema } from "./evidence-profile.js";
import { decisionProposalSchema } from "@egap/contracts";
import { digest, type Json, type ReleaseBundle, type RuntimeBundle, type RunState } from "./types.js";
import { planningContractView } from "./capability-authority.js";

/** 稳定材料只给目录；diff 有界投影，源码/规则必须通过 READ 才进入事实上下文。 */
export function contextView(bundle: RuntimeBundle, state: RunState): Json {
  if (bundle.task_mode === "EVIDENCE_REVIEW") return evidenceContext(bundle, state);
  const quality = state.check_repair?.quality;
  const issues = quality?.issues as {code:string;path:string;message:string}[] | undefined;
  // 大量同类缺项无损压缩为(code,path)+消息字典，不截断遗漏；数据库原回执不变。
  // 能力契约也占上下文，不能让重复说明挤掉模型真正需要修正的字段位置。
  const messages = new Map<string,string>();
  const compactIssues = issues?.map(({code,path,message}) => {
    if (!messages.has(message)) messages.set(message, "m" + messages.size);
    return [code,path,messages.get(message)];
  });
  const qualityView = issues && issues.length > 64 ? { ...quality,
    issues: compactIssues,
    issue_columns: ["code", "path", "message_ref"],
    issue_messages: Object.fromEntries([...messages].map(([message,id]) => [id,message])),
    issue_encoding: "CODE_PATH_WITH_SHARED_MESSAGES" } : quality;
  // attempts_used 是调用预留记账，不是新的决策依据。否则预留本次调用便会
  // 改变 context_ref，使返回的合法修正与 Graph 的调用前身份不一致。
  // 剩余次数另由 execution_limits 提供；权威 projection 仍保留完整计数。
  const repairView = state.check_repair ? {
    status: state.check_repair.status, root_proposal_id: state.check_repair.root_proposal_id,
    latest_proposal_id: state.check_repair.latest_proposal_id, latest_proposal: state.check_repair.latest_proposal,
    quality: qualityView, attempts_limit: state.check_repair.attempts_limit,
  } : null;
  let remaining = 12_000;
  const changes = bundle.changes.map(change => {
    const material = bundle.materials.find(m => m.material_ref === change.diff_material_ref);
    const excerpt = material?.content.slice(0, Math.min(4000, remaining)) ?? "";
    remaining -= excerpt.length;
    return { ...change, diff_excerpt: excerpt, excerpt_truncated: (material?.content.length ?? 0) > excerpt.length,
      line_numbering: "READ_USES_MATERIAL_LOCAL_LINES_NOT_DIFF_HUNK_LINE_NUMBERS" };
  });
  return {
    task: { goal: bundle.goal, task_mode: bundle.task_mode, scope: bundle.scope,
      input_provenance: bundle.input_provenance, baseline_sha: bundle.baseline_sha,
      candidate_sha: bundle.candidate_sha, catalog_commit: bundle.catalog_commit },
    // 初次先给规则和材料导航。读到业务依据后再投影选测目录，减少无关工具干扰。
    // 完整冻结目录仍在权威 bundle，既不丢资产身份，也不让模型扩大 dispatch 权限。
    // 同一缺项/上次规格仅回送一份；完整回执仍在数据库中，避免大提案重复挤占上下文。
    latest_feedback: state.latest_feedback?.status === "NEEDS_REVISION" && repairView
      ? { status: state.latest_feedback.status, error_code: state.latest_feedback.error_code,
          correction: state.latest_feedback.correction, details_ref: "check_repair" }
      : state.latest_feedback,
    check_repair: repairView,
    changes, requirement_refs: bundle.requirement_refs,
    catalog: state.read_receipts.length > 0 ? {
      status: bundle.catalog.status, dispatch_authorized: false, requirements: bundle.catalog.requirements,
      capabilities: bundle.catalog.capabilities, scenarios: bundle.catalog.scenarios,
      sources: bundle.catalog.sources,
    } : { status: bundle.catalog.status, dispatch_authorized: false, requirements: bundle.catalog.requirements,
      capability_index_status: "AVAILABLE_AFTER_FIRST_READ" },
    material_index: bundle.materials.map(({ content, ...m }) => ({ material_ref: m.material_ref, title: m.title, kind: m.kind,
      total_lines: content.split("\n").length,
      read_window: { start_line: 1, end_line: Math.min(200, content.split("\n").length) } })),
    hypotheses: state.hypotheses, read_receipts: state.read_receipts,
    known_gaps: bundle.gaps,
    // 任意过去场景通过数都不属于本候选。此切片不执行实验，也不签发 PASS。
    current_candidate_coverage: [], final_eligibility: finalEligibility(),
    allowed_evidence_refs: evidenceRefs(bundle, state),
    profile: "LOCAL_MODEL_READ_AND_PLAN_ONLY",
    planning_contracts: (bundle.planning_contracts ?? []).map(planningContractView),
  };
}
export function contextRef(bundle: RuntimeBundle, state: RunState): string {
  return "context:" + digest({ run: state.run_id, epoch: state.candidate_epoch,
    revision: state.decision_basis_revision, view: contextView(bundle, state) });
}
export function evidenceRefs(bundle: ReleaseBundle, state: RunState): string[] {
  let remaining = 12_000;
  const visible = bundle.changes.flatMap(c => {
    const material = bundle.materials.find(m => m.material_ref === c.diff_material_ref);
    const size = Math.min(material?.content.length ?? 0, 4000, remaining);
    remaining -= size;
    return size > 0 && material ? [material.material_ref] : [];
  });
  return [...new Set([...visible,
    ...state.read_receipts.flatMap(r => [String(r.receipt_ref), String(r.material_ref)])])];
}
export function finalEligibility(): Json {
  return { eligible: false, evaluation_status: "NOT_RUN", gaps: [
    "BUSINESS_EXECUTION_NOT_ENABLED", "MANDATORY_COVERAGE_MISSING", "FINAL_EVALUATOR_NOT_CONNECTED",
  ] };
}
/** 被引用的合同片段必须确实读完；只看规则名称或第一行不能据此设计业务断言。 */
export function groundedRequirements(bundle: ReleaseBundle, state: RunState): string[] {
  return (bundle.catalog.requirements as Json[]).flatMap(requirement => {
    if (!bundle.requirement_refs.includes(String(requirement.id))) return [];
    const material = bundle.materials.find(m => m.material_ref === requirement.material_ref);
    if (!material) return [];
    let through = 0;
    for (const receipt of state.read_receipts.filter(r => r.material_ref === material.material_ref)
      .sort((a, b) => Number(a.start_line) - Number(b.start_line))) {
      if (Number(receipt.start_line) > through + 1) break;
      through = Math.max(through, Number(receipt.end_line));
    }
    return through >= material.content.split("\n").length ? [String(requirement.id)] : [];
  });
}
/** 同一协议派生生成提示；最终仍由共享 schema 校验。省略身份字段防止模型授予自己权限。 */
export function modelBodySchema(bundle?: RuntimeBundle, state?: RunState): Json {
  if (bundle?.task_mode === "EVIDENCE_REVIEW") return evidenceModelSchema(bundle, state);
  const schema = structuredClone(decisionProposalSchema) as Json;
  const omitted = ["schema_version", "proposal_id", "based_on_decision_revision", "candidate_epoch", "context_view_ref"];
  const properties = schema.properties as Json;
  for (const key of omitted) delete properties[key];
  schema.required = (schema.required as string[]).filter(key => !omitted.includes(key));
  const branches = schema.allOf as { if: { properties: { action_kind: { const: string } } }; then: { properties: { payload: Json } } }[];
  // Ollama 的生成语法不完整支持 if/then。将同一权威协议转换为明确的互斥对象分支；
  // 不能把全量 Draft2020 schema 直接当作 Provider 已执行的约束，也不降低最终校验。
  const generated = { $defs: schema.$defs, oneOf: branches.map(branch => ({
    type: "object", additionalProperties: false, required: schema.required,
    properties: { action_kind: { const: branch.if.properties.action_kind.const },
      // 先写简短影响假设/目的，再选择引用，避免模型先盲选某个规则 ID 后补理由。
      ...Object.fromEntries(["hypothesis_updates", "expected_information", "rationale", "requirement_refs", "hypothesis_refs", "evidence_refs"]
        .map(key => [key, properties[key]])),
      payload: branch.then.properties.payload },
  })) };
  if (bundle && state) {
    // 生成时只提供本任务真实引用和有新内容的分页窗口。模型仍选择要读哪份材料，
    // 但无需猜行号/拼写工具 ID；服务端共享协议与语义校验仍独立执行。
    const observed = evidenceRefs(bundle, state);
    const reference = (schema.$defs as Json).reference as Json;
    const definitions = schema.$defs as Json;
    const contract = bundle.planning_contracts?.[0];
    if (contract) {
      // 生成选项直接派生于真实接口，不能让模型猜路径或选择未完成适配的命令。
      // 接收端仍独立编译并检查；缩小语法选项不表示语义正确或授权运行。
      const spec = definitions.checkSpec as Json;
      spec.required = ["schema_version", "stage_ref", "inputs", "observations", "expectations", "control", "binding", "change_relation"];
      const props = spec.properties as Json;
      props.stage_ref = { const: contract.stage_ref };
      props.binding = { type: "object", additionalProperties: false, required: ["contract_ref", "fixture_ref"],
        properties: { contract_ref: { const: contract.id }, fixture_ref: { const: (contract.fixture as Json).id } } };
      const inputVariants = (contract.inputs as Json[]).map(input => ({ type: "object", additionalProperties: false,
        required: ["name", "value"], properties: { name: { const: input.name }, value:
          input.enum ? { type: "string", enum: (input.enum as unknown[]).map(String) }
          : input.type === "boolean" ? { enum: ["true", "false"] }
          : input.type === "integer" ? { type: "string", pattern: "^-?[0-9]+$", maxLength: 18 }
          : { type: "string", minLength: 1, maxLength: 2000 } } }));
      definitions.checkInput = { oneOf: inputVariants };
      const observation = definitions.checkObservation as Json;
      (observation.properties as Json).source_ref = { const: contract.source_ref };
      (observation.properties as Json).field = { enum: (contract.outputs as Json[]).map(o => o.field) };
      const control = definitions.checkControl as Json;
      (control.properties as Json).scenario_ref = { const: contract.scenario_ref };
      (control.properties as Json).stage_ref = { const: contract.stage_ref };
      for (const key of ["inputs", "observations", "expectations"]) (props[key] as Json).minItems = 1;
    }
    if (state.check_repair?.status === "PENDING") {
      // 修正时生成器提示完整结构；最终仍由共享 Schema 和 quality 独立验证。
      // 未修改接收协议，故外部模型返回部分规格仍能得到逐项缺陷，而非绕过账本。
      const spec = definitions.checkSpec as Json;
      spec.required = [...new Set([...(spec.required as string[]), "stage_ref", "inputs", "observations", "expectations", "control"])];
      const props = spec.properties as Json;
      for (const key of ["inputs", "observations", "expectations"]) (props[key] as Json).minItems = 1;
    }
    const arrays = (values: string[]): Json => values.length ? { type: "array", maxItems: Math.min(32, new Set(values).size), uniqueItems: true,
      items: { ...reference, enum: [...new Set(values)] } } : { type: "array", maxItems: 0, items: reference };
    const shownChanges = contextView(bundle, state).changes as Json[];
    for (const branch of generated.oneOf) {
      const props = branch.properties as Json;
      if ((props.action_kind as {const:string}).const === "NOVEL_CHECK" && state.read_receipts.length === 0) {
        // 尚未读任何依据，不能把“不知道如何使用现有接口”伪装成已确认的能力缺口。
        (branch as Json).disabled = true; continue;
      }
      props.requirement_refs = arrays(bundle.requirement_refs);
      props.evidence_refs = arrays(observed);
      const hypotheses = props.hypothesis_updates as { items: { properties: Json } };
      hypotheses.items.properties.evidence_refs = arrays(observed);
      if (!state.hypotheses.length && ["READ", "CHECK_INTENT"].includes((props.action_kind as { const: string }).const)) {
        // 本切片要求首次影响判断显式留下一个可审查假设；它仍不是已证实业务事实。
        props.hypothesis_updates = { ...hypotheses, minItems: 1 };
      }
      if ((props.action_kind as { const: string }).const === "READ") {
        if (state.reads_used >= state.limits.reads) { (branch as Json).disabled = true; continue; }
        const windows: Json[] = [];
        for (const m of bundle.materials) {
          // 完整展示过的 diff 不再作为新增信息；截断 diff 仍可按固定窗口继续读取。
          const shown = shownChanges.find(c => c.diff_material_ref === m.material_ref);
          if (shown && shown.excerpt_truncated === false) continue;
          const lines = m.content.split("\n").length;
          for (let start = 1; start <= lines; start += 200) {
            const end = Math.min(start + 199, lines);
            if (state.read_receipts.some(r => r.material_ref === m.material_ref && r.start_line === start && r.end_line === end)) continue;
            windows.push({ type: "object", additionalProperties: false, required: ["material_ref", "start_line", "end_line"],
              properties: { material_ref: { ...reference, const: m.material_ref }, start_line: { const: start }, end_line: { const: end } } });
          }
        }
        // 没有新窗口时不给 READ 生成分支；STOP/CLARIFY 等仍按自身准入条件处理。
        if (windows.length) props.payload = { oneOf: windows };
        else (branch as Json).disabled = true;
      }
      if ((props.action_kind as { const: string }).const === "CHECK_INTENT") {
        const payload = props.payload as { properties: Json };
        const capabilities = contract ? [contract.capability_ref] : (bundle.catalog.capabilities as Json[]).map(c => c.id);
        const scenarios = contract ? [contract.scenario_ref] : (bundle.catalog.scenarios as Json[]).map(c => c.id);
        const grounded = groundedRequirements(bundle, state).filter(ref => !contract ||
          (contract.supported_requirements as string[]).includes(ref));
        if (!capabilities.length || !scenarios.length || !grounded.length) (branch as Json).disabled = true;
        else {
          props.requirement_refs = { ...arrays(grounded), minItems: 1 };
          if (contract) {
            ((definitions.checkExpectation as Json).properties as Json).requirement_ref = { ...reference, enum: grounded };
            const relation = ((definitions.checkSpec as Json).properties as Json).change_relation as Json;
            (relation.properties as Json).requirement_ref = { ...reference, enum: grounded };
          }
          props.evidence_refs = { ...arrays(observed), minItems: 1 };
          payload.properties.capability_ref = { ...reference, enum: capabilities };
          payload.properties.scenario_ref = { ...reference, enum: scenarios };
          if (state.check_repair?.status === "PENDING" || contract) {
            const required = (props.payload as Json).required as string[];
            (props.payload as Json).required = [...required, "check_spec"];
          }
        }
      }
    }
    generated.oneOf = generated.oneOf.filter(b => !(b as Json).disabled);
  }
  return generated;
}

/** 给模型可读的紧凑接口说明；native format 用完整派生 schema，不在 Prompt 重复七份公共字段。 */
export function modelShapeHint(): string {
  return "JSON fields: action_kind, requirement_refs (array of IDs), hypothesis_refs (array of IDs), evidence_refs (array of IDs), expected_information (string), rationale (string), hypothesis_updates (array of {id,statement,status:OPEN|SUPPORTED|WEAKENED,evidence_refs}), payload (object matching selected action). Use empty arrays when appropriate. No other fields. All fields required.";
}
