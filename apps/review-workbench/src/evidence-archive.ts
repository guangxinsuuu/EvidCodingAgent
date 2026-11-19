import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { join, resolve } from "node:path";
import type { EvidenceMaterial, EvidenceTarget } from "@egap/contracts";
import { HISTORICAL_SOURCE } from "./importer.js";
import type { WorkflowArchive } from "./workflow-types.js";

/** 受审阅的唯一归档入口；新增归档必须另行注册来源，不能接受浏览器提交任意路径。 */
export const ARCHIVE_SOURCE = {
  path: "artifacts/business-source/20261119-late-usage/execution-result.private.json",
  sha256: "e04eccfdf8f25852aa8b98c297e12bccda115f44f9c24bcb7762d167851ab843",
  historical_run_id: "egaplate20261119a",
} as const;
const sha = (value: string | Buffer): string => createHash("sha256").update(value).digest("hex");
const fail = (): never => { throw new Error("ARCHIVE_BINDING_INVALID"); };
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : fail();
}
function list(value: unknown): unknown[] { return Array.isArray(value) ? value : fail(); }
function text(value: unknown): string { return typeof value === "string" && value.length > 0 ? value : fail(); }
function integer(value: unknown): number { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : fail(); }

/**
 * main → loadEvidenceArchive → WorkflowStore.seedCase → requests 的授权材料清单。
 * 仅将归档中允许展示的业务字段抽取出来；source_ref 保留 JSON pointer 供复核。
 * 摘要校验只证明使用指定历史字节，不代表再次运行业务，也不产生模型原始响应。
 */
export function parseEvidenceArchive(bytes: Buffer): WorkflowArchive {
  if (bytes.length > 4_194_304 || sha(bytes) !== ARCHIVE_SOURCE.sha256) throw new Error("ARCHIVE_DIGEST_MISMATCH");
  const root = record(JSON.parse(bytes.toString("utf8")));
  if (root.experiment_id !== ARCHIVE_SOURCE.historical_run_id || record(root.candidate).candidate_sha !== HISTORICAL_SOURCE.candidate_sha) fail();
  const materials: EvidenceMaterial[] = [], targets: EvidenceTarget[] = [];
  const add = (target: EvidenceTarget, suffix: string, kind: string, body: Record<string, unknown>, pointer: string): EvidenceMaterial => {
    const content = JSON.stringify({ kind, ...target, ...body }, null, 2);
    const material: EvidenceMaterial = {
      material_ref: `archive:${target.historical_run_id}:${target.phase}:${suffix}`, kind,
      title: `${target.phase} / ${kind}`, sha256: sha(content), content,
      provenance: { ...target, candidate_sha: HISTORICAL_SOURCE.candidate_sha,
        source_ref: `${ARCHIVE_SOURCE.path}#${pointer}`, source_sha256: ARCHIVE_SOURCE.sha256 },
    };
    materials.push(material); return material;
  };
  const cases = list(root.cases);
  if (cases.length !== 2) fail();
  for (const [index, raw] of cases.entries()) {
    const scenario = record(raw), label = text(scenario.label);
    if (!["late_usage", "post_claim_usage"].includes(label)) fail();
    for (const stage of ["before", "after"] as const) {
      const phase = record(scenario[stage]), result = record(phase.result), job = record(result.job);
      const draft = record(phase.draft), delivery = record(result.delivery), scope = record(phase.scope);
      const target: EvidenceTarget = { tenant_id: text(job.tenant_id), historical_run_id: ARCHIVE_SOURCE.historical_run_id,
        job_id: text(job.job_id), ticket_id: text(job.ticket_id), ticket_revision: integer(job.ticket_revision), phase: `${label}.${stage}` };
      if (target.tenant_id !== "tenant-b" || target.ticket_revision < 1 || phase.candidate_sha !== HISTORICAL_SOURCE.candidate_sha ||
        phase.experiment_id !== target.historical_run_id || phase.requested_job_id !== target.job_id || scope.tenant_id !== target.tenant_id ||
        delivery.job_id !== target.job_id || delivery.delivery_state !== "COMPLETE" || draft.ticket_id !== target.ticket_id ||
        draft.ticket_revision !== target.ticket_revision) fail();
      const messageId = integer(delivery.external_message_id);
      const matches = list(record(phase.observations).messages).map(record).filter(m => m.id === messageId);
      if (matches.length !== 1 || matches[0]!.private !== true || matches[0]!.conversation_id !== scope.conversation_id) fail();
      const message = matches[0]!;
      const observed_at = text(phase.observed_at), pointer = `/cases/${index}/${stage}`;
      targets.push(target);
      add(target, "draft", "SAVED_PRODUCT_DRAFT", { customer_reply_draft: text(draft.customer_reply_draft), observed_at }, `${pointer}/draft`);
      const note = add(target, "note", "DELIVERED_PRIVATE_NOTE", { external_message_id: messageId, private: true,
        content: text(message.content), conversation_id: integer(message.conversation_id),
        message_created_at: integer(message.created_at), observed_at }, `${pointer}/observations/messages/id=${messageId}`);
      add(target, "delivery", "DELIVERY_RECEIPT", { external_message_id: messageId, delivery_state: "COMPLETE",
        delivered_note_ref: note.material_ref, delivered_note_sha256: note.sha256, observed_at }, `${pointer}/result/delivery`);
      // model_request/model_output 在固定归档中均为 null；绝不拿保存草稿冒充 provider 原文。
      if (phase.model_request !== null || phase.model_output !== null) throw new Error("ARCHIVE_PROFILE_REVIEW_REQUIRED");
    }
  }
  if (new Set(targets.map(t => t.phase)).size !== 4) fail();
  return { historical_run_id: ARCHIVE_SOURCE.historical_run_id, materials, targets,
    review_policy_version: "local-evidence-linkage-v1" };
}

/** 限定固定来源路径；拒绝各层符号链接，读取上限固定，不把私有文件全文写入日志。 */
export async function loadEvidenceArchive(repositoryRoot: string): Promise<WorkflowArchive> {
  let path = await realpath(resolve(repositoryRoot));
  for (const part of ARCHIVE_SOURCE.path.split("/")) {
    path = join(path, part);
    if ((await lstat(path)).isSymbolicLink()) throw new Error("ARCHIVE_LINK_REJECTED");
  }
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > 4_194_304) throw new Error("ARCHIVE_SIZE_REJECTED");
    const buffer = Buffer.alloc(4_194_305);
    let size = 0;
    while (size < buffer.length) {
      const read = await file.read(buffer, size, buffer.length - size, size);
      if (read.bytesRead === 0) break;
      size += read.bytesRead;
    }
    return parseEvidenceArchive(buffer.subarray(0, size));
  } finally { await file.close(); }
}
