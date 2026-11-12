import { decisionProposalSchema } from "@egap/contracts";
import { digest, type Json, type ReleaseBundle, type RunState } from "./types.js";

/** 稳定材料只给目录；diff 有界投影，源码/规则必须通过 READ 才进入事实上下文。 */
export function contextView(bundle: ReleaseBundle, state: RunState): Json {
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
    latest_feedback: state.latest_feedback, changes, requirement_refs: bundle.requirement_refs,
    catalog: state.read_receipts.length > 0 ? {
      status: bundle.catalog.status, dispatch_authorized: false, requirements: bundle.catalog.requirements,
      capabilities: bundle.catalog.capabilities, scenarios: bundle.catalog.scenarios,
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
  };
}
export function contextRef(bundle: ReleaseBundle, state: RunState): string {
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
export function modelBodySchema(bundle?: ReleaseBundle, state?: RunState): Json {
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
    const arrays = (values: string[]): Json => values.length ? { type: "array", maxItems: Math.min(32, new Set(values).size), uniqueItems: true,
      items: { ...reference, enum: [...new Set(values)] } } : { type: "array", maxItems: 0, items: reference };
    const shownChanges = contextView(bundle, state).changes as Json[];
    for (const branch of generated.oneOf) {
      const props = branch.properties as Json;
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
        const capabilities = (bundle.catalog.capabilities as Json[]).map(c => c.id);
        const scenarios = (bundle.catalog.scenarios as Json[]).map(c => c.id);
        const grounded = groundedRequirements(bundle, state);
        if (!capabilities.length || !scenarios.length || !grounded.length) (branch as Json).disabled = true;
        else {
          props.requirement_refs = { ...arrays(grounded), minItems: 1 };
          props.evidence_refs = { ...arrays(observed), minItems: 1 };
          payload.properties.capability_ref = { ...reference, enum: capabilities };
          payload.properties.scenario_ref = { ...reference, enum: scenarios };
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
