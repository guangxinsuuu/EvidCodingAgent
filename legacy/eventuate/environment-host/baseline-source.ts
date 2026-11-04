import { prepareBaselineSourceWithPolicy } from "./baseline-source-core.js";
import type { BaselineSourceOptions, BaselineSourceReceipt } from "./baseline-source-core.js";
export type { BaselineSourceOptions, BaselineSourceReceipt } from "./baseline-source-core.js";

/**
 * CLI → 本入口 → 固定来源核验/导出 → sut 原子落位。
 * 只准备05.1源码和构建输入，不启动Gradle/Docker，不赋予业务健康资格。
 * 已有完整副本只校验后返回；脏副本、半完成状态或锁冲突均拒绝覆盖。
 */
export async function prepareBaselineSource(options: BaselineSourceOptions): Promise<BaselineSourceReceipt> {
  return prepareBaselineSourceWithPolicy(options, {
    commit: "894f2dbbe9aba2be6129748b6bbf5f30356deb07",
    fileCount: 190,
    distributionSha256: "6f74b601422d6d6fc4e1f9a1ab6522f642c2fdcbc15ae33ebd30ba3d7198e854",
    wrapperSha256: "14dfa961b6704bb3decdea06502781edaa796a82e6da41cd2e1962b14fbe21a3",
  });
}
