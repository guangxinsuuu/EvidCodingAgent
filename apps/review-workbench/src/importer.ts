import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { join, resolve } from "node:path";
import type { ImportCase, ImportEvidence, Json } from "./types.js";

/**
 * 首版只接收这份已审阅的历史实验报告，不把任意 JSON 当作可信业务证据。
 * 哈希用于证明“导入的是指定版本材料”，不是对原始实验的再次执行或独立认证。
 * 新实验应增加受审阅的适配器/材料版本，不能通过去掉哈希校验复用此入口。
 */
export const HISTORICAL_SOURCE = {
  directory: "reports/05-baseline/20261119-late-usage-execution",
  summary_sha256: "39c227fc9078817bb0c30e304450c736ea811059148b67aaf69c5ecc64671917",
  checks_sha256: "8b2a6c544ab45c242291058d88ab4917da716fb72069e8b457ecbfce66c07e90",
  candidate_sha: "45a0378585b8970fb2860897c9e49792e15a9df0",
  case_id: "review-egaplate20261119a-v1",
} as const;
const MAX_SOURCE_BYTES = 1_048_576;
const SCOPE = "MANUAL_FIXED_PLAN_INTEGRATION_ONLY";
const IMPORT_LIMITATIONS = [
  "HISTORICAL_REPORT_IMPORT_NOT_REEXECUTION",
  "PRIVATE_EVIDENCE_REFERENCES_NOT_REVERIFIED_BY_IMPORTER",
  "NO_BASELINE_DIFF_OR_PR_AVAILABLE",
  "LOCAL_EXPERIMENTAL_REVIEW_IDENTITIES_ONLY",
  "REVIEW_ACKNOWLEDGEMENT_IS_NOT_BUSINESS_OR_RELEASE_APPROVAL",
];
const digest = (value: string | Buffer): string => createHash("sha256").update(value).digest("hex");

/** 错误只输出稳定分类，避免 JSON 解析错误或文件异常暴露原文/私有绝对路径。 */
export class HistoricalImportError extends Error {
  constructor(readonly code: string) { super(code); }
}
const invalid = (): never => { throw new HistoricalImportError("HISTORICAL_REPORT_INVALID"); };
function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : invalid();
}
function list(value: unknown): unknown[] { return Array.isArray(value) ? value : invalid(); }
function text(value: unknown, max = 1000): string {
  return typeof value === "string" && value.length > 0 && value.length <= max ? value : invalid();
}
function number(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : invalid();
}
function flag(value: unknown): boolean { return typeof value === "boolean" ? value : invalid(); }
function strings(value: unknown): string[] { return list(value).map(item => text(item)); }

/** 显式复制允许展示的字段，不传播 source 中的凭据、客户标识、私有材料路径或未知扩展。 */
function phase(value: unknown): { [key: string]: Json } {
  const row = object(value);
  return {
    eligibility: text(row.eligibility), proposed_action: text(row.proposed_action),
    coverage_status: text(row.coverage_status), coverage_revision: number(row.coverage_revision),
    pending_count: number(row.pending_count), claim_interval_units: text(row.claim_interval_units),
    missing_evidence: strings(row.missing_evidence), window_seconds: number(row.window_seconds),
    snapshot_sha256: text(row.snapshot_sha256, 64), facts_sha256: text(row.facts_sha256, 64),
    source_revision: number(row.source_revision), gaps: strings(row.gaps),
    raw_draft_semantics: text(row.raw_draft_semantics),
    native_metric_units: list(row.native_metric_units).map(item => {
      const metric = object(item);
      return { code: text(metric.code), units: text(metric.units), events_count: number(metric.events_count) };
    }),
  };
}

function evidence(suffix: string, title: string, kind: string, content: { [key: string]: Json } | Json[], source: "summary" | "checks", limitations: string[]): ImportEvidence {
  const serialized = JSON.stringify(content, null, 2);
  return {
    id: `${HISTORICAL_SOURCE.case_id}-${suffix}`, title, kind, content: serialized,
    sha256: digest(serialized), source_ref: `${HISTORICAL_SOURCE.directory}/${source}.json`,
    source_sha256: source === "summary" ? HISTORICAL_SOURCE.summary_sha256 : HISTORICAL_SOURCE.checks_sha256,
    limitations,
  };
}

/**
 * CLI/startup → loadHistoricalCase → parseHistoricalCase → Store.importCase。
 * 导入仅生成固定版本 Review Bundle；不调用 Runtime.projection、模型或被测系统。
 * 页面得到的是报告摘录与来源哈希，机器结果和待人审结论继续分开保存。
 */
export function parseHistoricalCase(summaryBytes: Buffer, checksBytes: Buffer): ImportCase {
  if (summaryBytes.length > MAX_SOURCE_BYTES || checksBytes.length > MAX_SOURCE_BYTES) {
    throw new HistoricalImportError("HISTORICAL_SOURCE_TOO_LARGE");
  }
  if (digest(summaryBytes) !== HISTORICAL_SOURCE.summary_sha256 || digest(checksBytes) !== HISTORICAL_SOURCE.checks_sha256) {
    throw new HistoricalImportError("HISTORICAL_SOURCE_DIGEST_MISMATCH");
  }
  let summary: Record<string, unknown>;
  let checks: unknown[];
  try { summary = object(JSON.parse(summaryBytes.toString("utf8"))); checks = list(JSON.parse(checksBytes.toString("utf8"))); }
  catch { throw new HistoricalImportError("HISTORICAL_REPORT_INVALID"); }
  if (summary.schema_version !== 1 || summary.scope !== SCOPE || summary.candidate_sha !== HISTORICAL_SOURCE.candidate_sha || summary.experiment_id !== "egaplate20261119a") invalid();
  const authorization = object(summary.authorization);
  if (["agent_dispatch", "agent_autonomous_success", "protected_final", "release"].some(key => authorization[key] !== false)) invalid();
  const assertions = number(object(summary.business_assertions).PASS);
  const checkRows = checks.map(item => {
    const row = object(item);
    if (!["late_usage", "post_claim_usage"].includes(text(row.label)) || !["initial_verdict", "final_verdict", "transition"].includes(text(row.stage)) || row.status !== "PASS") invalid();
    return { label: text(row.label), stage: text(row.stage), name: text(row.name), status: "PASS" };
  });
  if (checkRows.length !== assertions || new Set(checkRows.map(row => `${row.label}/${row.stage}/${row.name}`)).size !== checkRows.length) invalid();
  const cases = list(summary.cases).map(object);
  if (cases.length !== 2 || new Set(cases.map(row => row.label)).size !== 2 || cases.some(row => row.tenant_id !== "tenant-b" || !["late_usage", "post_claim_usage"].includes(text(row.label)))) invalid();
  for (const row of cases) {
    const counts = object(row.checks_by_stage);
    for (const stage of ["initial_verdict", "final_verdict", "transition"]) {
      if (number(counts[stage]) !== checkRows.filter(check => check.label === row.label && check.stage === stage).length) invalid();
    }
  }
  const operations = object(summary.operations);
  const model = object(summary.model);
  const limitations = [...strings(summary.limitations), ...IMPORT_LIMITATIONS];
  const machineResult: { [key: string]: Json } = {
    status: text(summary.scoped_result), execution_mode: SCOPE,
    interpretation: "历史固定计划报告中限定范围的确定性检查通过；未形成新候选发布结论。",
    business_assertions_passed: assertions,
    operations_completed: number(object(operations.states).COMPLETED),
    historical_verified_evidence_refs_reported: number(operations.verified_evidence_refs),
    evidence_reference_note: "151 是原报告记录；本次导入只核验两个报告文件哈希，未重新访问全部私有证据。",
    sut_model: text(model.name), sut_model_calls: number(model.calls),
    verifier_autonomous_execution: false, protected_final: false, release_authorized: false,
    source_date: text(summary.date), source_candidate_sha: HISTORICAL_SOURCE.candidate_sha,
  };
  const materials: ImportEvidence[] = [
    evidence("overview", "历史实验范围与机器检查汇总", "HISTORICAL_REPORT_EXCERPT", machineResult, "summary", IMPORT_LIMITATIONS),
    ...cases.map(row => {
      const event = object(row.event);
      return evidence(text(row.label), row.label === "late_usage" ? "申诉前迟到用量：OPEN 到 SEALED" : "申诉后用量：总量与申诉区间分离", "BUSINESS_FACTS_REPORT_EXCERPT", {
        scenario: text(row.label), scope: text(row.scope), claimed_at: text(row.claimed_at),
        event: { units: text(event.units), occurred_at: text(event.occurred_at), native_occurred_at: text(event.native_occurred_at) },
        clock_note: "业务申诉/事件时钟为合成实验时间；native_occurred_at 是本地原生事件时间，不可混为真实生产时序。",
        before: phase(row.before), after: phase(row.after),
        old_snapshot_unchanged: flag(row.old_snapshot_unchanged), original_claim_unchanged: flag(row.original_claim_unchanged),
        event_dispatch_count: number(row.event_dispatch_count),
      }, "summary", ["REPORT_EXCERPT_NOT_ORIGINAL_OBSERVER_PAYLOAD", "SYNTHETIC_CONTRACT_PAYMENT_CLAIM_CLOCK", "LOCAL_CONTROLLED_EVENT_SET_ONLY"]);
    }),
    evidence("checks", "183 条限定范围检查的历史清单", "HISTORICAL_ASSERTION_LIST", checkRows, "checks", ["NOT_REEXECUTED_BY_IMPORTER", "CHECK_NAMES_ARE_NOT_COMPANY_APPROVED_CONTRACTS"]),
  ];
  const observation = object(summary.manual_review_observation);
  materials.push(evidence("wording", "原始草稿语义：仍需人工核对", "RAW_DRAFT_REVIEW_EXCERPT", {
    status: text(observation.status), excerpt: text(observation.excerpt), basis: text(observation.basis),
    phases: strings(observation.phases),
    probes: list(summary.review_required_probes).map(item => {
      const probe = object(item);
      return { scenario: text(probe.label), stage: text(probe.stage), code: text(probe.code), status: text(probe.status), basis: text(probe.basis), draft_sha256: text(probe.draft_sha256, 64) };
    }),
    completeness: "仅收录原报告已有的语义摘录与探针；原始完整模型请求/响应不在本材料中。",
  }, "summary", ["WORDING_NOT_FULLY_EVALUATED", "ACTUAL_MODEL_REQUEST_NOT_CAPTURED", "RAW_MODEL_OUTPUT_NOT_CAPTURED"]));
  materials.push(evidence("delivery", "最终私有备注：历史检查与缺失原文", "DELIVERY_CHECK_REPORT_EXCERPT", {
    checks: checkRows.filter(row => ["exact_private_note_delivery", "deterministic_note_lab_scope", "no_public_outgoing_message", "conversation_remains_open"].includes(row.name)),
    completeness: "原报告记载备注交付及确定性实验声明检查通过；这里未导入备注全文，不能据此核实每句最终文案。",
    open_question: "原始草稿中的资格暗示与最终备注中的实验声明是否让审查者正确理解？需要原文与语义核对，不能由交付成功推定。",
  }, "checks", ["FINAL_PRIVATE_NOTE_TEXT_NOT_IMPORTED", "DELIVERY_SUCCESS_DOES_NOT_PROVE_WORDING_CORRECT"]));
  return {
    id: HISTORICAL_SOURCE.case_id, tenant_id: "tenant-b", title: "客服退款争议 · 迟到用量与申诉区间历史审查",
    candidate_sha: HISTORICAL_SOURCE.candidate_sha, baseline_sha: null, execution_mode: SCOPE,
    source_ref: `${HISTORICAL_SOURCE.directory}/summary.json`, source_sha256: HISTORICAL_SOURCE.summary_sha256,
    machine_result: machineResult, limitations, evidence: materials,
    obligations: [
      { role: "BUSINESS", title: "核对申诉区间与退款资格口径", instruction: "结合迟到用量与申诉后用量两个样本，核对 OPEN 缺证不能判无用量、申诉区间与原生总量的区别。这里只确认实验材料口径，不构成真实公司合同批准或退款授权。" },
      { role: "SOURCE", title: "核对来源完整性、时钟与投递边界", instruction: "检查来源摘录、哈希、覆盖状态与 pending_count；识别合成业务时钟和原生事件时钟。缺少原始证据时提出补证，不能把原报告记录的 151 个引用当作本次独立复核。" },
      { role: "QA", title: "核对草稿与最终交付的语义差异", instruction: "原始草稿可能在缺证状态暗示不具资格；私有备注交付通过不证明文案正确。核对已有语义探针与交付检查，缺少完整文案时申请补证，不把历史限定检查当成发布通过。" },
    ],
  };
}

/** 固定路径、拒绝链接、限制大小；用户可选仓库根，但不能经参数读取任意私有文件。 */
async function readFixedSource(repositoryRoot: string, filename: "summary.json" | "checks.json"): Promise<Buffer> {
  try {
    const root = await realpath(resolve(repositoryRoot));
    let target = root;
    for (const part of [...HISTORICAL_SOURCE.directory.split("/"), filename]) {
      target = join(target, part);
      const info = await lstat(target);
      if (info.isSymbolicLink()) throw new HistoricalImportError("HISTORICAL_SOURCE_LINK_REJECTED");
    }
    const file = await open(target, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const info = await file.stat();
      if (!info.isFile()) throw new HistoricalImportError("HISTORICAL_SOURCE_NOT_FILE");
      if (info.size > MAX_SOURCE_BYTES) throw new HistoricalImportError("HISTORICAL_SOURCE_TOO_LARGE");
      // 读取有界缓冲区，避免文件在 stat 后增长导致无限分配。
      const bytes = Buffer.alloc(MAX_SOURCE_BYTES + 1);
      let length = 0;
      while (length < bytes.length) {
        const read = await file.read(bytes, length, bytes.length - length, length);
        if (read.bytesRead === 0) break;
        length += read.bytesRead;
      }
      if (length > MAX_SOURCE_BYTES) throw new HistoricalImportError("HISTORICAL_SOURCE_TOO_LARGE");
      return bytes.subarray(0, length);
    } finally { await file.close(); }
  } catch (error) {
    if (error instanceof HistoricalImportError) throw error;
    throw new HistoricalImportError("HISTORICAL_SOURCE_UNAVAILABLE");
  }
}

export async function loadHistoricalCase(repositoryRoot: string): Promise<ImportCase> {
  const [summary, checks] = await Promise.all([readFixedSource(repositoryRoot, "summary.json"), readFixedSource(repositoryRoot, "checks.json")]);
  return parseHistoricalCase(summary, checks);
}
