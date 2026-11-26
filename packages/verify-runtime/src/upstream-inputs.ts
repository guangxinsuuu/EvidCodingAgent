import { canonical, requireThat, type Json, type ReleaseBundle, type RunState } from "./types.js";

export const upstreamRefs = {
  diagnosis: "diagnostics:INV-LAB-100", evidence: "diagnostics:INV-LAB-100-OBSERVATION",
  handoff: "diagnostics:INV-LAB-100-HANDOFF", provider: "providers:phase-platforms",
  qa: "acceptance:phase-review-contract",
} as const;
const object = (v: unknown): v is Json => v !== null && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string" && v.length > 0 && v.length <= 4000;
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.length <= 32 && v.every(text) && new Set(v).size === v.length;
const sha = (v: unknown): boolean => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const commit = (v: unknown): boolean => typeof v === "string" && /^[a-f0-9]{40}$/.test(v);

function keys(value: Json, required: string[], optional: string[] = []): void {
  requireThat(required.every(k => Object.hasOwn(value, k)) && Object.keys(value).every(k => [...required, ...optional].includes(k)),
    "UPSTREAM_FIELDS_INVALID", 400);
}
function material(bundle: ReleaseBundle, ref: string): Json | undefined {
  const rows = bundle.materials.filter(m => m.external_source_ref === ref);
  if (!bundle.external_sources?.some(s => s.ref === ref)) {
    requireThat(!rows.length, "UPSTREAM_SOURCE_REQUIRED", 400); return undefined;
  }
  requireThat(rows.length === 1 && rows[0]!.material_ref === ref && rows[0]!.source_byte_start === 0 &&
    rows[0]!.source_byte_end === Buffer.byteLength(rows[0]!.content), "UPSTREAM_FULL_ORIGINAL_REQUIRED", 400);
  let value: unknown;
  try { value = JSON.parse(rows[0]!.content); } catch { requireThat(false, "UPSTREAM_JSON_INVALID", 400); }
  requireThat(object(value), "UPSTREAM_OBJECT_REQUIRED", 400);
  return value;
}

/** 来源原件 → 任务准入。诊断、证据目录和 handoff 必须共同绑定准确候选与引用。
 * 这些是上游输入，不能写入 requirements、coverage、修复授权或 Final；缺原件保留为缺项。
 * 地址、凭据、命令都不属于此协议；可执行端口仍由 Runtime 受信安装配置登记。
 */
export function validateUpstreamInputs(bundle: ReleaseBundle): void {
  const diagnosis = material(bundle, upstreamRefs.diagnosis);
  const evidence = material(bundle, upstreamRefs.evidence);
  const handoff = material(bundle, upstreamRefs.handoff);
  if (diagnosis || evidence || handoff) {
    requireThat(diagnosis && evidence && handoff && bundle.product_binding, "PROJECT1_LINKED_ARTIFACTS_REQUIRED", 400);
    const common = ["schema_version", "kind", "investigation_id", "revision", "source_platform_id", "authority_scope",
      "candidate", "repair_authorized", "release_authorized", "current_candidate_coverage"];
    for (const value of [diagnosis, evidence, handoff]) {
      requireThat(value.schema_version === 1 && value.investigation_id === "INV-LAB-100" && value.revision === 1 &&
        value.source_platform_id === "simulated-project1" && value.authority_scope === "SYNTHETIC_LOCAL_CALIBRATION" &&
        value.repair_authorized === false && value.release_authorized === false && canonical(value.current_candidate_coverage) === "[]",
        "PROJECT1_AUTHORITY_UPGRADE_FORBIDDEN", 400);
      requireThat(object(value.candidate) && value.candidate.commit === bundle.candidate_sha &&
        value.candidate.repository_url === bundle.product_binding.repository_url, "PROJECT1_CANDIDATE_MISMATCH", 400);
      keys(value.candidate, ["repository_url", "commit"]);
    }
    keys(evidence, [...common, "records", "limitations"]);
    requireThat(evidence.kind === "PROJECT1_EVIDENCE_MANIFEST" && strings(evidence.limitations) &&
      Array.isArray(evidence.records) && evidence.records.length > 0 && evidence.records.length <= 16, "PROJECT1_EVIDENCE_INVALID", 400);
    const ids = new Set<string>();
    for (const value of evidence.records) {
      requireThat(object(value), "PROJECT1_EVIDENCE_INVALID", 400);
      keys(value, ["id", "kind", "sha256", "source_repository", "source_commit", "index_path", "observed_at", "deployment_id", "image_id", "original_access", "summary"]);
      requireThat(text(value.id) && !ids.has(value.id) && value.kind === "ARCHIVED_OBSERVATION_POINTER" && sha(value.sha256) &&
        commit(value.source_commit) && value.source_repository === "https://github.com/guangxinsuuu/EvidCodingAgent.git" &&
        typeof value.index_path === "string" && /^reports\/[A-Za-z0-9/._-]+\.json$/.test(value.index_path) && !value.index_path.includes("..") &&
        text(value.observed_at) && Number.isFinite(Date.parse(value.observed_at)) && text(value.deployment_id) &&
        typeof value.image_id === "string" && /^sha256:[a-f0-9]{64}$/.test(value.image_id) &&
        value.original_access === "REFERENCE_ONLY_NOT_INGESTED" && text(value.summary), "PROJECT1_EVIDENCE_INVALID", 400);
      ids.add(value.id);
    }
    keys(diagnosis, [...common, "environment", "evidence_manifest_ref", "classification", "hypotheses", "unresolved_gaps", "business_verdict"]);
    requireThat(diagnosis.kind === "PROJECT1_DIAGNOSIS" && diagnosis.evidence_manifest_ref === upstreamRefs.evidence &&
      diagnosis.classification === "UNDETERMINED" && diagnosis.business_verdict === "NOT_EVALUATED" &&
      strings(diagnosis.unresolved_gaps) && object(diagnosis.environment) && Array.isArray(diagnosis.hypotheses) &&
      diagnosis.hypotheses.length > 0 && diagnosis.hypotheses.length <= 16, "PROJECT1_DIAGNOSIS_INVALID", 400);
    keys(diagnosis.environment, ["kind", "deployment_id", "time_window"]);
    const environment = diagnosis.environment;
    const window = environment.time_window;
    requireThat(environment.kind === "LOCAL_SYNTHETIC" && object(window) &&
      text(window.start) && text(window.end) && Date.parse(window.start) <= Date.parse(window.end) &&
      evidence.records.every(r => (r as Json).deployment_id === environment.deployment_id), "PROJECT1_OBSERVATION_SCOPE_MISMATCH", 400);
    keys(window, ["start", "end"]);
    const hypotheses = new Set<string>();
    for (const value of diagnosis.hypotheses) {
      requireThat(object(value), "PROJECT1_HYPOTHESIS_INVALID", 400);
      keys(value, ["id", "statement", "status", "evidence_refs"]);
      requireThat(text(value.id) && !hypotheses.has(value.id) && text(value.statement) && value.status === "UNCONFIRMED" &&
        strings(value.evidence_refs) && value.evidence_refs.length > 0 && value.evidence_refs.every(r => ids.has(r)), "PROJECT1_HYPOTHESIS_INVALID", 400);
      hypotheses.add(value.id);
    }
    keys(handoff, [...common, "diagnosis_ref", "evidence_manifest_ref", "evidence_refs", "suggested_paths", "validation_questions", "requested_next_steps", "openspec_status", "unresolved_gaps"]);
    requireThat(handoff.kind === "PROJECT1_ENGINEERING_HANDOFF" && handoff.diagnosis_ref === upstreamRefs.diagnosis &&
      handoff.evidence_manifest_ref === upstreamRefs.evidence && strings(handoff.evidence_refs) && handoff.evidence_refs.length > 0 &&
      handoff.evidence_refs.every(r => ids.has(r)) && strings(handoff.suggested_paths) &&
      handoff.suggested_paths.every(p => /^src\/genai_support\/[A-Za-z0-9/_-]+\.py$/.test(p)) &&
      handoff.openspec_status === "HANDOFF_INPUT_NOT_GENERATED_OPENSPEC_CHANGE" && strings(handoff.unresolved_gaps) &&
      strings(handoff.requested_next_steps) && handoff.requested_next_steps.every(v => ["INVESTIGATE_EVIDENCE", "DESIGN_VERIFICATION"].includes(v)) &&
      Array.isArray(handoff.validation_questions) && handoff.validation_questions.length > 0 && handoff.validation_questions.length <= 16,
      "PROJECT1_HANDOFF_INVALID", 400);
    for (const question of handoff.validation_questions) {
      requireThat(object(question), "PROJECT1_QUESTION_INVALID", 400);
      keys(question, ["requirement_ref", "question"]);
      requireThat(text(question.requirement_ref) && bundle.requirement_refs.includes(question.requirement_ref) && text(question.question),
        "PROJECT1_REQUIREMENT_NOT_IN_TASK", 400);
    }
  }
  const provider = material(bundle, upstreamRefs.provider);
  if (provider) {
    keys(provider, ["schema_version", "kind", "contract_id", "authority_scope", "approval_state", "maintainer_ref", "runtime_dispatch_authorized", "implementation_basis", "operations", "limitations"]);
    requireThat(provider.schema_version === 1 && provider.kind === "DECLARED_PROVIDER_CONTRACT" &&
      provider.contract_id === "providers.phase-platforms.v1" && provider.authority_scope === "SYNTHETIC_LOCAL_CALIBRATION" &&
      provider.approval_state === "DRAFT" && provider.maintainer_ref === "lab:service-owner-publisher" &&
      provider.runtime_dispatch_authorized === false && strings(provider.limitations) && object(provider.implementation_basis) &&
      Array.isArray(provider.operations) && provider.operations.length > 0 && provider.operations.length <= 32,
      "PROVIDER_CONTRACT_INVALID", 400);
    const basis = provider.implementation_basis;
    requireThat(basis.repository_url === "https://github.com/guangxinsuuu/EvidBusinessPlatforms.git" && commit(basis.commit) &&
      Array.isArray(basis.files) && basis.files.every(f => object(f) && text(f.path) && sha(f.sha256)), "PROVIDER_IMPLEMENTATION_BASIS_INVALID", 400);
    const ids = new Set<string>();
    for (const op of provider.operations) {
      requireThat(object(op), "PROVIDER_OPERATION_INVALID", 400);
      keys(op, ["id", "method", "path_template", "roles", "effect", "required_scope", "response_fields", "failure"], ["constraints", "pagination"]);
      requireThat(text(op.id) && !ids.has(op.id) && ["GET", "POST"].includes(String(op.method)) &&
        text(op.path_template) && op.path_template.startsWith("/") && !op.path_template.includes("://") &&
        strings(op.roles) && op.roles.length > 0 && op.roles.every(r => ["PRODUCT", "OBSERVER", "PREPARER"].includes(r)) &&
        op.effect === (op.method === "GET" ? "READ" : "WRITE") && !(op.method === "POST" && op.roles.includes("OBSERVER")) &&
        strings(op.required_scope) && strings(op.response_fields) && text(op.failure) &&
        (op.constraints === undefined || strings(op.constraints)) && (op.pagination === undefined || text(op.pagination)), "PROVIDER_OPERATION_INVALID", 400);
      ids.add(op.id);
    }
  }
  const qa = material(bundle, upstreamRefs.qa);
  if (qa) requireThat(qa.schema_version === 1 && qa.contract_id === "qa.commercial-phase-review.v1" && qa.version === "phase-v1" &&
    qa.effect === "NO_BUSINESS_WRITES_NO_MODEL_CALLS" && qa.operation === "REVIEW_SUPPLIED_OBSERVATIONS" &&
    qa.release_authorized === false && qa.protected_final === false && qa.authority_scope === "SYNTHETIC_LOCAL_CALIBRATION",
    "QA_PLANNING_CONTRACT_INVALID", 400);
}

/** 主图只获得来源导航和真实能力边界；未 READ 的诊断正文不偷偷塞进上下文。 */
export function upstreamContext(bundle: ReleaseBundle, state: RunState): Json {
  const refs = Object.values(upstreamRefs);
  return { inputs: bundle.materials.filter(m => refs.includes(m.material_ref as typeof refs[number])).map(m => ({
    material_ref: m.material_ref, kind: m.kind, sha256: m.sha256,
    read_receipt_present: state.read_receipts.some(r => r.material_ref === m.material_ref),
    authority: m.material_ref.startsWith("diagnostics:") ? "UPSTREAM_HYPOTHESIS_OR_ARCHIVE_POINTER" : "DECLARED_INTERFACE_NOT_EXECUTION_AUTHORITY",
  })), dispatch_qualified: false, inherited_candidate_coverage: [],
    evidence_limit: "Reading an evidence manifest is not reading its referenced private originals.",
    qa_execution_scope: "ARCHIVED_KNOWN_PHASE_REVIEW_REQUIRES_SEPARATE_EXECUTION_ADMISSION" };
}
