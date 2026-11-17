import { readFileSync } from "node:fs";
import { digest, sha, requireThat, type Json } from "./types.js";

/** 受保护 Runtime 自己加载接口；模型/bundle 不能登记新的可执行工具。 */
const contractFile = new URL("../../../protocols/verify-agent/capabilities/commercial-decision.v1.json", import.meta.url);
export const capabilityContract = JSON.parse(readFileSync(contractFile, "utf8")) as Json;
export const capabilityContractDigest = digest(capabilityContract);

/** 装配投影必须与本构建的真实契约逐字义相等；缺失仍可读材料，但 CHECK 会拒绝。 */
export function validatePlanningAuthority(contracts: Json[] | undefined): void {
  if (contracts === undefined) return;
  requireThat(Array.isArray(contracts) && contracts.length === 1 && digest(contracts[0]) === capabilityContractDigest,
    "PLANNING_CONTRACT_AUTHORITY_MISMATCH", 400);
}

export function verifyCapabilitySource(): void {
  const entrypoint = capabilityContract.entrypoint as { path: string };
  // 路径来自本机受保护常量文件，而不是候选或模型；候选代码只在隔离材料包中。
  const source = readFileSync(new URL("../../../" + entrypoint.path, import.meta.url), "utf8");
  requireThat(sha(source) === capabilityContract.source_sha256, "CAPABILITY_SOURCE_DRIFT");
}

/** 模型获得真实接口和默认输入，不获得fixture运行结果或独立评估答案。 */
export function planningContractView(contract: Json): Json {
  const fixture = contract.fixture as Json;
  const inputs = contract.inputs as Json[];
  const defaults = Object.fromEntries(inputs.map(input => {
    let value: unknown = fixture.context;
    for (const key of String(input.name).split(".")) value = (value as Json)[key];
    return [String(input.name), value];
  }));
  return { ...contract, fixture: { id: fixture.id, sha256: fixture.sha256,
    defaults, meaning: "SYNTHETIC_INPUT_ONLY_NOT_AN_EXPECTED_RESULT" } };
}
