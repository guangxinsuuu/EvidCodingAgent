import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";
import { DecisionProposalError } from "./verify.js";

/** 历史材料的归属键；相同 Job 名称不能跨租户、run、阶段或 ticket revision 复用。 */
export interface EvidenceTarget {
  tenant_id: string; historical_run_id: string; job_id: string; ticket_id: string; ticket_revision: number; phase: string;
}
export interface EvidenceRequestItem {
  item_id: string; kind: string; target: EvidenceTarget; required: boolean; acceptance_criteria: string[];
}
export interface EvidenceMaterial {
  material_ref: string; kind: string; title: string; sha256: string; content: string;
  provenance: EvidenceTarget & { candidate_sha: string; source_ref: string; source_sha256: string };
}
/** Workbench 冻结请求 → Worker 装配 → Runtime。身份和材料由服务端登记，预算另由 Runtime 预留。 */
export interface EvidenceReviewInput {
  schema_version: 2; task_mode: "EVIDENCE_REVIEW"; scope: "LOCAL_ARCHIVED_EVIDENCE";
  tenant_id: string; candidate_sha: string; historical_run_id: string;
  request_id: string; request_spec_revision: number; request_spec_sha256: string;
  case_id: string; case_scope_revision: number; task_id: string; obligation_id: string;
  source_bundle_id: string; source_bundle_sha256: string; goal: string;
  reviewed_policy: { policy_id: string; policy_version: string; sha256: string };
  authorized_manifest: { manifest_id: string; manifest_version: string; sha256: string };
  /** 后继调查的历史导航；这些旧receipt不是本run已观察证据。旧冻结输入可省略以保持原摘要。 */
  continuation?: null | { predecessor_run_id: string; predecessor_request_spec_revision: number;
    read_receipt_refs: string[]; remaining_gaps: string[]; human_answer: string };
  request_items: EvidenceRequestItem[]; materials: EvidenceMaterial[];
}
export type EvidenceDisposition = "PROVIDED" | "PARTIAL" | "UNAVAILABLE" | "CONFLICTING";
export type EvidenceUnavailableReason = "NOT_CAPTURED_IN_PINNED_ARCHIVE" | "NOT_FOUND_IN_AUTHORIZED_SOURCES" | "ACCESS_DENIED" | "SOURCE_UNREACHABLE";
export interface EvidenceLocation { material_ref: string; receipt_ref: string; start_line: number; end_line: number }
export interface EvidenceResponseItem {
  item_id: string; proposed_disposition: EvidenceDisposition;
  evidence_refs: string[]; read_receipt_refs: string[]; supporting_locations: EvidenceLocation[];
  remaining_gaps: string[]; unavailable_reason: EvidenceUnavailableReason | null; explanation: string;
}
export interface ValidatedEvidenceResponseItem extends EvidenceResponseItem {
  validated_disposition: EvidenceDisposition;
  association_status: "BOUND" | "NO_SUPPORTING_MATERIAL";
  /** 结构与关联通过不等于公司业务语义已被 QA 接受。 */
  semantic_acceptance: "NOT_EVALUATED";
}
export interface EvidenceResponse {
  schema_version: 2; response_id: string; response_sha256: string; request_id: string;
  request_spec_revision: number; request_spec_sha256: string; run_id: string; operation_id: string;
  candidate_sha: string; historical_run_id: string; tenant_id: string; case_id: string; case_scope_revision: number;
  task_id: string; obligation_id: string; source_bundle_id: string; source_bundle_sha256: string;
  status: "PROPOSED"; qa_acceptance: "NOT_EVALUATED"; items: ValidatedEvidenceResponseItem[]; summary: string;
}
interface EvidenceDecisionBasis {
  schema_version: 2; proposal_id: string; based_on_decision_revision: number; candidate_epoch: number;
  context_view_ref: string; unresolved_questions: string[]; expected_information: string; rationale: string;
}
export type EvidenceDecisionProposal = EvidenceDecisionBasis & (
  | { action_kind: "READ"; payload: { material_ref: string; start_line: number; end_line: number } }
  | { action_kind: "CLARIFY"; payload: { question: string } }
  | { action_kind: "PROPOSE_EVIDENCE_RESPONSE"; payload: { items: EvidenceResponseItem[]; summary: string } }
  | { action_kind: "STOP"; payload: { reason: string } }
);

/** 由单一 TS authority 计算摘要，调用方保存此值而不是另写 JSON 排序算法。 */
export function evidenceReviewDigest(value: unknown): string {
  const canonical = (v: unknown): string => v === null || typeof v !== "object" ? JSON.stringify(v)
    : Array.isArray(v) ? "[" + v.map(canonical).join(",") + "]"
      : "{" + Object.entries(v).sort(([a], [b]) => a.localeCompare(b, "en"))
        .map(([k, item]) => JSON.stringify(k) + ":" + canonical(item)).join(",") + "}";
  return createHash("sha256").update(canonical(value)).digest("hex");
}
const text = (maxLength = 300) => ({ type: "string", minLength: 1, maxLength });
const object = (properties: Record<string, unknown>) => ({ type: "object", additionalProperties: false, required: Object.keys(properties), properties });
const hash = { type: "string", pattern: "^[0-9a-f]{64}$" }, commit = { type: "string", pattern: "^[0-9a-f]{40}$" };
const revision = { type: "integer", minimum: 1, maximum: Number.MAX_SAFE_INTEGER };
const target = { tenant_id: text(), historical_run_id: text(), job_id: text(), ticket_id: text(), ticket_revision: revision, phase: text() };
const inputSchema = object({ schema_version: { const: 2 }, task_mode: { const: "EVIDENCE_REVIEW" }, scope: { const: "LOCAL_ARCHIVED_EVIDENCE" },
  tenant_id: text(), candidate_sha: commit, historical_run_id: text(), request_id: text(), request_spec_revision: revision,
  request_spec_sha256: hash, case_id: text(), case_scope_revision: revision, task_id: text(), obligation_id: text(),
  source_bundle_id: text(), source_bundle_sha256: hash, goal: text(4000),
  reviewed_policy: object({ policy_id: text(), policy_version: text(), sha256: hash }),
  authorized_manifest: object({ manifest_id: text(), manifest_version: text(), sha256: hash }),
  continuation: { anyOf: [{ type: "null" }, object({ predecessor_run_id: text(), predecessor_request_spec_revision: revision,
    read_receipt_refs: { type: "array", maxItems: 32, uniqueItems: true, items: text() },
    remaining_gaps: { type: "array", maxItems: 32, uniqueItems: true, items: text(1500) }, human_answer: text(2000) })] },
  request_items: { type: "array", minItems: 1, maxItems: 20, items: object({ item_id: text(), kind: text(), target: object(target),
    required: { type: "boolean" }, acceptance_criteria: { type: "array", minItems: 1, maxItems: 20, uniqueItems: true, items: text(1500) } }) },
  materials: { type: "array", maxItems: 100, items: object({ material_ref: text(), kind: text(), title: text(500), sha256: hash,
    content: { type: "string", maxLength: 300_000 }, provenance: object({ ...target, candidate_sha: commit, source_ref: text(), source_sha256: hash }) }) },
});
const ajv = new Ajv2020({ strict: true, allErrors: true, coerceTypes: false, useDefaults: false, removeAdditional: false });
// 不把旧输入缺省值写成null，避免规范化改变已冻结Run的摘要；新装配器显式提供null或历史导航。
inputSchema.required = inputSchema.required.filter(key => key !== "continuation");
const validateInput = ajv.compile<EvidenceReviewInput>(inputSchema);
function freezeTree<T>(value: T): T {
  if (value !== null && typeof value === "object") { for (const child of Object.values(value)) freezeTree(child); Object.freeze(value); }
  return value;
}
export const evidenceDecisionProposalSchema = freezeTree(JSON.parse(readFileSync(new URL(
  "../../../protocols/verify-agent/v2/evidence-decision-proposal.schema.json", import.meta.url), "utf8")) as Record<string, unknown>);
const validateProposal = ajv.compile<EvidenceDecisionProposal>(evidenceDecisionProposalSchema);
function invalid(): never { throw new DecisionProposalError([{ keyword: "evidenceBinding", path: "" }]); }
/** 严格输入边界：材料摘要、全局来源身份、重复 item/ref 都在接收时拒绝。 */
export function parseEvidenceReviewInput(value: unknown): EvidenceReviewInput {
  if (!validateInput(value)) throw new DecisionProposalError((validateInput.errors ?? []).slice(0, 8).map(e => ({ keyword: e.keyword, path: e.instancePath })));
  if (Buffer.byteLength(JSON.stringify(value)) > 1_500_000) invalid();
  if (new Set(value.request_items.map(i => i.item_id)).size !== value.request_items.length ||
    new Set(value.materials.map(m => m.material_ref)).size !== value.materials.length) invalid();
  for (const item of value.request_items) if (item.target.tenant_id !== value.tenant_id || item.target.historical_run_id !== value.historical_run_id) invalid();
  for (const m of value.materials) {
    if (m.provenance.tenant_id !== value.tenant_id || m.provenance.historical_run_id !== value.historical_run_id ||
      m.provenance.candidate_sha !== value.candidate_sha || createHash("sha256").update(m.content).digest("hex") !== m.sha256) invalid();
  }
  return freezeTree(structuredClone(value));
}
export function parseEvidenceDecisionProposal(value: unknown): EvidenceDecisionProposal {
  if (!validateProposal(value)) throw new DecisionProposalError((validateProposal.errors ?? []).slice(0, 8).map(e => ({ keyword: e.keyword, path: e.instancePath })));
  return freezeTree(structuredClone(value));
}
