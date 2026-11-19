/** Web / CLI 共用的审查协议；它与 Planner 的 Run token、SUT 身份相互独立。 */
export const REVIEW_ROLES = ["BUSINESS", "SOURCE", "QA"] as const;
export type ReviewRole = typeof REVIEW_ROLES[number];
export interface Actor { id: string; display_name: string; role: ReviewRole; tenant_id: string }
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

/** 导入器只提交脱敏、限长材料；content 是展示文本，不是 HTML，也不是可执行命令。 */
export interface ImportEvidence {
  id: string;
  title: string;
  kind: string;
  content: string;
  sha256: string;
  source_ref: string;
  source_sha256: string;
  limitations: string[];
}
export interface ImportObligation { role: ReviewRole; title: string; instruction: string }
export interface ImportCase {
  id: string;
  tenant_id: string;
  title: string;
  candidate_sha: string;
  baseline_sha: string | null;
  execution_mode: string;
  source_ref: string;
  source_sha256: string;
  machine_result: { [key: string]: Json };
  limitations: string[];
  evidence: ImportEvidence[];
  obligations: ImportObligation[];
}
export interface CommandBasis {
  idempotency_key: string;
  expected_revision: number;
  candidate_sha: string;
  bundle_digest: string;
  reason: string;
  evidence_refs: string[];
}
export interface DecisionCommand extends CommandBasis { decision: "ACKNOWLEDGE" | "REQUEST_CHANGES" }
export interface EvidenceRequestCommand extends CommandBasis { question: string }
export interface CommandResult {
  id: string;
  case_id: string;
  revision: number;
  candidate_sha: string;
  bundle_digest: string;
  kind: "DECISION" | "EVIDENCE_REQUEST";
  status: string;
  actor_id: string;
  role: ReviewRole;
}
export class ReviewError extends Error {
  constructor(readonly code: string, readonly status: number) { super(code); }
}
