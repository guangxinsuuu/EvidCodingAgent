import { createHash } from "node:crypto";
import { readFileSync, realpathSync } from "node:fs";
import { checkedSourcePath, prepareSourceWithPolicy } from "./source-export-core.js";
import type { SourceOptions, SourcePolicy } from "./source-export-core.js";

export type GenAiSourceOptions = SourceOptions;
export interface GenAiSourcePolicy extends SourcePolicy { business_id: "genai-support" }
export interface GenAiSourceReceipt {
  status: "CREATED" | "VERIFIED_EXISTING";
  source_commit: string;
  build_revision: "genai-reference-unbuilt-v1";
  business_revision: "unchanged-upstream";
  reference_path: string;
  upstream_lock_path: string;
  upstream_lock_sha256: string;
  file_count: number;
  reference_role: "NON_SAAS_DEMO_REFERENCE";
  qualification: "SOURCE_REFERENCE_ONLY_NOT_RUNTIME_VALIDATED";
}

/** 固定公开来源身份；更新任何一项需重新审查源码策略和来源锁，不能只改配置。 */
export const GENAI_SOURCE_BINDING = Object.freeze({
  source_commit: "bd7bfca0f5abf50529370814c3e7c88542011925",
  build_revision: "genai-reference-unbuilt-v1",
  business_revision: "unchanged-upstream",
  upstream_lock_sha256: "ff404d353d0c9b1067fd31a6cb9efc62124cf95af8568fc833d77a0a6bf3f27f",
});
const POLICY_SHA256 = "4a1a93a904a93df4ea11b1bed2824e59c3b862a936303c226467453ebac63011";

/**
 * egap:lab prepare-source → 本入口 → 共享完整性导出器 → 独立GenAI参考来源。
 * 审核过的策略摘要在代码内固定；不安装依赖、不启动航空Demo，也不调用任何模型。
 * 来源完整性只获得REFERENCE资格，不能据此宣称已有SaaS业务或健康运行证据。
 */
export function prepareGenAiReferenceSource(options: GenAiSourceOptions): GenAiSourceReceipt {
  const bytes = readFileSync(checkedSourcePath(realpathSync(options.projectRoot), "config/genai-support-source-policy.json"));
  if (createHash("sha256").update(bytes).digest("hex") !== POLICY_SHA256) throw new Error("GENAI_SOURCE_POLICY_DIGEST_MISMATCH");
  const receipt = prepareGenAiSourceWithPolicy(options, JSON.parse(bytes.toString("utf8")) as GenAiSourcePolicy);
  if (receipt.upstream_lock_sha256 !== GENAI_SOURCE_BINDING.upstream_lock_sha256) throw new Error("GENAI_SOURCE_LOCK_DIGEST_MISMATCH");
  return receipt;
}

/**
 * 供固定wrapper与微型真实Git测试使用的业务门面；公共核心只允许已知业务布局。
 * 输出使用reference_path而非baseline_path，防止上层把航空参考误认成SaaS健康基线。
 */
export function prepareGenAiSourceWithPolicy(options: GenAiSourceOptions, policy: GenAiSourcePolicy): GenAiSourceReceipt {
  if (policy.business_id !== "genai-support") throw new Error("GENAI_SOURCE_INVALID_POLICY");
  const receipt = prepareSourceWithPolicy(options, policy);
  return { status: receipt.status, source_commit: receipt.source_commit, build_revision: "genai-reference-unbuilt-v1",
    business_revision: receipt.business_revision, reference_path: receipt.source_path,
    upstream_lock_path: receipt.upstream_lock_path, upstream_lock_sha256: receipt.upstream_lock_sha256,
    file_count: receipt.file_count, reference_role: "NON_SAAS_DEMO_REFERENCE",
    qualification: "SOURCE_REFERENCE_ONLY_NOT_RUNTIME_VALIDATED" };
}
