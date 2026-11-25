import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";

/**
 * 操作员提交的审查委托；本地确认与公司批准严格分开。
 * 引用可暂时不存在，由装配器/Runtime 登记缺项；本协议不为材料授予权威或执行权。
 */
export interface ReviewRequirement {
  readonly id: string;
  readonly kind: "NEW" | "PRESERVE";
  readonly statement: string;
  readonly material_ref: string;
  readonly authority: "LOCAL_CALIBRATION" | "UNVERIFIED";
  readonly status: "CONFIRMED_LOCAL" | "PENDING" | "CONFLICT";
  readonly owner_ref: string;
}
export interface ReviewRequest {
  readonly schema_version: 1;
  readonly request_id: string;
  readonly revision: number;
  readonly requester_ref: string;
  readonly owner_ref: string;
  readonly goal: string;
  readonly non_goals: readonly string[];
  readonly baseline: { readonly commit: string; readonly reason: string; readonly source_ref: string };
  readonly candidate: { readonly commit: string; readonly source_ref: string };
  readonly requirements: readonly ReviewRequirement[];
  readonly known_gaps: readonly { readonly id: string; readonly requirement_refs: readonly string[]; readonly description: string }[];
  readonly predecessor_run_id: string | null;
}

export class ReviewRequestError extends Error {
  readonly error_code = "INVALID_REVIEW_REQUEST";
  constructor(readonly issues: readonly { keyword: string; path: string }[] = []) {
    // 不反射字段值或 Ajv params，避免私有需求内容进入通用日志。
    super("审查请求不符合当前协议。");
  }
}
function freezeTree<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) freezeTree(child);
    Object.freeze(value);
  }
  return value;
}
export const reviewRequestSchema = freezeTree(JSON.parse(readFileSync(new URL(
  "../../../protocols/review-request/v1/review-request.schema.json", import.meta.url,
), "utf8")) as Record<string, unknown>);
const ajv = new Ajv2020({ strict: true, allErrors: true, coerceTypes: false, useDefaults: false, removeAdditional: false });
const validate = ajv.compile<ReviewRequest>(reviewRequestSchema);

/**
 * CLI 装配器与 Runtime 的共同输入边界：只验证声明结构，不接受隐式默认或批准升级。
 * 返回与调用方隔离的不可变副本；具体 Git 对象、材料存在性及来源状态由可信组件核对。
 */
export function parseReviewRequest(value: unknown): ReviewRequest {
  if (!validate(value)) throw new ReviewRequestError((validate.errors ?? []).slice(0, 8)
    .map(error => ({ keyword: error.keyword, path: error.instancePath })));
  if (Buffer.byteLength(JSON.stringify(value)) > 300_000) throw new ReviewRequestError([{ keyword: "size", path: "" }]);
  if (new Set(value.requirements.map(item => item.id)).size !== value.requirements.length)
    throw new ReviewRequestError([{ keyword: "duplicateIdentity", path: "/requirements" }]);
  if (new Set(value.known_gaps.map(item => item.id)).size !== value.known_gaps.length)
    throw new ReviewRequestError([{ keyword: "duplicateIdentity", path: "/known_gaps" }]);
  const requirements = new Set(value.requirements.map(item => item.id));
  if (value.known_gaps.some(gap => gap.requirement_refs.some(ref => !requirements.has(ref))))
    throw new ReviewRequestError([{ keyword: "unknownRequirement", path: "/known_gaps" }]);
  return freezeTree(structuredClone(value));
}
