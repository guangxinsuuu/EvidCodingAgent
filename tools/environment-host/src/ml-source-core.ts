import { checkedSourcePath, prepareSourceWithPolicy } from "./source-export-core.js";
import type { SourceFile, SourceOptions, SourcePolicy } from "./source-export-core.js";

export type MlSourceFile = SourceFile;
export type MlSourceOptions = SourceOptions;
export interface MlSourcePolicy extends SourcePolicy { business_id: "async-ml-inference" }
export interface MlSourceReceipt {
  status: "CREATED" | "VERIFIED_EXISTING"; source_commit: string; build_revision: "ml-upstream-unbuilt-v1";
  business_revision: "unchanged-upstream"; baseline_path: string; upstream_lock_path: string;
  upstream_lock_sha256: string; file_count: number; qualification: "SOURCE_ONLY_NOT_RUNTIME_VALIDATED";
}

/** 保留旧ML错误命名；公共完整性核心的重构不改变既有调用方语义。 */
function legacyCall<T>(work: () => T): T {
  try { return work(); }
  catch (error) {
    if (error instanceof Error && error.message.startsWith("SOURCE_")) throw new Error(`ML_${error.message}`);
    throw error;
  }
}
export function checkedMlPath(project: string, ref: string): string {
  return legacyCall(() => checkedSourcePath(project, ref));
}

/**
 * 历史ML固定策略入口 → 共享导出核心；仍输出原ML身份与锁字节，不能用于GenAI业务。
 * 测试可注入微型策略；正式入口ml-source.ts仍验证完整策略摘要。
 */
export function prepareMlSourceWithPolicy(options: MlSourceOptions, policy: MlSourcePolicy): MlSourceReceipt {
  if (policy.business_id !== "async-ml-inference") throw new Error("ML_SOURCE_INVALID_POLICY");
  const receipt = legacyCall(() => prepareSourceWithPolicy(options, policy));
  return { status: receipt.status, source_commit: receipt.source_commit, build_revision: "ml-upstream-unbuilt-v1",
    business_revision: receipt.business_revision, baseline_path: receipt.source_path,
    upstream_lock_path: receipt.upstream_lock_path, upstream_lock_sha256: receipt.upstream_lock_sha256,
    file_count: receipt.file_count, qualification: "SOURCE_ONLY_NOT_RUNTIME_VALIDATED" };
}
