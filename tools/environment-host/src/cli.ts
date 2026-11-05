import { existsSync } from "node:fs";
import { hostname } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { LabLedger, labDigest } from "./lab-ledger.js";
import { recoverAbandonedLabLock } from "./lab-lock.js";
import { prepareGenAiReferenceSource } from "./genai-source.js";
import { checkedSourcePath } from "./source-export-core.js";
import { assertActiveSourceBindings, loadActiveBusiness } from "./business-profile.js";

/**
 * pnpm egap:lab → 当前业务配置 → GenAI专属LabLedger → prepareGenAiReferenceSource。
 * 仅准备航空Demo参考来源；不执行上游Python/调用LLM/启动Docker或签发业务PASS。
 * 旧Eventuate和ML账本与来源保留原位，新业务不能沿用其实验ID、构建或资格。
 * source导出与日志绑定不是跨文件系统事务：导出成功后若绑定中断，
 * 用同一个--experiment重试会先核对完整副本，再完成一次来源绑定。
 */
async function main(): Promise<void> {
  const projectRoot = fileURLToPath(new URL("../../../", import.meta.url));
  const business = loadActiveBusiness(projectRoot);
  const ledgerRoot = checkedSourcePath(projectRoot, business.ledgerRef);
  const [command, ...rest] = process.argv.slice(2);
  const options = new Map<string, string>();
  if (rest.length % 2 !== 0) throw new Error("INVALID_ARGUMENTS");
  for (let i = 0; i < rest.length; i += 2) {
    const key = rest[i]!; const value = rest[i + 1]!;
    if (!value || options.has(key)) throw new Error("INVALID_ARGUMENTS");
    options.set(key, value);
  }
  const allowed: Record<string, string[]> = { "prepare-source": ["--experiment", "--reference-repository"], status: [], "new-attempt": ["--experiment"], "recover-lock": ["--expected-token"] };
  if (!command || !Object.hasOwn(allowed, command) || [...options.keys()].some(k => !allowed[command]!.includes(k))) throw new Error("INVALID_ARGUMENTS");
  if (command === "recover-lock") {
    const token = options.get("--expected-token"); if (!token) throw new Error("EXPECTED_LOCK_TOKEN_REQUIRED");
    process.stdout.write(JSON.stringify(recoverAbandonedLabLock(ledgerRoot, token), null, 2) + "\n"); return;
  }
  if (command === "status" && !existsSync(ledgerRoot)) { process.stdout.write(JSON.stringify({ business_id: business.businessId, status: "NOT_INITIALIZED" }) + "\n"); return; }
  const ledger = LabLedger.open(ledgerRoot);
  try {
    assertActiveSourceBindings(Object.values(ledger.snapshot().experiments).map(experiment => experiment.source));
    if (command === "status") { process.stdout.write(JSON.stringify({ business_id: business.businessId, ledger: ledger.snapshot() }, null, 2) + "\n"); return; }
    if (command === "new-attempt") {
      const experimentId = options.get("--experiment"); if (!experimentId) throw new Error("EXPERIMENT_ID_REQUIRED");
      process.stdout.write(JSON.stringify(ledger.newAttempt(experimentId), null, 2) + "\n"); return;
    }
    const selected = options.get("--experiment");
    // 重试/重新核验仍是新的物理尝试：同一experiment/run下分配新check/attempt，
    // 只复用来源绑定和已完整落位的副本，不复用第一次执行的attempt身份。
    const identity = selected ? ledger.newAttempt(selected) : ledger.createExperiment({
      actor: `local-uid:${process.getuid?.() ?? "unknown"}@${hostname()}`,
      authorization_scope: "User-authorized GenAI Support business migration: freeze the fixed public airline demo as reference source and establish a separate GenAI laboratory ledger; no upstream execution, LLM calls, SaaS runtime qualification or publication.",
    });
    if (!identity) throw new Error("EXPERIMENT_NOT_REGISTERED");
    // 提前给出可恢复身份，导出失败也能据此检查已提交日志，避免盲建新实验。
    process.stderr.write(JSON.stringify({ phase: "SOURCE_PREPARATION", experiment_id: identity.experiment_id }) + "\n");
    const receipt = prepareGenAiReferenceSource({ projectRoot,
      referenceRepository: options.get("--reference-repository") ?? join(projectRoot, "tmp/genai-migration/upstream"),
    });
    const binding = { source_commit: receipt.source_commit, build_revision: receipt.build_revision,
      business_revision: receipt.business_revision, upstream_lock_sha256: receipt.upstream_lock_sha256 };
    const current = ledger.snapshot().experiments[identity.experiment_id]!.source;
    if (current === null) ledger.bindSource(identity.experiment_id, binding);
    else if (labDigest(current) !== labDigest(binding)) throw new Error("SOURCE_BINDING_MISMATCH");
    process.stdout.write(JSON.stringify({ status: "REFERENCE_SOURCE_READY", business_id: business.businessId, identity, source: receipt,
      ledger_sequence: ledger.snapshot().sequence, environment_status: "NOT_PROVISIONED",
      business_verification: "NOT_RUN" }, null, 2) + "\n");
  } finally { ledger.close(); }
}

// 错误只输出固定错误码；不把外部命令原始stderr或带路径/秘密的异常转给调用者。
main().catch((error: unknown) => {
  const candidate = error instanceof Error ? error.message : "";
  const code = /^[A-Z][A-Z0-9_]{2,100}$/.test(candidate) ? candidate : "LAB_OPERATION_FAILED";
  process.stderr.write(JSON.stringify({ status: "FAILED", code }) + "\n"); process.exitCode = 1;
});
