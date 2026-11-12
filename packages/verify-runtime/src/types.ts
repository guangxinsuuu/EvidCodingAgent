import { createHash } from "node:crypto";
import type { HypothesisUpdate } from "@egap/contracts";

export type Json = Record<string, unknown>;
export interface Material {
  material_ref: string; kind: string; title: string; git_commit: string;
  path: string; sha256: string; content: string;
}
export interface ReleaseBundle {
  schema_version: 1; task_mode: "RELEASE_VERIFICATION"; scope: "LOCAL_SYNTHETIC_PLANNING";
  input_provenance: string; goal: string; baseline_sha: string; candidate_sha: string;
  catalog_commit: string; catalog_basis_commit: string; requirement_refs: string[];
  changes: Json[]; materials: Material[]; catalog: Json; gaps: unknown[];
  current_candidate_coverage: never[]; evaluation_status: "NOT_RUN";
}
export interface RuntimeConfig {
  databaseUrl: string;
  model: { endpoint: string; name: string; digest: string; timeoutMs: number; maxOutputTokens: number };
  plannerPrompt: string;
}
export interface Limits { model_calls: number; reads: number; no_progress: number }
export interface RunState {
  run_id: string; status: "ACTIVE" | "PLANNED" | "STOPPED" | "CANCELLED" | "BLOCKED";
  candidate_epoch: number; decision_basis_revision: number; event_seq: number;
  config_digest: string; limits: Limits; model_calls_used: number; reads_used: number;
  no_progress: number; hypotheses: HypothesisUpdate[]; read_receipts: Json[];
  latest_feedback: Json | null; stop_reason: string | null;
  identities?: Json;
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
  requireThat(b.schema_version === 1 && b.task_mode === "RELEASE_VERIFICATION" &&
    b.scope === "LOCAL_SYNTHETIC_PLANNING" && ["LOCAL_COMMITTED_RELEASE", "CALIBRATION_SYNTHETIC"].includes(b.input_provenance), "INVALID_RELEASE_SCOPE", 400);
  for (const id of [b.baseline_sha, b.candidate_sha, b.catalog_commit, b.catalog_basis_commit])
    requireThat(typeof id === "string" && /^[0-9a-f]{40}$/.test(id), "INVALID_COMMIT_IDENTITY", 400);
  requireThat(typeof b.goal === "string" && b.goal.length > 0 && b.goal.length <= 4000 &&
    Array.isArray(b.changes) && b.changes.length > 0 && b.changes.length <= 100 &&
    Array.isArray(b.materials) && b.materials.length <= 200 &&
    Array.isArray(b.requirement_refs) && b.requirement_refs.length > 0 &&
    Array.isArray(b.current_candidate_coverage) && b.current_candidate_coverage.length === 0 &&
    b.evaluation_status === "NOT_RUN", "INVALID_RELEASE_BUNDLE", 400);
  requireThat(b.catalog?.status === "DRAFT" && b.catalog.dispatch_authorized === false, "CATALOG_AUTHORITY_INVALID", 400);
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
