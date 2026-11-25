import { reviewPrompt, reviewProposalError, reviewPlanBasis } from "./review-profile.js";
import { assessReviewBasis, compilePlanDraft } from "./review-planning.js";
import { buildEvidenceResponse, evidenceProposalError, evidenceProfilePrompt, evidenceShapeHint } from "./evidence-profile.js";
import { randomUUID, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import type { PoolClient } from "pg";
import { parseReviewDecisionProposal, reviewDecisionProposalSchema, reviewRequestSchema, DecisionProposalError, decisionProposalSchema, parseDecisionProposal, type DecisionProposal, parseEvidenceReviewInput, parseEvidenceDecisionProposal, evidenceDecisionProposalSchema } from "@egap/contracts";
import { Store, type RunRow } from "./store.js";
import { contextRef, contextView, evidenceRefs, finalEligibility, groundedRequirements, modelBodySchema, modelShapeHint } from "./context.js";
import { invokeLocalModel, validateModelConfig } from "./model.js";
import { assessCheckQuality } from "./check-quality.js";
import { assessCheckBinding } from "./check-binding.js";
import { capabilityContract, capabilityContractDigest, validatePlanningAuthority, verifyCapabilitySource } from "./capability-authority.js";
import { canonical, digest, requireThat, RuntimeError, sha, validateBundle,
  type Json, type Limits, type ReleaseBundle, type RuntimeConfig, type RunState, type RuntimeProposal } from "./types.js";

interface CallRow {
  id: string; run_id: string; command_id: string; basis: number; status: string; context_ref: string;
  request: Json; request_text: string | null; proposal: RuntimeProposal | null; applied: boolean; deadline: Date | null;
}
interface OperationRow { id: string; proposal: RuntimeProposal; status: string; result: Json | null }

/**
 * Python LangGraph → HTTP RuntimePort → 本服务 → Store/PostgreSQL → 本地 ModelPort。
 * V0.1 只授权受控读取与计划。所有模型文字仍是提案，不进入业务证据或覆盖。
 * 以 run 行锁串行准入；独立 event_seq 和 decision_basis_revision 避免记账使提案自我过期。
 */
export class Runtime {
  readonly store: Store;
  readonly configDigest: string;
  readonly identities: Json;
  constructor(readonly config: RuntimeConfig) {
    validateModelConfig(config.model);
    verifyCapabilitySource();
    requireThat(config.model.seed === undefined || Number.isSafeInteger(config.model.seed) && config.model.seed >= 0 && config.model.seed <= 2147483647, "INVALID_MODEL_SEED", 400);
    this.store = new Store(config.databaseUrl);
    const modules = Object.fromEntries(["runtime.js", "store.js", "context.js", "model.js", "server.js", "types.js", "check-quality.js", "check-binding.js", "capability-authority.js",
      "evidence-profile.js", "review-profile.js", "review-planning.js", "../../contracts/dist/review-request.js", "../../contracts/dist/review-decision.js", "../../contracts/dist/evidence-review.js", "../../contracts/dist/verify.js"].map(path => [path, sha(readFileSync(new URL(path, import.meta.url), "utf8"))]));
    this.identities = { runtime_build_sha256: digest(modules), module_sha256: modules,
      planner_prompt_sha256: sha(config.plannerPrompt), action_schema_sha256: digest(decisionProposalSchema), evidence_action_schema_sha256: digest(evidenceDecisionProposalSchema),
      review_prompt_sha256: sha(reviewPrompt), review_action_schema_sha256: digest(reviewDecisionProposalSchema), review_request_schema_sha256: digest(reviewRequestSchema),
      evidence_prompt_sha256: sha(evidenceProfilePrompt), evidence_read_transport: config.evidenceReadPort ? "MCP_ARCHIVE" : "PINNED_MEMORY",
      evidence_dispatch_fence: Boolean(config.assertEvidenceDispatchAllowed),
      capability_contract_sha256: capabilityContractDigest,
      model_name: config.model.name, model_manifest_digest: config.model.digest };
    this.configDigest = digest({ identities: this.identities, model: config.model, prompt: config.plannerPrompt, schema: modelBodySchema(),
      wire_encoding: "PERSISTED_JSON_TEXT_V1_ACTION_BEFORE_PAYLOAD" });
  }
  async close(): Promise<void> { await this.store.close(); }
  /** 可信 operator / Workbench Worker 调用；HTTP 没有导入或扩大材料权限的入口。 */
  async acceptRun(input: unknown, token: string, limits: Limits = { model_calls: 6, reads: 4, no_progress: 2 }, id: string = randomUUID()): Promise<string> {
    const bundle = (input as {task_mode?:string})?.task_mode === "EVIDENCE_REVIEW" ? parseEvidenceReviewInput(input) : validateBundle(input);
    if (bundle.task_mode === "RELEASE_VERIFICATION") validatePlanningAuthority(bundle.planning_contracts);
    requireThat(/^[0-9a-f-]{36}$/.test(id) && token.length >= 32, "INVALID_RUN_IDENTITY", 400);
    const bounds = { model_calls: 12, reads: 8, no_progress: 3 };
    for (const key of ["model_calls", "reads", "no_progress"] as const)
      requireThat(Number.isInteger(limits[key]) && limits[key] >= 1 && limits[key] <= bounds[key], "INVALID_BUDGET", 400);
    const repairs = bundle.task_mode === "EVIDENCE_REVIEW" ? 0 : limits.plan_repairs ?? Math.min(1, limits.model_calls - 1);
    requireThat(Number.isInteger(repairs) && repairs >= 0 && repairs <= 2 && repairs < limits.model_calls, "INVALID_BUDGET", 400);
    limits = { ...limits, plan_repairs: repairs };
    const state: RunState = { run_id: id, status: "ACTIVE", candidate_epoch: 1, decision_basis_revision: 1,
      event_seq: 0, config_digest: this.configDigest, limits, model_calls_used: 0, reads_used: 0,
      no_progress: 0, hypotheses: [], read_receipts: [], latest_feedback: null, stop_reason: null, identities: this.identities };
    if (bundle.task_mode === "RELEASE_VERIFICATION" && bundle.schema_version === 2) state.review_cycle = { directions: [] };
    await this.store.transaction(async c => {
      if (bundle.task_mode === "RELEASE_VERIFICATION" && bundle.review_request) {
        const request = bundle.review_request;
        // 同一操作员命名空间下串行登记 revision，防止并发插入两个互相矛盾的任务依据。
        // 不增加第二执行账本，原包及其摘要仍由 runs 持久化。
        await c.query("SELECT pg_advisory_xact_lock(hashtextextended($1, 726))", [request.requester_ref + ":" + request.request_id]);
        const previous = await c.query<{ request: unknown; bundle_digest: string }>("SELECT bundle->'review_request' AS request, bundle_digest FROM verify_runtime.runs WHERE bundle->'review_request'->>'request_id'=$1 AND bundle->'review_request'->>'requester_ref'=$2 AND bundle->'review_request'->>'revision'=$3",
          [request.request_id, request.requester_ref, String(request.revision)]);
        requireThat(previous.rows.every(r => digest(r.request) === digest(request) && r.bundle_digest === digest(bundle)), "REVIEW_REQUEST_REVISION_CONFLICT");
        if (request.predecessor_run_id) {
          const parent = await c.query<{ request: { request_id: string; requester_ref: string; revision: number } }>("SELECT bundle->'review_request' AS request FROM verify_runtime.runs WHERE id=$1", [request.predecessor_run_id]);
          const old = parent.rows[0]?.request;
          requireThat(old?.request_id === request.request_id && old.requester_ref === request.requester_ref && old.revision < request.revision,
            "REVIEW_PREDECESSOR_MISMATCH");
        }
      }
      await c.query("INSERT INTO verify_runtime.runs(id,bundle,bundle_digest,state,token_hash) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
        [id, bundle, digest(bundle), state, sha(token)]);
      const row = await this.store.lock(c, id);
      requireThat(digest(row.bundle) === digest(bundle) && row.token_hash === sha(token) && row.state.config_digest === this.configDigest &&
        canonical(row.state.limits) === canonical(limits), "RUN_IDENTITY_CONFLICT");
    });
    return id;
  }
  async authorize(runId: string, token: string): Promise<void> {
    requireThat(/^[0-9a-f-]{36}$/.test(runId), "RUN_NOT_FOUND", 404);
    const rows = await this.store.pool.query<{ token_hash: string }>("SELECT token_hash FROM verify_runtime.runs WHERE id=$1", [runId]);
    const expected = rows.rows[0]?.token_hash;
    requireThat(expected && timingSafeEqual(Buffer.from(expected, "hex"), Buffer.from(sha(token), "hex")), "ACCESS_DENIED", 403);
  }
  private active(row: RunRow): void {
    requireThat(row.state.status === "ACTIVE", "RUN_NOT_ACTIVE");
    requireThat(row.state.config_digest === this.configDigest, "CONFIG_DRIFT");
  }
  private nextCommand(state: RunState): string { return `decision:${state.decision_basis_revision}:${state.model_calls_used}`; }
  /** UNKNOWN 无自动补发。只恢复还未派发的 READ；GET 查询不产生新模型调用。 */
  async projection(id: string, options: { dispatchReads?: boolean } = {}): Promise<Json> {
    await this.reconcile(id);
    if (options.dispatchReads !== false) await this.drainReads(id);
    return this.store.transaction(async c => {
      const { bundle, state } = await this.store.lock(c, id);
      const calls = await c.query<CallRow>("SELECT * FROM verify_runtime.calls WHERE run_id=$1 ORDER BY created_at,id", [id]);
      const ops = await c.query<OperationRow>("SELECT * FROM verify_runtime.operations WHERE run_id=$1 ORDER BY created_at,id", [id]);
      const pending = calls.rows.find(call => call.status === "COMPLETED" && !call.applied && call.basis === state.decision_basis_revision);
      const inFlight = calls.rows.find(call => ["RESERVED", "DISPATCHED"].includes(call.status));
      return { ...state, context_view_ref: contextRef(bundle, state),
        next_command_id: inFlight?.command_id ?? this.nextCommand(state),
        pending_decision: pending ? { model_call_id: pending.id, status: "COMPLETED", proposal: pending.proposal } : null,
        pending_model_call: inFlight ? { model_call_id: inFlight.id, status: inFlight.status } : null,
        pending_operation: ops.rows.filter(op => ["QUEUED", "DISPATCHED"].includes(op.status)).map(op => ({ operation_id: op.id, status: op.status, action_kind: op.proposal.action_kind }))[0] ?? null,
        task_identity: bundle.task_mode === "EVIDENCE_REVIEW" ? { task_mode: bundle.task_mode, scope: bundle.scope, candidate_sha: bundle.candidate_sha, historical_run_id: bundle.historical_run_id, request_id: bundle.request_id, request_spec_revision: bundle.request_spec_revision } : { baseline_sha: bundle.baseline_sha, candidate_sha: bundle.candidate_sha, catalog_commit: bundle.catalog_commit,
          scope: bundle.scope, input_provenance: bundle.input_provenance,
          ...(bundle.review_request ? { request_id: bundle.review_request.request_id, request_revision: bundle.review_request.revision,
            review_request_sha256: digest(bundle.review_request), predecessor_run_id: bundle.review_request.predecessor_run_id } : {}) },
        budgets: { model_calls: { limit: state.limits.model_calls, used: state.model_calls_used }, reads: { limit: state.limits.reads, used: state.reads_used },
          accounting_basis: "ADMITTED_RESERVATIONS_NOT_CONFIRMED_PROVIDER_COST",
          check_repair: { reserved_within_total: state.limits.plan_repairs ?? 0, used: state.review_repair?.attempts_used ?? state.check_repair?.attempts_used ?? 0 },
          profile: bundle.task_mode === "EVIDENCE_REVIEW" ? "EVIDENCE_REVIEW" : "READ_ONLY_DISCOVERY", pools: { CODING: 0, DISCOVERY: state.limits.model_calls - (state.limits.plan_repairs ?? 0),
            CHECK_REPAIR: state.limits.plan_repairs ?? 0, FINAL: 0, RECONCILIATION: "LOCAL_STATE_ONLY", CLEANUP: "NO_SUT_RESOURCES" } },
        model_calls: calls.rows.map(call => ({ model_call_id: call.id, status: call.status, basis: call.basis, applied: call.applied })),
        operations: ops.rows.map(op => ({ operation_id: op.id, status: op.status, action_kind: op.proposal.action_kind, result: op.result })),
        current_candidate_coverage: [], final_eligibility: finalEligibility(),
      };
    });
  }
  /** Worker 对 UNKNOWN/待入库结果只对账，不触发任何新模型或 MCP 调用。 */
  async reconcileProjection(id: string): Promise<Json> { return this.projection(id, { dispatchReads: false }); }
  private callProjection(call: CallRow): Json {
    return { status: call.status, model_call_id: call.id, ...(call.proposal ? { proposal: call.proposal } : {}) };
  }
  /**
   * 先原子预留预算/调用/outbox，再提交 DISPATCHED，最后发送请求。
   * 崩溃在发送前但 DISPATCHED 已登记也会保守 UNKNOWN；不声称 exactly-once。
   */
  async decide(id: string, command: { command_id: string; based_on_decision_revision: number }): Promise<Json> {
    requireThat(typeof command.command_id === "string" && command.command_id.length <= 120 && Number.isSafeInteger(command.based_on_decision_revision), "INVALID_DECISION_COMMAND", 400);
    await this.reconcile(id);
    await this.drainReads(id);
    const registered = await this.store.transaction(async c => {
      const row = await this.store.lock(c, id); const s = row.state;
      const existing = await c.query<CallRow>("SELECT * FROM verify_runtime.calls WHERE run_id=$1 AND command_id=$2", [id, command.command_id]);
      if (existing.rows[0]) {
        requireThat(existing.rows[0].basis === command.based_on_decision_revision, "COMMAND_ID_CONFLICT");
        return existing.rows[0];
      }
      this.active(row);
      requireThat(command.based_on_decision_revision === s.decision_basis_revision && command.command_id === this.nextCommand(s), "STALE_DECISION");
      const pending = await c.query("SELECT id FROM verify_runtime.calls WHERE run_id=$1 AND (status IN ('RESERVED','DISPATCHED') OR (status='COMPLETED' AND applied=false)) UNION ALL SELECT id FROM verify_runtime.operations WHERE run_id=$1 AND status IN ('QUEUED','DISPATCHED')", [id]);
      requireThat(pending.rowCount === 0, "DECISION_ALREADY_PENDING");
      if (s.model_calls_used >= s.limits.model_calls) {
        s.status = "STOPPED"; s.stop_reason = "DISCOVERY_BUDGET_EXHAUSTED";
        await this.store.event(c, s, "STOPPED", { reason: s.stop_reason }); await this.store.save(c, s);
        return null;
      }
      // 修正池只在登记了具体缺项之后开放；探索 READ 不能花掉预留额度。
      // 一次修正机会按模型调用计，故无效 JSON 或继续 READ 也不能无限延长修正。
      const repairRecord = s.review_repair ?? s.check_repair;
      const pendingRepair = repairRecord?.status === "PENDING" ? repairRecord : undefined;
      const discoveryLimit = s.limits.model_calls - (s.limits.plan_repairs ?? 0);
      const exhausted = pendingRepair ? pendingRepair.attempts_used >= pendingRepair.attempts_limit : (s.model_calls_used - (row.bundle.task_mode === "RELEASE_VERIFICATION" && row.bundle.schema_version === 2 ? s.review_repair?.attempts_used ?? 0 : 0)) >= discoveryLimit;
      if (exhausted) {
        s.status = "STOPPED"; s.stop_reason = pendingRepair ? "CHECK_REPAIR_LIMIT" : "DISCOVERY_BUDGET_EXHAUSTED";
        if (pendingRepair) pendingRepair.status = "EXHAUSTED";
        await this.store.event(c, s, "STOPPED", { reason: s.stop_reason }); await this.store.save(c, s);
        return null;
      }
      if (pendingRepair) pendingRepair.attempts_used += 1;
      s.model_calls_used += 1;
      const callId = randomUUID(); const ref = contextRef(row.bundle, s);
      const request = { model: this.config.model.name, stream: false, format: modelBodySchema(row.bundle, s),
        options: { temperature: 0, seed: this.config.model.seed ?? 19, num_ctx: 16384, num_predict: this.config.model.maxOutputTokens },
        keep_alive: "5m", messages: [
          { role: "system", content: (row.bundle.task_mode === "EVIDENCE_REVIEW" ? evidenceProfilePrompt + "\n" + evidenceShapeHint() : row.bundle.schema_version === 2 ? reviewPrompt : this.config.plannerPrompt + "\n" + modelShapeHint()) },
          { role: "user", content: JSON.stringify({ context_view: contextView(row.bundle, s),
            execution_limits: { model_calls_remaining_after_this: s.limits.model_calls - s.model_calls_used,
              discovery_calls_remaining_after_this: pendingRepair ? 0 : Math.max(0, discoveryLimit - s.model_calls_used + (row.bundle.task_mode === "RELEASE_VERIFICATION" && row.bundle.schema_version === 2 ? s.review_repair?.attempts_used ?? 0 : 0)),
              repair_calls_remaining_after_this: pendingRepair ? pendingRepair.attempts_limit - pendingRepair.attempts_used : Math.max(0, (s.limits.plan_repairs ?? 0) - (repairRecord?.attempts_used ?? 0)),
              decision_phase: pendingRepair ? "CHECK_REPAIR" : "DISCOVERY",
              reads_remaining: s.limits.reads - s.reads_used },
            model_identity: { name: this.config.model.name, digest: this.config.model.digest } }) },
        ] };
      const requestText = JSON.stringify(request);
      if (Buffer.byteLength(requestText) > 100_000) {
        // 规格可以合法但过大。不能事务回滚后永远保持 ACTIVE，让重启反复同错。
        // 尚未创建/派发调用，所以撤销本次预增记账，持久化阻塞和实际字节数。
        s.model_calls_used -= 1;
        if (pendingRepair) pendingRepair.attempts_used -= 1;
        s.status = "BLOCKED"; s.stop_reason = "CONTEXT_TOO_LARGE";
        await this.store.event(c, s, "MODEL_CONTEXT_REJECTED", { reason: s.stop_reason, request_bytes: Buffer.byteLength(requestText) });
        await this.store.save(c, s); return null;
      }
      // jsonb 会重排对象键。对 LLM/生成语法而言顺序影响动作选择：必须先生成
      // action_kind，再生成对应 payload。单独保存实际 wire 文本，恢复也发送原字节。
      const result = await c.query<CallRow>("INSERT INTO verify_runtime.calls(id,run_id,command_id,basis,status,context_ref,request,request_text) VALUES($1,$2,$3,$4,'RESERVED',$5,$6,$7) RETURNING *",
        [callId, id, command.command_id, s.decision_basis_revision, ref, request, requestText]);
      await c.query("INSERT INTO verify_runtime.outbox(id,run_id,kind,status) VALUES($1,$2,'MODEL','PENDING')", [callId, id]);
      await this.store.event(c, s, "MODEL_RESERVED", { model_call_id: callId, budget_pool: pendingRepair ? "CHECK_REPAIR" : "DISCOVERY", config_digest: this.configDigest,
        repair_root_proposal_id: pendingRepair?.root_proposal_id ?? null, repair_attempt: pendingRepair?.attempts_used ?? null,
        request_bytes_sha256: sha(requestText) });
      await this.store.save(c, s); return result.rows[0]!;
    });
    if (!registered) {
      const state = (await this.projection(id));
      return { status: "BLOCKED", reason: state.stop_reason };
    }
    if (registered.status !== "RESERVED") return this.callProjection(registered);
    const dispatch = await this.store.transaction(async c => {
      const row = await this.store.lock(c, id);
      const call = (await c.query<CallRow>("SELECT * FROM verify_runtime.calls WHERE id=$1", [registered.id])).rows[0]!;
      if (call.status !== "RESERVED") return false;
      if (row.state.status !== "ACTIVE" || row.state.config_digest !== this.configDigest || row.state.decision_basis_revision !== call.basis) {
        await c.query("UPDATE verify_runtime.calls SET status='CANCELLED' WHERE id=$1", [call.id]);
        await c.query("UPDATE verify_runtime.outbox SET status='CANCELLED' WHERE id=$1", [call.id]); return false;
      }
      await c.query("UPDATE verify_runtime.calls SET status='DISPATCHED',deadline=now()+$2*interval '1 millisecond' WHERE id=$1", [call.id, this.config.model.timeoutMs + 5000]);
      await c.query("UPDATE verify_runtime.outbox SET status='DISPATCHED' WHERE id=$1", [call.id]);
      await this.store.event(c, row.state, "MODEL_DISPATCHED", { model_call_id: call.id }); await this.store.save(c, row.state); return true;
    });
    if (!dispatch) return this.getCall(registered.id);
    // 工作台属于另一个数据库；授权检查在事务外进行，不能把跨库读伪装成同一原子事务。
    // 撤回与派发之间仍有真实竞态，返回端和工作台 inbox 会再次检查授权。
    if (!(await this.evidenceAuthorizationValid(id))) {
      await this.cancelEvidenceDispatch(id, registered.id); return this.getCall(registered.id);
    }
    let raw: string;
    try {
      requireThat(registered.request_text !== null, "MODEL_REQUEST_BYTES_UNAVAILABLE");
      raw = await invokeLocalModel(this.config.model, registered.request_text);
    }
    catch { await this.markUnknown(id, registered.id, "MODEL_EXECUTION_UNCERTAIN"); return this.getCall(registered.id); }
    if (!(await this.evidenceAuthorizationValid(id))) {
      await this.cancelEvidenceDispatch(id, registered.id, raw); return this.getCall(registered.id);
    }
    await this.store.transaction(async c => {
      const row = await this.store.lock(c, id); const s = row.state;
      let proposal: RuntimeProposal | null = null; let usage: Json | null = null;
      let validationIssues: readonly { keyword: string; path: string }[] = [];
      let status = "INVALID";
      try {
        const response = JSON.parse(raw) as Json;
        requireThat(response.model === this.config.model.name && response.done === true, "MODEL_RECEIPT_INVALID");
        usage = { prompt_tokens: Number.isSafeInteger(response.prompt_eval_count) ? response.prompt_eval_count : null,
          output_tokens: Number.isSafeInteger(response.eval_count) ? response.eval_count : null, cost: null, cost_basis: "LOCAL_NO_PROVIDER_BILL" };
        const body = JSON.parse((response.message as { content: string }).content) as Json;
        requireThat(!["schema_version", "proposal_id", "based_on_decision_revision", "candidate_epoch", "context_view_ref"].some(k => k in body), "MODEL_IDENTITY_OVERRIDE");
        const evidenceMode = row.bundle.task_mode === "EVIDENCE_REVIEW";
        const reviewMode = !evidenceMode && row.bundle.schema_version === 2;
        proposal = (evidenceMode ? parseEvidenceDecisionProposal : reviewMode ? parseReviewDecisionProposal : parseDecisionProposal)({ ...body, schema_version: evidenceMode ? 2 : reviewMode ? 3 : 1, proposal_id: randomUUID(),
          based_on_decision_revision: registered.basis, candidate_epoch: s.candidate_epoch, context_view_ref: registered.context_ref });
        status = "COMPLETED";
      } catch (error) {
        // 只回送校验器的字段位置与规则名；不把异常中的原文、schema params 或秘密复制到反馈。
        // 例如 uniqueItems 明确告诉模型消除重复引用，而不是一遍遍重试笼统 INVALID。
        if (error instanceof DecisionProposalError) validationIssues = error.issues;
      }
      await c.query("UPDATE verify_runtime.calls SET status=$2,raw_response=$3,proposal=$4,usage=$5,error_code=$6 WHERE id=$1",
        [registered.id, status, raw, proposal, usage, status === "INVALID" ? "MODEL_OUTPUT_INVALID" : null]);
      await c.query("UPDATE verify_runtime.outbox SET status='DONE' WHERE id=$1", [registered.id]);
      // 迟到响应只追加旧调用，不改变 STOP/CANCELLED/BLOCKED 的终态。
      if (status === "INVALID" && s.status === "ACTIVE") {
        this.feedback(s, { status: "INVALID", error_code: "MODEL_OUTPUT_INVALID", validation_issues: validationIssues,
          correction: "Follow the reported schema constraints. uniqueItems means remove duplicate references; type means use the required JSON type. Return one corrected decision body, without identity fields." }, false);
      }
      await this.store.event(c, s, "MODEL_RECEIPT", { model_call_id: registered.id, status, raw_sha256: sha(raw) });
      await this.store.save(c, s);
    });
    return this.getCall(registered.id);
  }
  private async evidenceAuthorizationValid(id: string): Promise<boolean> {
    if (!this.config.assertEvidenceDispatchAllowed) return true;
    const row = (await this.store.pool.query<{mode:string}>("SELECT bundle->>'task_mode' AS mode FROM verify_runtime.runs WHERE id=$1",[id])).rows[0];
    if (row?.mode !== "EVIDENCE_REVIEW") return true;
    try { await this.config.assertEvidenceDispatchAllowed(id); return true; } catch { return false; }
  }
  private async cancelEvidenceDispatch(id: string, callId: string, raw?: string): Promise<void> {
    await this.store.transaction(async c => {
      const {state} = await this.store.lock(c,id);
      // 已取得但失去授权的响应仍留在私有账本；不能因撤回擦掉真实文本和消耗。
      let usage: Json | null = null;
      if (raw) {try {const receipt=JSON.parse(raw) as Json; usage={prompt_tokens:receipt.prompt_eval_count??null,output_tokens:receipt.eval_count??null,cost:null,cost_basis:"LOCAL_NO_PROVIDER_BILL"};} catch { /* 原字节仍保留供对账。 */ }}
      await c.query("UPDATE verify_runtime.calls SET status='CANCELLED',error_code='EVIDENCE_AUTHORIZATION_REVOKED',raw_response=COALESCE($2,raw_response),usage=COALESCE($3,usage) WHERE id=$1 AND status='DISPATCHED'",[callId,raw??null,usage]);
      await c.query("UPDATE verify_runtime.outbox SET status='CANCELLED' WHERE id=$1",[callId]);
      if (state.status === "ACTIVE") {state.status="CANCELLED";state.stop_reason="EVIDENCE_AUTHORIZATION_REVOKED";state.decision_basis_revision+=1;}
      await this.store.event(c,state,"EVIDENCE_DISPATCH_CANCELLED",{model_call_id:callId}); await this.store.save(c,state);
    });
  }
  private async getCall(id: string): Promise<Json> {
    const row = (await this.store.pool.query<CallRow>("SELECT * FROM verify_runtime.calls WHERE id=$1", [id])).rows[0]!;
    return this.callProjection(row);
  }
  private feedback(state: RunState, result: Json, progress: boolean): void {
    state.latest_feedback = result; state.decision_basis_revision += 1;
    state.no_progress = progress ? 0 : state.no_progress + 1;
    const repair = state.review_repair ?? state.check_repair;
    // 第一次质量拒绝不被通用 no_progress 抢先终止；给出明确、有界的修正权利。
    // 最后一次机会被 READ/无效响应用完时也结束，不能重置计数再申请模型调用。
    if (repair?.status === "PENDING" && state.status === "ACTIVE") {
      if (repair.attempts_used >= repair.attempts_limit) {
        repair.status = "EXHAUSTED"; state.status = "STOPPED"; state.stop_reason = "CHECK_REPAIR_LIMIT";
      }
    } else if (state.no_progress >= state.limits.no_progress && state.status === "ACTIVE") {
      state.status = "STOPPED"; state.stop_reason = "NO_PROGRESS_LIMIT";
    }
  }
  private async markUnknown(id: string, callId: string, reason: string): Promise<void> {
    await this.store.transaction(async c => {
      const { state } = await this.store.lock(c, id);
      const changed = await c.query("UPDATE verify_runtime.calls SET status='UNKNOWN',error_code=$2 WHERE id=$1 AND status='DISPATCHED'", [callId, reason]);
      // reconciliation 读到过期后，回执可能先提交；已完成调用不能再被迟到扫描降级。
      if (changed.rowCount === 0) return;
      await c.query("UPDATE verify_runtime.outbox SET status='UNKNOWN' WHERE id=$1 AND status='DISPATCHED'", [callId]);
      if (state.status === "ACTIVE") { state.status = "BLOCKED"; state.stop_reason = reason; }
      await this.store.event(c, state, "MODEL_UNKNOWN", { model_call_id: callId, reason }); await this.store.save(c, state);
    });
  }
  private async reconcile(id: string): Promise<void> {
    const expired = await this.store.pool.query<{ id: string }>("SELECT id FROM verify_runtime.calls WHERE run_id=$1 AND status='DISPATCHED' AND deadline<now()", [id]);
    for (const call of expired.rows) await this.markUnknown(id, call.id, "MODEL_RECONCILIATION_UNAVAILABLE");
    await this.store.transaction(async c => {
      const {state} = await this.store.lock(c,id);
      const stale = await c.query<{id:string}>("UPDATE verify_runtime.operations SET status='UNKNOWN',result=$2 WHERE run_id=$1 AND status='DISPATCHED' AND deadline<now() RETURNING id",[id,{status:"UNKNOWN",error_code:"READ_RECONCILIATION_UNAVAILABLE"}]);
      for (const op of stale.rows) {
        await c.query("UPDATE verify_runtime.outbox SET status='UNKNOWN' WHERE id=$1",[op.id]);
        if (state.status === "ACTIVE") {state.status="BLOCKED";state.stop_reason="READ_RECONCILIATION_UNAVAILABLE";}
        await this.store.event(c,state,"READ_UNKNOWN",{operation_id:op.id});
      }
      if (stale.rowCount) await this.store.save(c,state);
    });
  }
  private semanticError(row: RunRow, p: RuntimeProposal): string | null {
    const s = row.state;
    if (row.bundle.task_mode === "EVIDENCE_REVIEW") {
      if (p.schema_version !== 2) return "PROFILE_PROPOSAL_MISMATCH";
      if (p.candidate_epoch !== s.candidate_epoch || p.based_on_decision_revision !== s.decision_basis_revision || p.context_view_ref !== contextRef(row.bundle, s)) return "STALE_DECISION";
      return evidenceProposalError(row.bundle, s, p);
    }
    if (p.schema_version !== (row.bundle.schema_version === 2 ? 3 : 1)) return "PROFILE_PROPOSAL_MISMATCH";
    const b = row.bundle;
    if (p.candidate_epoch !== s.candidate_epoch || p.based_on_decision_revision !== s.decision_basis_revision || p.context_view_ref !== contextRef(b, s)) return "STALE_DECISION";
    const evidence = new Set(evidenceRefs(b, s));
    if (p.requirement_refs.some(ref => !b.requirement_refs.includes(ref))) return "REQUIREMENT_OUT_OF_SCOPE";
    if (p.evidence_refs.some(ref => !evidence.has(ref)) || p.hypothesis_updates.some(h => h.evidence_refs.some(ref => !evidence.has(ref)))) return "EVIDENCE_NOT_OBSERVED";
    const hypotheses = new Set([...s.hypotheses.map(h => h.id), ...p.hypothesis_updates.map(h => h.id)]);
    if (p.hypothesis_refs.some(ref => !hypotheses.has(ref)) || new Set(p.hypothesis_updates.map(h => h.id)).size !== p.hypothesis_updates.length) return "HYPOTHESIS_REFERENCE_INVALID";
    if (p.action_kind === "READ") {
      const m = b.materials.find(m => m.material_ref === p.payload.material_ref);
      if (!m) return "MATERIAL_ACCESS_DENIED";
      if (p.payload.start_line > p.payload.end_line || p.payload.end_line - p.payload.start_line >= 200 || p.payload.end_line > m.content.split("\n").length) return "READ_RANGE_INVALID";
    }
    if (p.action_kind === "NOVEL_CHECK" && s.read_receipts.length === 0) return "CAPABILITY_GAP_GROUNDING_REQUIRED";
    if (p.action_kind === "CHECK_INTENT") {
      if (s.read_receipts.length === 0 || p.requirement_refs.length === 0 || p.evidence_refs.length === 0) return "CHECK_GROUNDING_REQUIRED";
      if (p.requirement_refs.some(ref => !groundedRequirements(b, s).includes(ref))) return "APPLICABLE_RULE_READ_REQUIRED";
      const caps = b.catalog.capabilities as Json[], scenarios = b.catalog.scenarios as Json[];
      if (!caps.some(c => c.id === p.payload.capability_ref) || !scenarios.some(c => c.id === p.payload.scenario_ref)) return "CAPABILITY_REFERENCE_INVALID";
    }
    if (p.schema_version === 3) return reviewProposalError(b, s, p, evidence, groundedRequirements(b, s));
    return null;
  }
  /** 模型提案、假设更新、预算与 READ outbox 原子登记；假设只能保留为模型判断。 */
  async submit(id: string, input: unknown): Promise<Json> {
    const version = (input as {schema_version?:number})?.schema_version;
    const p: RuntimeProposal = version === 2 ? parseEvidenceDecisionProposal(input) : version === 3 ? parseReviewDecisionProposal(input) : parseDecisionProposal(input);
    const admitted = await this.store.transaction(async c => {
      const row = await this.store.lock(c, id); const s = row.state;
      const old = (await c.query<OperationRow>("SELECT * FROM verify_runtime.operations WHERE run_id=$1 AND proposal_id=$2", [id, p.proposal_id])).rows[0];
      if (old) { requireThat(canonical(old.proposal) === canonical(p), "PROPOSAL_ID_CONFLICT"); return old.id; }
      this.active(row);
      const call = (await c.query<CallRow>("SELECT * FROM verify_runtime.calls WHERE run_id=$1 AND status='COMPLETED' AND applied=false AND proposal->>'proposal_id'=$2", [id, p.proposal_id])).rows[0];
      requireThat(call && canonical(call.proposal) === canonical(p), "PROPOSAL_NOT_FROM_REGISTERED_CALL");
      let error = this.semanticError(row, p);
      const opId = randomUUID();
      const key = p.action_kind === "READ" ? digest({ epoch: s.candidate_epoch, action: p.action_kind, payload: p.payload,
        ...(row.bundle.task_mode === "EVIDENCE_REVIEW" ? { material_sha256: row.bundle.materials.find(m => m.material_ref === p.payload.material_ref)?.sha256 ?? null, manifest_sha256: row.bundle.authorized_manifest.sha256 } : {}) }) : digest({ proposal_id: p.proposal_id });
      const cached = (await c.query<OperationRow>("SELECT * FROM verify_runtime.operations WHERE run_id=$1 AND operation_key=$2 AND status IN ('COMPLETED','REJECTED')", [id, key])).rows[0];
      if (cached && !error) {
        const updated = [...s.hypotheses];
        for (const h of (p.schema_version !== 2 ? p.hypothesis_updates : [])) {
          const index = updated.findIndex(old => old.id === h.id);
          if (index >= 0) updated[index] = structuredClone(h); else updated.push(structuredClone(h));
        }
        requireThat(updated.length <= 32, "HYPOTHESIS_LIMIT");
        s.hypotheses = updated;
        await c.query("UPDATE verify_runtime.calls SET applied=true WHERE id=$1", [call.id]);
        const reused = { status: "CACHE_REUSED", original_operation_id: cached.id, result: cached.result };
        // 新提案有自己的回执映射，传输重放可找回它；原材料仍只读取、计费一次。
        await c.query("INSERT INTO verify_runtime.operations(id,run_id,proposal_id,operation_key,proposal,status,result) VALUES($1,$2,$3,$4,$5,'COMPLETED',$6)",
          [opId, id, p.proposal_id, digest({ cached_proposal_id: p.proposal_id }), p, reused]);
        this.feedback(s, reused, false);
        await this.store.event(c, s, "READ_CACHE_REUSED", { operation_id: opId, original_operation_id: cached.id, proposal_id: p.proposal_id }); await this.store.save(c, s); return opId;
      }
      if (!error && p.action_kind === "READ" && s.reads_used >= s.limits.reads) error = "READ_BUDGET_EXHAUSTED";
      let status = "COMPLETED", result: Json | null;
      let reviewProgress = false;
      if (error) {
        status = "REJECTED"; result = { status, error_code: error, rejected_action: { action_kind: p.action_kind, payload: p.payload } };
        if (error === "READ_RANGE_INVALID" && p.action_kind === "READ") {
          const lines = row.bundle.materials.find(m => m.material_ref === p.payload.material_ref)!.content.split("\n").length;
          result.allowed_range = { start_line: 1, end_line: lines, max_span: 200 };
          result.correction = "READ counts lines in the selected material itself. Diff @@ hunk numbers are source coordinates, not READ coordinates. Choose a range inside allowed_range or a different material.";
        }
        if (error === "APPLICABLE_RULE_READ_REQUIRED" && row.bundle.task_mode === "RELEASE_VERIFICATION" && p.schema_version !== 2) {
          const releaseBundle = row.bundle;
          result.missing_rule_reads = (releaseBundle.catalog.requirements as Json[]).filter(r => p.requirement_refs.includes(String(r.id)) &&
            !groundedRequirements(releaseBundle, s).includes(String(r.id))).map(r => r.material_ref);
          result.correction = "Read the complete applicable rule excerpt before proposing assertions. A rule title or a baseline behavior is not the contract text.";
        }
        if (error === "CAPABILITY_GAP_GROUNDING_REQUIRED") result.correction = "Read a relevant rule or implementation material before claiming a capability gap. Inspect the registered planning contract; an unexecuted prerequisite is not a missing interface.";
        if (p.schema_version === 3) result.correction = "Inspect review_basis, allowed_actions and the exact plan reference. Resolve missing/conflicting requirements by READ or CLARIFY; a new observation or changed hypothesis requires a freshly compiled plan.";
      }
      else if (p.action_kind === "READ") { status = "QUEUED"; result = null; s.reads_used += 1; }
      else if (p.schema_version === 3 && row.bundle.task_mode === "RELEASE_VERIFICATION" && p.action_kind === "UPDATE_DIRECTION") {
        const cycle = s.review_cycle!;
        // 有新依据/实质假设变化才算进展；重复解释不会无限刷新 no_progress。
        reviewProgress = p.hypothesis_updates.some(h => digest(s.hypotheses.find(old => old.id === h.id) ?? null) !== digest(h));
        cycle.directions = [...cycle.directions, { proposal_id: p.proposal_id, focus: p.payload.focus, evidence_refs: p.evidence_refs }].slice(-8);
        result = { status: "DIRECTION_UPDATED", focus: p.payload.focus, hypothesis_status: "PROPOSED_NOT_VERIFIED",
          review_basis: assessReviewBasis(row.bundle), business_executed: false };
      }
      else if (p.schema_version === 3 && row.bundle.task_mode === "RELEASE_VERIFICATION" && p.action_kind === "PLAN_DRAFT") {
        const compilation = compilePlanDraft(row.bundle, p, capabilityContract);
        const previous = s.review_cycle!.draft;
        const nextHypotheses = [...s.hypotheses.filter(h => !p.hypothesis_updates.some(next => next.id === h.id)), ...p.hypothesis_updates];
        const basis = reviewPlanBasis(row.bundle, { ...s, hypotheses: nextHypotheses });
        const planDigest = digest({ basis, draft: p.payload.draft, compiled: compilation.proposal?.payload ?? null });
        const record = { ref: "plan:" + opId, digest: planDigest, basis_digest: basis, status: compilation.status,
          proposal: structuredClone(p), compiled: compilation.proposal, issues: compilation.issues };
        s.review_cycle!.draft = record;
        reviewProgress = previous?.digest !== planDigest;
        if (compilation.status === "NEEDS_REVISION") {
          const previousRepair = s.review_repair;
          s.review_repair = { status: "PENDING", root_proposal_id: previousRepair?.root_proposal_id ?? p.proposal_id,
            latest_proposal_id: p.proposal_id, attempts_used: previousRepair?.attempts_used ?? 0,
            attempts_limit: previousRepair?.attempts_limit ?? s.limits.plan_repairs ?? 0 };
          status = "REJECTED";
          result = { status: "NEEDS_REVISION", draft_ref: record.ref, draft_digest: planDigest,
            issues: compilation.issues, quality: compilation.quality ?? null, binding: compilation.binding ?? null,
            correction: "Revise the draft fields listed in issues. Inputs, control and assertions are your decisions; the compiler does not supply business answers." };
        } else {
          if (s.review_repair) { s.review_repair.status = "SATISFIED"; s.review_repair.latest_proposal_id = p.proposal_id; }
          result = { status: "BOUND_PLAN", draft_ref: record.ref, draft_digest: planDigest, compiled_check: compilation.proposal?.payload,
            semantic_correctness: "NOT_ESTABLISHED", execution_authorized: false, current_candidate_coverage: [],
            next_step: "MAIN_AGENT_REVIEW_REQUIRED" };
        }
      }
      else if (p.schema_version === 3 && row.bundle.task_mode === "RELEASE_VERIFICATION" && p.action_kind === "ACCEPT_PLAN") {
        const draft = s.review_cycle!.draft!;
        s.review_cycle!.accepted_plan_ref = draft.ref;
        result = { status: "PLAN_ACCEPTED_FOR_REVIEW", draft_ref: draft.ref, draft_digest: draft.digest,
          compiled_check: draft.compiled?.payload, semantic_correctness: "NOT_ESTABLISHED", experiment_executed: false,
          remaining_requirement_refs: row.bundle.requirement_refs.filter(ref => !draft.compiled?.requirement_refs.includes(ref)),
          final_eligibility: finalEligibility() };
        s.status = "PLANNED"; s.stop_reason = "SCOPED_PLAN_ACCEPTED";
        reviewProgress = true;
      }
      else if (p.action_kind === "PROPOSE_EVIDENCE_RESPONSE" && row.bundle.task_mode === "EVIDENCE_REVIEW") {
        result = { status: "PROPOSED", evidence_response: buildEvidenceResponse(row.bundle, s, p, opId), trusted_finding: false };
        s.status = "PLANNED"; s.stop_reason = "EVIDENCE_RESPONSE_PROPOSED";
      }
      else if (p.action_kind === "CHECK_INTENT" && row.bundle.task_mode === "RELEASE_VERIFICATION") {
        // 传输 Schema 允许旧的笼统 CHECK；业务规划完整性在此一次返回全部缺项。
        // Runtime 不替模型补预期答案，且结构完整与业务正确性/可派发性分别记账。
        const structure = assessCheckQuality(row.bundle, p);
        const binding = assessCheckBinding(row.bundle, p, capabilityContract);
        const quality = { ...structure, policy_version: "check-binding-v2",
          status: structure.issues.length || binding.issues.length ? "NEEDS_REVISION" : "STRUCTURALLY_COMPLETE",
          issues: [...structure.issues, ...binding.issues], binding_status: binding.status };
        const previous = s.check_repair;
        const lineage = { root_proposal_id: previous?.root_proposal_id ?? p.proposal_id,
          previous_proposal_id: previous?.latest_proposal_id ?? null,
          attempt: previous?.attempts_used ?? 0, limit: previous?.attempts_limit ?? s.limits.plan_repairs ?? 0 };
        if (quality.status === "NEEDS_REVISION") {
          error = "CHECK_SPEC_INCOMPLETE"; status = "REJECTED";
          s.check_repair = { status: "PENDING", root_proposal_id: lineage.root_proposal_id,
            latest_proposal_id: p.proposal_id, attempts_used: lineage.attempt, attempts_limit: lineage.limit,
            latest_proposal: structuredClone(p), quality: { ...quality } };
          result = { status: "NEEDS_REVISION", error_code: error, quality, repair: lineage,
            rejected_action: { action_kind: p.action_kind, payload: p.payload },
            correction: "Use the issue paths to revise check_spec. Specify a registered stage, concrete inputs, observations from registered fact sources, rule-backed expectations and a discriminating control. Repair requests consume the reserved bounded model-call allowance. Structural completion does not establish correctness or permit execution." };
        } else {
          if (previous) {
            previous.status = "SATISFIED"; previous.latest_proposal_id = p.proposal_id;
            previous.latest_proposal = structuredClone(p); previous.quality = { ...quality };
          }
          result = { status: "PROPOSED_ONLY", check_intent: p.payload, requirement_refs: p.requirement_refs,
            quality, binding, repair: lineage,
            gaps: ["CAPABILITY_NOT_DISPATCH_ENABLED", "NO_CURRENT_CANDIDATE_CHECK_RESULT"] };
          s.status = "PLANNED"; s.stop_reason = "PLANNING_SLICE_COMPLETE";
        }
      } else if (p.action_kind === "REQUEST_FINAL") { result = { status: "NOT_ELIGIBLE", ...finalEligibility() }; }
      else if (p.action_kind === "STOP") { result = { status: "STOPPED", reason: p.payload.reason }; s.status = "STOPPED"; s.stop_reason = p.payload.reason; }
      else {
        result = { status: "PROPOSED_ONLY", action_kind: p.action_kind, proposal: p.payload,
          trusted_finding: false, external_message_sent: false, experiment_executed: false };
        if (p.action_kind === "CLARIFY" || p.action_kind === "NOVEL_CHECK") { s.status = "BLOCKED"; s.stop_reason = p.action_kind === "CLARIFY" ? "CLARIFICATION_REQUIRED" : "CAPABILITY_GAP"; }
      }
      // 错误动作不接受其假设更新；成功动作的更新仍标为 proposal，不能升级业务事实。
      if (!error) for (const h of (p.schema_version !== 2 ? p.hypothesis_updates : [])) { s.hypotheses = [...s.hypotheses.filter(old => old.id !== h.id), structuredClone(h)]; }
      if (!error && p.schema_version === 3) s.review_cycle!.last_frame = structuredClone(p.decision_frame);
      requireThat(s.hypotheses.length <= 32, "HYPOTHESIS_LIMIT");
      await c.query("INSERT INTO verify_runtime.operations(id,run_id,proposal_id,operation_key,proposal,status,result) VALUES($1,$2,$3,$4,$5,$6,$7)",
        [opId, id, p.proposal_id, error ? digest({ rejected: p.proposal_id }) : key, p, status, result]);
      await c.query("UPDATE verify_runtime.calls SET applied=true WHERE id=$1", [call.id]);
      if (status === "QUEUED") await c.query("INSERT INTO verify_runtime.outbox(id,run_id,kind,status) VALUES($1,$2,'READ','PENDING')", [opId, id]);
      else this.feedback(s, result!, !error && (reviewProgress || ["CHECK_INTENT", "PROPOSE_EVIDENCE_RESPONSE"].includes(p.action_kind)));
      await this.store.event(c, s, "PROPOSAL_REGISTERED", { proposal_id: p.proposal_id, operation_id: opId, status });
      await this.store.save(c, s); return opId;
    });
    await this.drainReads(id);
    return this.operation(id, admitted);
  }
  /**
   * READ 准入/预留已在 submit 的短事务完成。此处再以短事务领取，网络 MCP 在事务外执行，
   * 最后短事务重检撤销并登记回执。并发 projection 不会重复派发同一 operation。
   * 进程在派发后崩溃时保留 UNKNOWN 和预算，不把未知副作用当失败重试。
   */
  private async drainReads(id: string): Promise<void> {
    const reserved = await this.store.transaction(async c => {
      const row = await this.store.lock(c, id);
      const queued = await c.query<OperationRow>("SELECT * FROM verify_runtime.operations WHERE run_id=$1 AND status='QUEUED' ORDER BY created_at", [id]);
      const work: { op: OperationRow; bundle: RunRow["bundle"]; epoch: number }[] = [];
      for (const op of queued.rows) {
        if (op.proposal.action_kind !== "READ") continue;
        if (row.state.status !== "ACTIVE" || op.proposal.candidate_epoch !== row.state.candidate_epoch || row.state.config_digest !== this.configDigest) {
          await c.query("UPDATE verify_runtime.operations SET status='CANCELLED',result=$2 WHERE id=$1", [op.id, {status:"CANCELLED",error_code:"AUTHORIZATION_REVOKED"}]);
          await c.query("UPDATE verify_runtime.outbox SET status='CANCELLED' WHERE id=$1", [op.id]);
          continue;
        }
        await c.query("UPDATE verify_runtime.operations SET status='DISPATCHED',deadline=now()+interval '30 seconds' WHERE id=$1", [op.id]);
        await c.query("UPDATE verify_runtime.outbox SET status='DISPATCHED' WHERE id=$1", [op.id]);
        await this.store.event(c, row.state, "READ_DISPATCHED", {operation_id:op.id});
        work.push({ op, bundle: row.bundle, epoch: row.state.candidate_epoch });
      }
      await this.store.save(c, row.state); return work;
    });
    for (const {op,bundle,epoch} of reserved) {
      const p = op.proposal;
      if (p.action_kind !== "READ") continue;
      const m = bundle.materials.find(m => m.material_ref === p.payload.material_ref)!;
      let status = "COMPLETED";
      let result: Json;
      try {
        requireThat(await this.evidenceAuthorizationValid(id), "EVIDENCE_AUTHORIZATION_REVOKED");
        const content = m.content.split("\n").slice(p.payload.start_line - 1, p.payload.end_line).join("\n");
        requireThat(Buffer.byteLength(content) <= 16_384, "FRAGMENT_TOO_LARGE");
        if (bundle.task_mode === "EVIDENCE_REVIEW" && this.config.evidenceReadPort) {
          let timeout: ReturnType<typeof setTimeout> | undefined;
          // Adapter 也可设更短超时；Runtime 自己仍给外部 Promise 加上硬上界。
          // 超时不说明远端没收到请求，因此保留 UNKNOWN，绝不自动补发。
          const receipt = await Promise.race([
            this.config.evidenceReadPort.read({run_id:id,operation_id:op.id,
              material_ref:m.material_ref,sha256:m.sha256,start_line:p.payload.start_line,end_line:p.payload.end_line}),
            new Promise<never>((_resolve,reject)=>{timeout=setTimeout(()=>reject(new Error("MCP_READ_TIMEOUT")),25_000);}),
          ]).finally(()=>{if(timeout) clearTimeout(timeout);});
          requireThat(receipt.material_ref === m.material_ref && receipt.sha256 === m.sha256 && receipt.start_line === p.payload.start_line &&
            receipt.end_line === p.payload.end_line && receipt.content === content && receipt.segment_sha256 === sha(content), "MCP_READ_BINDING_INVALID");
        }
        result = {status,receipt_ref:"read:"+op.id,material_ref:m.material_ref,candidate_epoch:epoch,
          source_sha256:m.sha256,fragment_sha256:sha(content),start_line:p.payload.start_line,end_line:p.payload.end_line,content,
          evidence_kind:bundle.task_mode === "EVIDENCE_REVIEW" ? "PINNED_ARCHIVED_EVIDENCE" : "PINNED_MATERIAL_EXCERPT",
          transport:bundle.task_mode === "EVIDENCE_REVIEW" && this.config.evidenceReadPort ? "MCP_ARCHIVE" : "PINNED_MEMORY",
          ...( "provenance" in m ? {provenance:m.provenance} : {git_commit:m.git_commit}),proves_current_business_behavior:false};
        if (!(await this.evidenceAuthorizationValid(id))) {
          status="CANCELLED";result={status,error_code:"EVIDENCE_AUTHORIZATION_REVOKED",discarded_receipt:result};
        }
      } catch (error) {
        status = error instanceof RuntimeError ? (error.code === "EVIDENCE_AUTHORIZATION_REVOKED" ? "CANCELLED" : "REJECTED") : "UNKNOWN";
        result = {status,error_code:error instanceof RuntimeError ? error.code : "MCP_READ_UNCERTAIN"};
      }
      await this.store.transaction(async c => {
        const {state} = await this.store.lock(c,id);
        const current = (await c.query<{status:string}>("SELECT status FROM verify_runtime.operations WHERE id=$1",[op.id])).rows[0];
        // 已对账为 UNKNOWN 的迟到回执只保留审计事件，不重新激活已终止的 run。
        if (current?.status !== "DISPATCHED") { await this.store.event(c,state,"LATE_READ_RECEIPT",{operation_id:op.id,status,discarded_receipt:result}); await this.store.save(c,state); return; }
        if (state.status !== "ACTIVE" || state.candidate_epoch !== epoch || state.config_digest !== this.configDigest) {
          status = "CANCELLED"; result = {status,error_code:"AUTHORIZATION_REVOKED_AFTER_DISPATCH",discarded_receipt:result};
        } else if (status === "COMPLETED") state.read_receipts.push(result);
        else if (status === "CANCELLED") {state.status="CANCELLED";state.stop_reason="EVIDENCE_AUTHORIZATION_REVOKED";}
        else if (status === "UNKNOWN") {state.status="BLOCKED";state.stop_reason="MCP_READ_UNCERTAIN";}
        await c.query("UPDATE verify_runtime.operations SET status=$2,result=$3 WHERE id=$1",[op.id,status,result]);
        await c.query("UPDATE verify_runtime.outbox SET status=$2 WHERE id=$1",[op.id,status === "UNKNOWN" ? "UNKNOWN" : "DONE"]);
        this.feedback(state,result,status === "COMPLETED");
        await this.store.event(c,state,"READ_RECEIPT",{operation_id:op.id,status}); await this.store.save(c,state);
      });
    }
  }
  async operation(runId: string, id: string): Promise<Json> {
    const op = (await this.store.pool.query<OperationRow>("SELECT * FROM verify_runtime.operations WHERE run_id=$1 AND id=$2", [runId, id])).rows[0];
    requireThat(op, "OPERATION_NOT_FOUND", 404);
    return { operation_id: op.id, status: op.status, result: op.result };
  }
  async cancel(id: string): Promise<Json> {
    await this.store.transaction(async c => {
      const { state } = await this.store.lock(c, id);
      if (state.status === "ACTIVE") { state.status = "CANCELLED"; state.stop_reason = "OPERATOR_CANCELLED"; state.decision_basis_revision += 1;
        await this.store.event(c, state, "RUN_CANCELLED", {}); await this.store.save(c, state); }
    });
    return this.projection(id);
  }
}

export type { RuntimeConfig, EvidenceReadPort, Limits } from "./types.js";
