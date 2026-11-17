import { randomUUID, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import type { PoolClient } from "pg";
import { DecisionProposalError, decisionProposalSchema, parseDecisionProposal, type DecisionProposal } from "@egap/contracts";
import { Store, type RunRow } from "./store.js";
import { contextRef, contextView, evidenceRefs, finalEligibility, groundedRequirements, modelBodySchema, modelShapeHint } from "./context.js";
import { invokeLocalModel, validateModelConfig } from "./model.js";
import { assessCheckQuality } from "./check-quality.js";
import { assessCheckBinding } from "./check-binding.js";
import { capabilityContract, capabilityContractDigest, validatePlanningAuthority, verifyCapabilitySource } from "./capability-authority.js";
import { canonical, digest, requireThat, RuntimeError, sha, validateBundle,
  type Json, type Limits, type ReleaseBundle, type RuntimeConfig, type RunState } from "./types.js";

interface CallRow {
  id: string; run_id: string; command_id: string; basis: number; status: string; context_ref: string;
  request: Json; request_text: string | null; proposal: DecisionProposal | null; applied: boolean; deadline: Date | null;
}
interface OperationRow { id: string; proposal: DecisionProposal; status: string; result: Json | null }

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
      "../../contracts/dist/verify.js"].map(path => [path, sha(readFileSync(new URL(path, import.meta.url), "utf8"))]));
    this.identities = { runtime_build_sha256: digest(modules), module_sha256: modules,
      planner_prompt_sha256: sha(config.plannerPrompt), action_schema_sha256: digest(decisionProposalSchema),
      capability_contract_sha256: capabilityContractDigest,
      model_name: config.model.name, model_manifest_digest: config.model.digest };
    this.configDigest = digest({ identities: this.identities, model: config.model, prompt: config.plannerPrompt, schema: modelBodySchema(),
      wire_encoding: "PERSISTED_JSON_TEXT_V1_ACTION_BEFORE_PAYLOAD" });
  }
  async close(): Promise<void> { await this.store.close(); }
  /** 仅本地 operator CLI 调用；HTTP 没有导入/扩大材料权限的入口。 */
  async acceptRun(input: unknown, token: string, limits: Limits = { model_calls: 6, reads: 4, no_progress: 2 }, id: string = randomUUID()): Promise<string> {
    const bundle = validateBundle(input);
    validatePlanningAuthority(bundle.planning_contracts);
    requireThat(/^[0-9a-f-]{36}$/.test(id) && token.length >= 32, "INVALID_RUN_IDENTITY", 400);
    const bounds = { model_calls: 12, reads: 8, no_progress: 3 };
    for (const key of ["model_calls", "reads", "no_progress"] as const)
      requireThat(Number.isInteger(limits[key]) && limits[key] >= 1 && limits[key] <= bounds[key], "INVALID_BUDGET", 400);
    const repairs = limits.plan_repairs ?? Math.min(1, limits.model_calls - 1);
    requireThat(Number.isInteger(repairs) && repairs >= 0 && repairs <= 2 && repairs < limits.model_calls, "INVALID_BUDGET", 400);
    limits = { ...limits, plan_repairs: repairs };
    const state: RunState = { run_id: id, status: "ACTIVE", candidate_epoch: 1, decision_basis_revision: 1,
      event_seq: 0, config_digest: this.configDigest, limits, model_calls_used: 0, reads_used: 0,
      no_progress: 0, hypotheses: [], read_receipts: [], latest_feedback: null, stop_reason: null, identities: this.identities };
    await this.store.transaction(async c => {
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
  async projection(id: string): Promise<Json> {
    await this.reconcile(id);
    await this.drainReads(id);
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
        task_identity: { baseline_sha: bundle.baseline_sha, candidate_sha: bundle.candidate_sha, catalog_commit: bundle.catalog_commit,
          scope: bundle.scope, input_provenance: bundle.input_provenance },
        budgets: { model_calls: { limit: state.limits.model_calls, used: state.model_calls_used }, reads: { limit: state.limits.reads, used: state.reads_used },
          accounting_basis: "ADMITTED_RESERVATIONS_NOT_CONFIRMED_PROVIDER_COST",
          check_repair: { reserved_within_total: state.limits.plan_repairs ?? 0, used: state.check_repair?.attempts_used ?? 0 },
          profile: "READ_ONLY_DISCOVERY", pools: { CODING: 0, DISCOVERY: state.limits.model_calls - (state.limits.plan_repairs ?? 0),
            CHECK_REPAIR: state.limits.plan_repairs ?? 0, FINAL: 0, RECONCILIATION: "LOCAL_STATE_ONLY", CLEANUP: "NO_SUT_RESOURCES" } },
        model_calls: calls.rows.map(call => ({ model_call_id: call.id, status: call.status, basis: call.basis, applied: call.applied })),
        operations: ops.rows.map(op => ({ operation_id: op.id, status: op.status, action_kind: op.proposal.action_kind, result: op.result })),
        current_candidate_coverage: [], final_eligibility: finalEligibility(),
      };
    });
  }
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
      const pending = await c.query("SELECT id FROM verify_runtime.calls WHERE run_id=$1 AND (status IN ('RESERVED','DISPATCHED') OR (status='COMPLETED' AND applied=false)) UNION ALL SELECT id FROM verify_runtime.operations WHERE run_id=$1 AND status='QUEUED'", [id]);
      requireThat(pending.rowCount === 0, "DECISION_ALREADY_PENDING");
      if (s.model_calls_used >= s.limits.model_calls) {
        s.status = "STOPPED"; s.stop_reason = "DISCOVERY_BUDGET_EXHAUSTED";
        await this.store.event(c, s, "STOPPED", { reason: s.stop_reason }); await this.store.save(c, s);
        return null;
      }
      // 修正池只在登记了具体缺项之后开放；探索 READ 不能花掉预留额度。
      // 一次修正机会按模型调用计，故无效 JSON 或继续 READ 也不能无限延长修正。
      const pendingRepair = s.check_repair?.status === "PENDING" ? s.check_repair : undefined;
      const discoveryLimit = s.limits.model_calls - (s.limits.plan_repairs ?? 0);
      const exhausted = pendingRepair ? pendingRepair.attempts_used >= pendingRepair.attempts_limit : s.model_calls_used >= discoveryLimit;
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
          { role: "system", content: this.config.plannerPrompt + "\n" + modelShapeHint() },
          { role: "user", content: JSON.stringify({ context_view: contextView(row.bundle, s),
            execution_limits: { model_calls_remaining_after_this: s.limits.model_calls - s.model_calls_used,
              discovery_calls_remaining_after_this: pendingRepair ? 0 : Math.max(0, discoveryLimit - s.model_calls_used),
              repair_calls_remaining_after_this: pendingRepair ? pendingRepair.attempts_limit - pendingRepair.attempts_used : s.limits.plan_repairs ?? 0,
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
    let raw: string;
    try {
      requireThat(registered.request_text !== null, "MODEL_REQUEST_BYTES_UNAVAILABLE");
      raw = await invokeLocalModel(this.config.model, registered.request_text);
    }
    catch { await this.markUnknown(id, registered.id, "MODEL_EXECUTION_UNCERTAIN"); return this.getCall(registered.id); }
    await this.store.transaction(async c => {
      const row = await this.store.lock(c, id); const s = row.state;
      let proposal: DecisionProposal | null = null; let usage: Json | null = null;
      let validationIssues: readonly { keyword: string; path: string }[] = [];
      let status = "INVALID";
      try {
        const response = JSON.parse(raw) as Json;
        requireThat(response.model === this.config.model.name && response.done === true, "MODEL_RECEIPT_INVALID");
        usage = { prompt_tokens: Number.isSafeInteger(response.prompt_eval_count) ? response.prompt_eval_count : null,
          output_tokens: Number.isSafeInteger(response.eval_count) ? response.eval_count : null, cost: null, cost_basis: "LOCAL_NO_PROVIDER_BILL" };
        const body = JSON.parse((response.message as { content: string }).content) as Json;
        requireThat(!["schema_version", "proposal_id", "based_on_decision_revision", "candidate_epoch", "context_view_ref"].some(k => k in body), "MODEL_IDENTITY_OVERRIDE");
        proposal = parseDecisionProposal({ ...body, schema_version: 1, proposal_id: randomUUID(),
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
  private async getCall(id: string): Promise<Json> {
    const row = (await this.store.pool.query<CallRow>("SELECT * FROM verify_runtime.calls WHERE id=$1", [id])).rows[0]!;
    return this.callProjection(row);
  }
  private feedback(state: RunState, result: Json, progress: boolean): void {
    state.latest_feedback = result; state.decision_basis_revision += 1;
    state.no_progress = progress ? 0 : state.no_progress + 1;
    const repair = state.check_repair;
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
  }
  private semanticError(row: RunRow, p: DecisionProposal): string | null {
    const b = row.bundle, s = row.state;
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
    return null;
  }
  /** 模型提案、假设更新、预算与 READ outbox 原子登记；假设只能保留为模型判断。 */
  async submit(id: string, input: unknown): Promise<Json> {
    const p = parseDecisionProposal(input);
    const admitted = await this.store.transaction(async c => {
      const row = await this.store.lock(c, id); const s = row.state;
      const old = (await c.query<OperationRow>("SELECT * FROM verify_runtime.operations WHERE run_id=$1 AND proposal_id=$2", [id, p.proposal_id])).rows[0];
      if (old) { requireThat(canonical(old.proposal) === canonical(p), "PROPOSAL_ID_CONFLICT"); return old.id; }
      this.active(row);
      const call = (await c.query<CallRow>("SELECT * FROM verify_runtime.calls WHERE run_id=$1 AND status='COMPLETED' AND applied=false AND proposal->>'proposal_id'=$2", [id, p.proposal_id])).rows[0];
      requireThat(call && canonical(call.proposal) === canonical(p), "PROPOSAL_NOT_FROM_REGISTERED_CALL");
      let error = this.semanticError(row, p);
      const opId = randomUUID();
      const key = p.action_kind === "READ" ? digest({ epoch: s.candidate_epoch, action: p.action_kind, payload: p.payload }) : digest({ proposal_id: p.proposal_id });
      const cached = (await c.query<OperationRow>("SELECT * FROM verify_runtime.operations WHERE run_id=$1 AND operation_key=$2 AND status IN ('COMPLETED','REJECTED')", [id, key])).rows[0];
      if (cached && !error) {
        const updated = [...s.hypotheses];
        for (const h of p.hypothesis_updates) {
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
      if (error) {
        status = "REJECTED"; result = { status, error_code: error, rejected_action: { action_kind: p.action_kind, payload: p.payload } };
        if (error === "READ_RANGE_INVALID" && p.action_kind === "READ") {
          const lines = row.bundle.materials.find(m => m.material_ref === p.payload.material_ref)!.content.split("\n").length;
          result.allowed_range = { start_line: 1, end_line: lines, max_span: 200 };
          result.correction = "READ counts lines in the selected material itself. Diff @@ hunk numbers are source coordinates, not READ coordinates. Choose a range inside allowed_range or a different material.";
        }
        if (error === "APPLICABLE_RULE_READ_REQUIRED") {
          result.missing_rule_reads = (row.bundle.catalog.requirements as Json[]).filter(r => p.requirement_refs.includes(String(r.id)) &&
            !groundedRequirements(row.bundle, s).includes(String(r.id))).map(r => r.material_ref);
          result.correction = "Read the complete applicable rule excerpt before proposing assertions. A rule title or a baseline behavior is not the contract text.";
        }
        if (error === "CAPABILITY_GAP_GROUNDING_REQUIRED") result.correction = "Read a relevant rule or implementation material before claiming a capability gap. Inspect the registered planning contract; an unexecuted prerequisite is not a missing interface.";
      }
      else if (p.action_kind === "READ") { status = "QUEUED"; result = null; s.reads_used += 1; }
      else if (p.action_kind === "CHECK_INTENT") {
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
      if (!error) for (const h of p.hypothesis_updates) { s.hypotheses = [...s.hypotheses.filter(old => old.id !== h.id), structuredClone(h)]; }
      requireThat(s.hypotheses.length <= 32, "HYPOTHESIS_LIMIT");
      await c.query("INSERT INTO verify_runtime.operations(id,run_id,proposal_id,operation_key,proposal,status,result) VALUES($1,$2,$3,$4,$5,$6,$7)",
        [opId, id, p.proposal_id, error ? digest({ rejected: p.proposal_id }) : key, p, status, result]);
      await c.query("UPDATE verify_runtime.calls SET applied=true WHERE id=$1", [call.id]);
      if (status === "QUEUED") await c.query("INSERT INTO verify_runtime.outbox(id,run_id,kind,status) VALUES($1,$2,'READ','PENDING')", [opId, id]);
      else this.feedback(s, result!, !error && p.action_kind === "CHECK_INTENT");
      await this.store.event(c, s, "PROPOSAL_REGISTERED", { proposal_id: p.proposal_id, operation_id: opId, status });
      await this.store.save(c, s); return opId;
    });
    await this.drainReads(id);
    return this.operation(id, admitted);
  }
  private async drainReads(id: string): Promise<void> {
    await this.store.transaction(async c => {
      const row = await this.store.lock(c, id); const s = row.state;
      const queued = await c.query<OperationRow>("SELECT * FROM verify_runtime.operations WHERE run_id=$1 AND status='QUEUED' ORDER BY created_at", [id]);
      for (const op of queued.rows) {
        const p = op.proposal;
        if (p.action_kind !== "READ") continue;
        let status = "COMPLETED", result: Json;
        // 撤销在消费提案后仍能阻止未派发动作；每个后续步骤都以当前状态重检。
        if (s.status !== "ACTIVE" || p.candidate_epoch !== s.candidate_epoch || s.config_digest !== this.configDigest) {
          status = "CANCELLED"; result = { status, error_code: "AUTHORIZATION_REVOKED" };
        } else {
          const m = row.bundle.materials.find(m => m.material_ref === p.payload.material_ref)!;
          const content = m.content.split("\n").slice(p.payload.start_line - 1, p.payload.end_line).join("\n");
          if (Buffer.byteLength(content) > 16_384) { status = "REJECTED"; result = { status, error_code: "FRAGMENT_TOO_LARGE" }; }
          else {
            result = { status, receipt_ref: "read:" + op.id, material_ref: m.material_ref, git_commit: m.git_commit,
              candidate_epoch: s.candidate_epoch, source_sha256: m.sha256, fragment_sha256: sha(content),
              start_line: p.payload.start_line, end_line: p.payload.end_line, content,
              evidence_kind: "PINNED_MATERIAL_EXCERPT", proves_current_business_behavior: false };
            s.read_receipts.push(result);
          }
        }
        await c.query("UPDATE verify_runtime.operations SET status=$2,result=$3 WHERE id=$1", [op.id, status, result]);
        await c.query("UPDATE verify_runtime.outbox SET status='DONE' WHERE id=$1", [op.id]);
        this.feedback(s, result, status === "COMPLETED");
        await this.store.event(c, s, "READ_RECEIPT", { operation_id: op.id, status });
      }
      await this.store.save(c, s);
    });
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
