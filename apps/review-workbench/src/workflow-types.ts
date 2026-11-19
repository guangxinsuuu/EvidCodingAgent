import type { EvidenceReviewInput, EvidenceTarget, EvidenceResponse } from "@egap/contracts";
import type { Actor, ImportCase } from "./types.js";

/** 人员命令的版本依据与异步执行依据分离：转派不改变 request spec。 */
export interface WorkflowBasis {
  idempotency_key: string; expected_task_revision: number; expected_assignment_revision: number;
  expected_case_scope_revision: number; reviewed_bundle_id: string; reviewed_bundle_digest: string; candidate_sha: string;
}
export type EvidenceItem = EvidenceReviewInput["request_items"][number];
export type EvidenceMaterial = EvidenceReviewInput["materials"][number];
export interface WorkflowArchive {
  historical_run_id: string; materials: EvidenceMaterial[]; targets: EvidenceTarget[];
  review_policy_version?: string;
}
export interface WorkflowSeed { legacy: ImportCase; archive: WorkflowArchive; actors: Actor[]; owner_id: string; qa_assignee_id: string }
export interface Budget { model_calls: number; read_calls: number }
export interface RunMapping {
  request_id: string; request_spec_revision: number; run_id: string; frozen_input_digest: string;
  execution_profile: string; config_digest: string; limits: Record<string, unknown>; credential_ref: string;
  allocation: Budget; consumed: Budget; settlement: "RESERVED" | "SETTLED" | "UNKNOWN";
  last_runtime_event_seq?: number; last_runtime_result_digest?: string;
}
export interface WorkItem { id: string; request_id: string; request_spec_revision: number; worker_id: string; attempts: number }
/** 旧 Run 的已验证读取仅用于追溯；这些引用不成为后继 Run 的有效读取回执。 */
export interface ExecutionContext {
  run_id: string; request_spec_revision: number; execution_state: string;
  read_receipts: Record<string, unknown>[]; remaining_gaps: string[]; consumed: Budget;
}
export interface RequestSnapshot {
  id: string; case_id: string; task_id: string; request_spec_revision: number; spec_digest: string;
  input: EvidenceReviewInput; input_digest: string; execution_state: string; review_state: string; fulfillment_state: string;
  withdrawn: boolean; current_response_id: string | null; mapping: RunMapping | null; execution_context: Partial<ExecutionContext>;
}
/** Worker 已通过 Runtime 确定性验证的结果。原提案与读回执在 Runtime 保留。 */
export interface RuntimeResult {
  request_id: string; request_spec_revision: number; run_id: string; input_digest: string;
  execution_state: "FINISHED" | "BLOCKED" | "UNKNOWN" | "CANCELLED";
  runtime_event_seq?: number;
  response?: EvidenceResponse; read_receipts?: Record<string, unknown>[]; materials?: EvidenceMaterial[]; consumed: Budget;
  clarification?: string;
}
