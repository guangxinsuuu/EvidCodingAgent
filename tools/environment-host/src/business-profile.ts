import { readFileSync, realpathSync } from "node:fs";
import { checkedMlPath } from "./ml-source-core.js";
import type { SourceBinding } from "./lab-ledger.js";

/**
 * CLI打开账本后、任何尝试分配之前核对来源；防止误复制旧账本后继承订单实验身份。
 * 新建未绑定实验可继续来源准备；已绑定实验必须属于当前固定ML来源/构建阶段。
 */
export function assertActiveSourceBindings(sources: Iterable<SourceBinding | null>): void {
  for (const source of sources) if (source !== null && (
    source.source_commit !== "c200bcb4721c4428a9bfe808ca8a61648a2b16f6" ||
    source.build_revision !== "ml-upstream-unbuilt-v1" || source.business_revision !== "unchanged-upstream"
  )) throw new Error("ACTIVE_BUSINESS_SOURCE_MISMATCH");
}

/** 当前单业务入口；不能通过任意配置字段把新 ML 操作路由到旧订单账本或任意路径。 */
export function loadActiveBusiness(project: string): { businessId: "async-ml-inference"; ledgerRef: "artifacts/lab/ml-inference" } {
  const value: unknown = JSON.parse(readFileSync(checkedMlPath(realpathSync(project), "config/active-business.json"), "utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("ACTIVE_BUSINESS_INVALID");
  const v = value as Record<string, unknown>;
  if (v.schema_version !== 1 || v.business_id !== "async-ml-inference" || v.task_mode !== "RELEASE_VERIFICATION" ||
      v.source_policy_ref !== "config/ml-source-policy.json" || v.source_lock_ref !== "sut/ml-inference/upstream.lock.json" ||
      v.baseline_ref !== "sut/ml-inference/workspaces/baseline" || v.lab_ledger_ref !== "artifacts/lab/ml-inference" ||
      v.api_adapter !== "@egap/ml-inference-adapter") throw new Error("ACTIVE_BUSINESS_INVALID");
  return { businessId: "async-ml-inference", ledgerRef: "artifacts/lab/ml-inference" };
}
