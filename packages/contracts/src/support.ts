import { Ajv2020 } from "ajv/dist/2020.js";

/**
 * 自有 GenAI Support SUT 的 DRAFT v1 接口，不是航空 Demo 已提供的接口。
 * HTTP adapter → 本模块校验传输形状/对象关联；不裁决退款资格、政策或权限。
 * 请求不允许 tenant/date/policy/答案字段：Tenant 由服务端认证，业务事实由工具读取。
 */
export interface SupportRequest {
  readonly schema_version: 1;
  readonly ticket_id: string;
  readonly ticket_revision: number;
}
export interface SupportSubmission extends SupportRequest {
  readonly job_id: string;
  readonly tenant_id: string;
  readonly status: "QUEUED";
}
export interface ResolutionDraft {
  readonly resolution_type: "REFUND_ELIGIBILITY_REVIEW";
  readonly ticket_id: string;
  readonly ticket_revision: number;
  readonly applicable_policy_ref: string | null;
  readonly billing_record_ref: string | null;
  readonly eligibility: "ELIGIBLE" | "INELIGIBLE" | "NEEDS_MORE_EVIDENCE";
  readonly proposed_action: "HUMAN_REVIEW" | "REQUEST_INFORMATION" | "NO_ACTION";
  readonly evidence_refs: readonly string[];
  readonly customer_reply_draft: string;
}
export interface SupportJob extends SupportRequest {
  readonly job_id: string;
  readonly tenant_id: string;
  readonly status: "QUEUED" | "PROCESSING" | "DONE" | "FAILED";
  readonly draft: ResolutionDraft | null;
  readonly error_code: "PROCESSING_FAILED" | null;
}

const reference = { type: "string", minLength: 1, maxLength: 128,
  pattern: "^[A-Za-z0-9][A-Za-z0-9._:-]*$", not: { pattern: "\\s" } };
const revision = { type: "integer", minimum: 1, maximum: Number.MAX_SAFE_INTEGER };
const requestProperties = { schema_version: { type: "integer", const: 1 }, ticket_id: reference, ticket_revision: revision };
const identityProperties = { ...requestProperties,
  job_id: { type: "string", pattern: "^[a-f0-9]{32}$", minLength: 32, maxLength: 32 }, tenant_id: reference };
const nullableReference = { anyOf: [reference, { type: "null" }] };
const draftProperties = {
  resolution_type: { const: "REFUND_ELIGIBILITY_REVIEW" }, ticket_id: reference, ticket_revision: revision,
  applicable_policy_ref: nullableReference, billing_record_ref: nullableReference,
  eligibility: { enum: ["ELIGIBLE", "INELIGIBLE", "NEEDS_MORE_EVIDENCE"] },
  proposed_action: { enum: ["HUMAN_REVIEW", "REQUEST_INFORMATION", "NO_ACTION"] },
  evidence_refs: { type: "array", maxItems: 32, uniqueItems: true, items: reference },
  customer_reply_draft: { type: "string", minLength: 1, maxLength: 16_384 },
};
function shape(properties: Record<string, unknown>): Record<string, unknown> {
  return { type: "object", additionalProperties: false, required: Object.keys(properties), properties };
}
const draftSchema = shape(draftProperties);
/** 深冻结导出的 Schema，避免加载者运行时改变其他进程应共享的接口说明。 */
function freeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
export const supportRequestSchema = freeze({ $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "urn:egap:support-request:1", ...shape(requestProperties) });
export const supportSubmissionSchema = freeze({ $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "urn:egap:support-submission:1", ...shape({ ...identityProperties, status: { const: "QUEUED" } }) });
export const supportJobSchema = freeze({ $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "urn:egap:support-job:1", oneOf: [
    shape({ ...identityProperties, status: { enum: ["QUEUED", "PROCESSING"] }, draft: { type: "null" }, error_code: { type: "null" } }),
    shape({ ...identityProperties, status: { const: "DONE" }, draft: draftSchema, error_code: { type: "null" } }),
    shape({ ...identityProperties, status: { const: "FAILED" }, draft: { type: "null" }, error_code: { const: "PROCESSING_FAILED" } }),
  ] });
const ajv = new Ajv2020({ strict: true, allErrors: false, coerceTypes: false, useDefaults: false, removeAdditional: false });
const requestValidator = ajv.compile<SupportRequest>(supportRequestSchema);
const submissionValidator = ajv.compile<SupportSubmission>(supportSubmissionSchema);
const jobValidator = ajv.compile<SupportJob>(supportJobSchema);
function invalid(): never { throw new TypeError("Invalid support protocol value."); }

/** 固定错误不回显工单文本；返回独立快照，调用方无法在验证后更改已验请求。 */
export function parseSupportRequest(value: unknown): Readonly<SupportRequest> {
  if (!requestValidator(value)) invalid();
  return Object.freeze({ schema_version: 1, ticket_id: value.ticket_id, ticket_revision: value.ticket_revision });
}
/** 202 仅确认排队；身份是否属于当前请求仍由 adapter 对照认证上下文检查。 */
export function parseSupportSubmission(value: unknown): Readonly<SupportSubmission> {
  if (!submissionValidator(value)) invalid();
  return Object.freeze({ ...parseSupportRequest({ schema_version: 1, ticket_id: value.ticket_id, ticket_revision: value.ticket_revision }),
    job_id: value.job_id, tenant_id: value.tenant_id, status: "QUEUED" });
}
/**
 * DONE 必须携带同工单/版本的草稿；FAILED 只暴露固定错误码。
 * 这只阻止协议矛盾，不能证明实际持久提交、引用真实性或自然语言正确。
 */
export function parseSupportJob(value: unknown): Readonly<SupportJob> {
  if (!jobValidator(value)) invalid();
  const draft = value.draft;
  if (draft && (draft.ticket_id !== value.ticket_id || draft.ticket_revision !== value.ticket_revision)) invalid();
  return Object.freeze({ schema_version: 1, job_id: value.job_id, tenant_id: value.tenant_id,
    ticket_id: value.ticket_id, ticket_revision: value.ticket_revision, status: value.status, error_code: value.error_code,
    draft: draft ? Object.freeze({ ...draft, evidence_refs: Object.freeze([...draft.evidence_refs]) }) : null });
}
