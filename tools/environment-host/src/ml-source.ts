import { createHash } from "node:crypto";
import { readFileSync, realpathSync } from "node:fs";
import { checkedMlPath, prepareMlSourceWithPolicy } from "./ml-source-core.js";
import type { MlSourceOptions, MlSourcePolicy, MlSourceReceipt } from "./ml-source-core.js";
export type { MlSourceOptions, MlSourceReceipt } from "./ml-source-core.js";

/**
 * egap:lab prepare-source → 本入口 → ml-source-core → 新 ML 来源命名空间。
 * 固定策略摘要防止仅修改 JSON 就将不同来源认作同一基线；更换源码需显式审查并更新此摘要。
 * Git 身份是公开来源的完整性锚点，不冒充上游作者签名或企业生产背书。
 */
export function prepareMlBaselineSource(options: MlSourceOptions): MlSourceReceipt {
  const policyBytes = readFileSync(checkedMlPath(realpathSync(options.projectRoot), "config/ml-source-policy.json"));
  if (createHash("sha256").update(policyBytes).digest("hex") !== "f90035e2e1fd1855cc52f4bef33bece18d2b602e903e337fdafd6d52b8684689") throw new Error("ML_SOURCE_POLICY_DIGEST_MISMATCH");
  return prepareMlSourceWithPolicy(options, JSON.parse(policyBytes.toString("utf8")) as MlSourcePolicy);
}
