import { readFileSync, realpathSync } from "node:fs";
import { checkedSourcePath } from "./source-export-core.js";
import { GENAI_SOURCE_BINDING } from "./genai-source.js";
import type { SourceBinding } from "./lab-ledger.js";

/**
 * CLI打开账本后、任何尝试分配之前核对来源；防止复制ML/订单账本后继承旧资格。
 * 新建未绑定实验可继续来源准备；已绑定实验必须匹配完整的GenAI参考来源锁。
 * commit相同但锁内容/资格被替换仍拒绝，不能仅靠目录名称隔离不同业务。
 */
export function assertActiveSourceBindings(sources: Iterable<SourceBinding | null>): void {
  for (const source of sources) if (source !== null && (
    source.source_commit !== GENAI_SOURCE_BINDING.source_commit ||
    source.build_revision !== GENAI_SOURCE_BINDING.build_revision ||
    source.business_revision !== GENAI_SOURCE_BINDING.business_revision ||
    source.upstream_lock_sha256 !== GENAI_SOURCE_BINDING.upstream_lock_sha256
  )) throw new Error("ACTIVE_BUSINESS_SOURCE_MISMATCH");
}

/** 当前单业务入口；明确注册GenAI参考来源，不接受任意插件、路径或自称已运行的配置。 */
export function loadActiveBusiness(project: string): { businessId: "genai-support"; ledgerRef: "artifacts/lab/genai-support" } {
  const value: unknown = JSON.parse(readFileSync(checkedSourcePath(realpathSync(project), "config/active-business.json"), "utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("ACTIVE_BUSINESS_INVALID");
  const v = value as Record<string, unknown>;
  if (v.schema_version !== 1 || v.business_id !== "genai-support" || v.task_mode !== "RELEASE_VERIFICATION" ||
      v.source_policy_ref !== "config/genai-support-source-policy.json" || v.source_lock_ref !== "sut/genai-support/upstream.lock.json" ||
      v.reference_ref !== "sut/genai-support/workspaces/reference" || v.lab_ledger_ref !== "artifacts/lab/genai-support" ||
      v.api_adapter !== "@egap/genai-support-adapter" || v.source_role !== "NON_SAAS_DEMO_REFERENCE" ||
      v.runtime_qualification !== "NOT_RUN" || v.sut_implementation_status !== "NOT_IMPLEMENTED") throw new Error("ACTIVE_BUSINESS_INVALID");
  return { businessId: "genai-support", ledgerRef: "artifacts/lab/genai-support" };
}
