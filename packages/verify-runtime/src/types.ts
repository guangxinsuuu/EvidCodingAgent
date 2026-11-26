import { createHash } from "node:crypto";
import { validateProductBinding, type ProductBinding } from "./product-authority.js";
import { validateSourceBindings, type SourceBinding, type SimulatedSourceReader } from "./source-authority.js";
import { parseReviewRequest, type ReviewRequest, type ReviewDecisionProposal,
  type DecisionProposal, type HypothesisUpdate, type EvidenceReviewInput, type EvidenceDecisionProposal } from "@egap/contracts";

export type Json = Record<string, unknown>;
export interface Material {
  material_ref: string; kind: string; title: string; git_commit: string;
  path: string; sha256: string; content: string;
  /** 独立产品的真实仓库/路径；path 保留历史能力合同使用的逻辑命名空间。 */
  repository_id?: string; source_path?: string;
  external_source_ref?: string; source_byte_start?: number; source_byte_end?: number;
}
export interface ReleaseBundle {
  schema_version: 1 | 2; task_mode: "RELEASE_VERIFICATION"; scope: "LOCAL_SYNTHETIC_PLANNING";
  input_provenance: string; goal: string; baseline_sha: string; candidate_sha: string;
  catalog_commit: string; catalog_basis_commit: string; requirement_refs: string[];
  changes: Json[]; materials: Material[]; catalog: Json; gaps: unknown[];
  current_candidate_coverage: never[]; evaluation_status: "NOT_RUN";
  planning_contracts?: Json[];
  /** v2 的任务依据由可信装配器冻结；不是模型可编辑的审批或执行授权。 */
  review_request?: ReviewRequest;
  product_binding?: ProductBinding;
  external_sources?: SourceBinding[];
}
export type RuntimeBundle = ReleaseBundle | EvidenceReviewInput;
export type RuntimeProposal = DecisionProposal | EvidenceDecisionProposal | ReviewDecisionProposal;
/** 仅冻结档案的有界读接口；MCP adapter 不获得数据库事务或写入工作台的权限。 */
export interface EvidenceReadPort {
  read(request: { run_id: string; operation_id: string; material_ref: string; sha256: string; start_line: number; end_line: number }): Promise<{ material_ref: string; sha256: string; start_line: number; end_line: number; content: string; segment_sha256: string }>;
}
export interface RuntimeConfig {
  databaseUrl: string;
  model: { endpoint: string; name: string; digest: string; timeoutMs: number; maxOutputTokens: number; seed?: number };
  plannerPrompt: string;
  evidenceReadPort?: EvidenceReadPort;
  sourceReader?: SimulatedSourceReader;
  /** Workbench 撤回/租约 fence 检查；在数据库事务外调用，失败不释放已预留额度。 */
  assertEvidenceDispatchAllowed?: (runId: string) => Promise<void>;
}
/** plan_repairs 从 model_calls 总额内预留，不能作为额外、未计费调用。 */
export interface Limits { model_calls: number; reads: number; no_progress: number; plan_repairs?: number }
export interface CheckRepair {
  status: "PENDING" | "SATISFIED" | "EXHAUSTED";
  root_proposal_id: string; latest_proposal_id: string;
  attempts_used: number; attempts_limit: number;
  latest_proposal: Extract<DecisionProposal, { action_kind: "CHECK_INTENT" }>;
  quality: Json;
}
export interface RunState {
  run_id: string; status: "ACTIVE" | "PLANNED" | "STOPPED" | "CANCELLED" | "BLOCKED";
  candidate_epoch: number; decision_basis_revision: number; event_seq: number;
  config_digest: string; limits: Limits; model_calls_used: number; reads_used: number;
  no_progress: number; hypotheses: HypothesisUpdate[]; read_receipts: Json[];
  latest_feedback: Json | null; stop_reason: string | null;
  identities?: Json;
  /** 与 latest_feedback 分开持久化：后续 READ 回执不能抹掉待补缺项或重置次数。 */
  check_repair?: CheckRepair;
  /** 主图工作产物：原提案仍在 operations，投影只保留最新草案与有界方向历史。 */
  review_cycle?: {
    last_frame?: ReviewDecisionProposal["decision_frame"];
    directions: { proposal_id: string; focus: string; evidence_refs: readonly string[]; observation_basis_digest?: string }[];
    /** 仅动作/无效输出完成结算时递增，不能在模型预留时使本轮 context 自行改变。 */
    decisions_completed?: number;
    closure_calls_used?: number;
    draft?: { ref: string; digest: string; basis_digest: string; status: "NEEDS_REVISION" | "BOUND_PLAN";
      proposal: Extract<ReviewDecisionProposal, { action_kind: "PLAN_DRAFT" }>;
      compiled: DecisionProposal | null; issues: readonly unknown[] };
    accepted_plan_ref?: string;
  };
  review_repair?: { status: "PENDING" | "SATISFIED" | "EXHAUSTED"; root_proposal_id: string;
    latest_proposal_id: string; attempts_used: number; attempts_limit: number };
  /** 总额内独立预留的最后一次主图审阅调用；未知或无效也不释放额度。 */
  review_close_calls_reserved?: number;
}
export class RuntimeError extends Error {
  constructor(readonly code: string, readonly statusCode = 409) { super(code); }
}
export function requireThat(value: unknown, code: string, status = 409): asserts value {
  if (!value) throw new RuntimeError(code, status);
}
/** 规范摘要只由 TS authority 产生；跨语言消费者保存引用，不自行重算 JSON 摘要。 */
export function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  return "{" + Object.entries(value).sort(([a], [b]) => a.localeCompare(b, "en"))
    .map(([key, item]) => JSON.stringify(key) + ":" + canonical(item)).join(",") + "}";
}
export function digest(value: unknown): string { return sha(canonical(value)); }
export function sha(text: string): string { return createHash("sha256").update(text).digest("hex"); }

/** 只接收可信 operator 装配的本地发布包；模型没有导入材料和改动规则的接口。 */
export function validateBundle(value: unknown): ReleaseBundle {
  requireThat(value && typeof value === "object", "INVALID_RELEASE_BUNDLE", 400);
  const b = value as ReleaseBundle;
  requireThat([1, 2].includes(b.schema_version) && b.task_mode === "RELEASE_VERIFICATION" &&
    b.scope === "LOCAL_SYNTHETIC_PLANNING" && ["LOCAL_COMMITTED_RELEASE", "CALIBRATION_SYNTHETIC", "REGISTERED_EXTERNAL_PRODUCT"].includes(b.input_provenance), "INVALID_RELEASE_SCOPE", 400);
  for (const id of [b.baseline_sha, b.candidate_sha, b.catalog_commit, b.catalog_basis_commit])
    requireThat(typeof id === "string" && /^[0-9a-f]{40}$/.test(id), "INVALID_COMMIT_IDENTITY", 400);
  requireThat(typeof b.goal === "string" && b.goal.length > 0 && b.goal.length <= 4000 &&
    Array.isArray(b.changes) && b.changes.length > 0 && b.changes.length <= 100 &&
    Array.isArray(b.materials) && b.materials.length <= 200 &&
    Array.isArray(b.requirement_refs) && (b.schema_version === 2 || b.requirement_refs.length > 0) &&
    Array.isArray(b.current_candidate_coverage) && b.current_candidate_coverage.length === 0 &&
    b.evaluation_status === "NOT_RUN", "INVALID_RELEASE_BUNDLE", 400);
  requireThat(b.catalog?.status === "DRAFT" && b.catalog.dispatch_authorized === false, "CATALOG_AUTHORITY_INVALID", 400);
  if (b.schema_version === 2) {
    const r = parseReviewRequest(b.review_request);
    requireThat(r.baseline.commit === b.baseline_sha && r.candidate.commit === b.candidate_sha && r.goal === b.goal,
      "REVIEW_REQUEST_IDENTITY_MISMATCH", 400);
    requireThat(canonical(r.requirements.map(item => item.id).sort()) === canonical([...b.requirement_refs].sort()),
      "REVIEW_REQUIREMENT_SCOPE_MISMATCH", 400);
  } else requireThat(b.review_request === undefined, "LEGACY_REVIEW_REQUEST_NOT_SUPPORTED", 400);
  validateProductBinding(b);
  validateSourceBindings(b);
  requireThat(Buffer.byteLength(canonical(b)) <= 1_500_000, "BUNDLE_TOO_LARGE", 400);
  const refs = new Set<string>();
  for (const m of b.materials) {
    requireThat(typeof m.material_ref === "string" && m.material_ref.length <= 300 && !refs.has(m.material_ref) &&
      typeof m.content === "string" && Buffer.byteLength(m.content) <= 300_000 && sha(m.content) === m.sha256 &&
      typeof m.path === "string" && !/(^|\/)\.env(?:\.|$)|(?:^|\/)(?:artifacts|reports|\.git|output)\//.test(m.path), "MATERIAL_INTEGRITY_INVALID", 400);
    refs.add(m.material_ref);
  }
  return structuredClone(b);
}
