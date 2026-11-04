import { createHash, randomUUID } from "node:crypto";
import { closeSync, constants, existsSync, fsyncSync, ftruncateSync, lstatSync, mkdirSync, openSync, readFileSync, realpathSync, renameSync, unlinkSync, writeSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseEnvironmentInstance, parseLabIdentity } from "@egap/contracts";
import type { EnvironmentInstance, LabIdentity } from "@egap/contracts";
import { newCheckId, newRunId } from "@egap/domain";
import { acquireLabLock } from "./lab-lock.js";

export interface SourceBinding { source_commit: string; build_revision: string; business_revision: string; upstream_lock_sha256: string }
export interface LabExperiment {
  identity: LabIdentity; actor: string; authorization_scope: string; created_at: string;
  attempts: LabIdentity[]; source: SourceBinding | null;
}
export interface LabResource { resource_id: string; kind: "container" | "volume" | "network" | "process" | "directory"; provider_id: string }
export interface LabAction {
  action_id: string; environment_instance_id: string; lease_epoch: number;
  kind: "PROVISION" | "INSPECT" | "DESTROY" | "SMOKE";
  status: "PENDING" | "UNKNOWN" | "SUCCEEDED" | "FAILED" | "NOT_DISPATCHED";
  receipt_digest: string | null;
}
/** 分配记录与实际EnvironmentInstance分开：PLANNED不表示容器、fixture或端口已存在。 */
export interface EnvironmentReservation {
  environment_instance_id: string; experiment_id: string; fixture_reset_id: string;
  environment_config_digest: string; source_commit: string; build_revision: string;
  lease_epoch: number; expires_at: string; manifest: EnvironmentInstance | null;
  status: "PLANNED" | "REGISTERED" | "RETIRED"; resources: LabResource[];
}
export interface LabState {
  schema_version: 1; sequence: number; last_hash: string | null; last_epoch: number;
  experiments: Record<string, LabExperiment>; environments: Record<string, EnvironmentReservation>;
  actions: Record<string, LabAction>;
}
/**
 * 由可信provisioner独立只读检查资源和屏障后提供；摘要本身不是检查动作或工具授权。
 * 只读对账采集器尚未实现，采集时不能重发原动作或另建普通LabAction绕过UNKNOWN。
 */
export interface ReconciliationReceipt {
  receipt_digest: string;
  /** 已检查的账本资源ID，包含确认已不存在的资源；present/absent详情在摘要对应证据中。 */
  observed_resource_ids: string[];
  barrier_checked: true;
}
interface Event { schema_version: 1; sequence: number; previous_hash: string | null; event_id: string; at: string; kind: string; payload: unknown; hash: string }
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}";
const emptyState = (): LabState => ({ schema_version: 1, sequence: 0, last_hash: null, last_epoch: 0, experiments: {}, environments: {}, actions: {} });

export class LabLedgerError extends Error {
  constructor(readonly code: string, readonly sequence: number | null = null) { super(code); this.name = "LabLedgerError"; }
}
function demand(condition: unknown, code: string): asserts condition { if (!condition) throw new LabLedgerError(code); }
function obj(value: unknown, keys: string[]): Record<string, unknown> {
  demand(value !== null && typeof value === "object" && !Array.isArray(value), "INVALID_EVENT_PAYLOAD");
  const v = value as Record<string, unknown>;
  demand(Object.keys(v).length === keys.length && Object.keys(v).every(k => keys.includes(k)), "INVALID_EVENT_FIELDS");
  return v;
}
function text(value: unknown, max = 1024): string { demand(typeof value === "string" && value.length > 0 && value.length <= max && value.trim() === value && !/[\x00-\x1f]/.test(value), "INVALID_TEXT"); return value; }
function hex(value: unknown, length = 64): string { const v = text(value); demand(new RegExp(`^[0-9a-f]{${length}}$`).test(v), "INVALID_DIGEST"); return v; }
function id(value: unknown, prefix: string): string { const v = text(value); demand(new RegExp(`^${prefix}_${UUID}$`).test(v), "INVALID_ID"); return v; }
function integer(value: unknown): number { demand(typeof value === "number" && Number.isSafeInteger(value) && value >= 1, "INVALID_COUNTER"); return value; }
function instant(value: unknown): string { const v = text(value); demand(Number.isFinite(Date.parse(v)) && new Date(v).toISOString() === v, "INVALID_TIME"); return v; }
function choice<T extends string>(value: unknown, choices: readonly T[]): T { demand(typeof value === "string" && choices.includes(value as T), "INVALID_ENUM"); return value as T; }

/** 固定排序的JSON字节用于日志链与配置摘要；拒绝undefined/NaN等不稳定输入。 */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") { demand(Number.isFinite(value), "NON_JSON_VALUE"); return JSON.stringify(value); }
  if (Array.isArray(value)) {
    const keys = Reflect.ownKeys(value);
    demand(keys.length === value.length + 1 && keys.includes("length") &&
      Array.from({ length: value.length }, (_, i) => String(i)).every(k => {
        const descriptor = Object.getOwnPropertyDescriptor(value, k);
        return descriptor && descriptor.enumerable && "value" in descriptor;
      }), "NON_JSON_VALUE");
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  demand(typeof value === "object" && value !== null && [Object.prototype, null].includes(Object.getPrototypeOf(value) as object | null), "NON_JSON_VALUE");
  demand(Reflect.ownKeys(value).every(k => typeof k === "string") && Object.values(Object.getOwnPropertyDescriptors(value)).every(d => "value" in d && d.enumerable), "NON_JSON_VALUE");
  return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonicalJson((value as Record<string, unknown>)[k])}`).join(",")}}`;
}
export function labDigest(value: unknown): string { return createHash("sha256").update(canonicalJson(value)).digest("hex"); }
function sourceBinding(value: unknown): SourceBinding {
  const v = obj(value, ["source_commit", "build_revision", "business_revision", "upstream_lock_sha256"]);
  return { source_commit: hex(v.source_commit, 40), build_revision: text(v.build_revision, 128), business_revision: text(v.business_revision, 128), upstream_lock_sha256: hex(v.upstream_lock_sha256) };
}
function environment(state: LabState, value: unknown, epoch: unknown, at: string, allowExpired = false): EnvironmentReservation {
  const item = state.environments[id(value, "environment")];
  demand(item, "ENVIRONMENT_NOT_REGISTERED");
  demand(item.lease_epoch === integer(epoch), "STALE_ENVIRONMENT_EPOCH");
  demand(item.status !== "RETIRED", "ENVIRONMENT_RETIRED");
  demand(allowExpired || Date.parse(item.expires_at) > Date.parse(at), "ENVIRONMENT_EXPIRED");
  return item;
}
function unresolved(state: LabState, env: EnvironmentReservation): LabAction[] {
  return Object.values(state.actions).filter(a => a.environment_instance_id === env.environment_instance_id && ["PENDING", "UNKNOWN"].includes(a.status));
}
function reconciliation(value: unknown, env: EnvironmentReservation): ReconciliationReceipt {
  const r = obj(value, ["receipt_digest", "observed_resource_ids", "barrier_checked"]);
  demand(r.barrier_checked === true && Array.isArray(r.observed_resource_ids), "RECONCILIATION_REQUIRED");
  const ids = r.observed_resource_ids.map(x => id(x, "resource"));
  demand(new Set(ids).size === ids.length && canonicalJson([...ids].sort()) === canonicalJson(env.resources.map(x => x.resource_id).sort()), "RESOURCE_RECONCILIATION_INCOMPLETE");
  return { receipt_digest: hex(r.receipt_digest), observed_resource_ids: ids, barrier_checked: true };
}

/**
 * live提交与磁盘重放共用状态机。摘要正确仍须验证字段、归属和状态转换，
 * 避免手工拼造的日志或实现错误越过epoch/UNKNOWN门槛。
 */
function applyEvent(previous: LabState, event: Event): LabState {
  const state = structuredClone(previous);
  switch (event.kind) {
    case "EXPERIMENT_CREATED": {
      const p = obj(event.payload, ["identity", "actor", "authorization_scope"]);
      const identity = parseLabIdentity(p.identity);
      demand(!state.experiments[identity.experiment_id], "DUPLICATE_EXPERIMENT");
      demand(!Object.values(state.experiments).some(e => e.identity.run_id === identity.run_id), "DUPLICATE_RUN");
      demand(!Object.values(state.experiments).flatMap(e => e.attempts).some(a => a.check_run_id === identity.check_run_id || a.execution_attempt_id === identity.execution_attempt_id), "DUPLICATE_ATTEMPT");
      state.experiments[identity.experiment_id] = { identity, actor: text(p.actor, 200), authorization_scope: text(p.authorization_scope, 2000), created_at: event.at, attempts: [identity], source: null };
      break;
    }
    case "ATTEMPT_ALLOCATED": {
      const p = obj(event.payload, ["identity"]); const identity = parseLabIdentity(p.identity);
      const experiment = state.experiments[identity.experiment_id]; demand(experiment, "EXPERIMENT_NOT_REGISTERED");
      demand(identity.run_id === experiment.identity.run_id, "RUN_MISMATCH");
      demand(!Object.values(state.experiments).flatMap(e => e.attempts).some(a => a.check_run_id === identity.check_run_id || a.execution_attempt_id === identity.execution_attempt_id), "DUPLICATE_ATTEMPT");
      experiment.attempts.push(identity); break;
    }
    case "SOURCE_BOUND": {
      const p = obj(event.payload, ["experiment_id", "source"]); const experiment = state.experiments[id(p.experiment_id, "experiment")];
      demand(experiment && !experiment.source, "SOURCE_ALREADY_BOUND_OR_EXPERIMENT_MISSING");
      experiment.source = sourceBinding(p.source); break;
    }
    case "ENVIRONMENT_RESERVED": {
      const p = obj(event.payload, ["environment_instance_id", "experiment_id", "fixture_reset_id", "environment_config_digest", "source_commit", "build_revision", "lease_epoch", "expires_at"]);
      const experiment = state.experiments[id(p.experiment_id, "experiment")];
      demand(experiment?.source, "BOUND_SOURCE_REQUIRED");
      const envId = id(p.environment_instance_id, "environment"); const epoch = integer(p.lease_epoch);
      demand(!state.environments[envId] && epoch === state.last_epoch + 1, "INVALID_ENVIRONMENT_ALLOCATION");
      demand(p.source_commit === experiment.source.source_commit && p.build_revision === experiment.source.build_revision, "SOURCE_IDENTITY_MISMATCH");
      const expires = instant(p.expires_at); demand(Date.parse(expires) > Date.parse(event.at), "ENVIRONMENT_EXPIRED");
      state.environments[envId] = { environment_instance_id: envId, experiment_id: experiment.identity.experiment_id, fixture_reset_id: id(p.fixture_reset_id, "fixture"), environment_config_digest: hex(p.environment_config_digest), source_commit: hex(p.source_commit, 40), build_revision: text(p.build_revision, 128), lease_epoch: epoch, expires_at: expires, manifest: null, status: "PLANNED", resources: [] };
      state.last_epoch = epoch; break;
    }
    case "ACTION_REQUESTED": {
      const p = obj(event.payload, ["action_id", "environment_instance_id", "lease_epoch", "kind"]);
      const kind = choice(p.kind, ["PROVISION", "INSPECT", "DESTROY", "SMOKE"]);
      const env = environment(state, p.environment_instance_id, p.lease_epoch, event.at, kind === "INSPECT" || kind === "DESTROY");
      demand(unresolved(state, env).length === 0, "UNRESOLVED_ACTION_REQUIRES_RECONCILIATION");
      demand(kind !== "SMOKE" || env.manifest !== null, "OBSERVED_ENVIRONMENT_REQUIRED");
      demand(kind !== "PROVISION" || (env.manifest === null && env.resources.length === 0), "EXISTING_RESOURCES_REQUIRE_INSPECTION");
      const actionId = id(p.action_id, "action"); demand(!state.actions[actionId], "DUPLICATE_ACTION");
      state.actions[actionId] = { action_id: actionId, environment_instance_id: env.environment_instance_id, lease_epoch: env.lease_epoch, kind, status: "PENDING", receipt_digest: null }; break;
    }
    case "RESOURCE_OBSERVED": {
      const p = obj(event.payload, ["environment_instance_id", "lease_epoch", "resource"]);
      const env = environment(state, p.environment_instance_id, p.lease_epoch, event.at, true);
      const r = obj(p.resource, ["resource_id", "kind", "provider_id"]);
      const resource: LabResource = { resource_id: id(r.resource_id, "resource"), kind: choice(r.kind, ["container", "volume", "network", "process", "directory"]), provider_id: text(r.provider_id, 512) };
      demand(!Object.values(state.environments).flatMap(e => e.resources).some(x => x.resource_id === resource.resource_id || (x.kind === resource.kind && x.provider_id === resource.provider_id)), "RESOURCE_ALREADY_OWNED");
      env.resources.push(resource); break;
    }
    case "ENVIRONMENT_OBSERVED": {
      const p = obj(event.payload, ["manifest"]); const manifest = parseEnvironmentInstance(p.manifest);
      const env = environment(state, manifest.environment_instance_id, manifest.lease_epoch, event.at);
      demand(env.resources.length > 0, "RESOURCE_MANIFEST_REQUIRED");
      for (const key of ["environment_config_digest", "source_commit", "build_revision", "fixture_reset_id", "expires_at"] as const) demand(manifest[key] === env[key], "ENVIRONMENT_IDENTITY_MISMATCH");
      demand(Object.keys(manifest.endpoints).length > 0 && Object.keys(manifest.image_digests).length > 0, "OBSERVED_TOPOLOGY_REQUIRED");
      demand(env.manifest === null, "MANIFEST_ALREADY_REGISTERED");
      env.manifest = manifest; env.status = "REGISTERED"; break;
    }
    case "ACTION_COMPLETED":
    case "ACTION_RECONCILED": {
      const isReconcile = event.kind === "ACTION_RECONCILED";
      const p = obj(event.payload, ["action_id", "lease_epoch", "outcome", isReconcile ? "reconciliation" : "receipt_digest"]);
      const action = state.actions[id(p.action_id, "action")]; demand(action, "ACTION_NOT_REGISTERED");
      const env = environment(state, action.environment_instance_id, p.lease_epoch, event.at, true);
      demand(action.lease_epoch === env.lease_epoch, "STALE_ACTION_EPOCH");
      demand(isReconcile ? ["PENDING", "UNKNOWN"].includes(action.status) : action.status === "PENDING", "ACTION_RECONCILIATION_REQUIRED_OR_TERMINAL");
      const outcome = choice(p.outcome, isReconcile ? ["SUCCEEDED", "FAILED", "NOT_DISPATCHED"] : ["SUCCEEDED", "FAILED"]);
      demand(!(action.kind === "PROVISION" && outcome === "SUCCEEDED") || env.manifest !== null, "OBSERVED_ENVIRONMENT_REQUIRED");
      action.receipt_digest = isReconcile ? reconciliation(p.reconciliation, env).receipt_digest : hex(p.receipt_digest);
      action.status = outcome;
      if (action.kind === "DESTROY" && outcome === "SUCCEEDED") { env.status = "RETIRED"; env.manifest = null; }
      break;
    }
    case "EPOCH_ADVANCED": {
      const p = obj(event.payload, ["environment_instance_id", "lease_epoch", "new_epoch", "reconciliation"]);
      const env = environment(state, p.environment_instance_id, p.lease_epoch, event.at, true);
      demand(unresolved(state, env).length === 0, "UNRESOLVED_ACTION_REQUIRES_RECONCILIATION");
      reconciliation(p.reconciliation, env);
      const next = integer(p.new_epoch); demand(next === state.last_epoch + 1, "INVALID_EPOCH_ALLOCATION");
      // 接管不把旧端口快照当成新epoch观测；资源历史保留，provisioner必须重新登记。
      env.lease_epoch = next; env.manifest = null; env.status = "PLANNED"; state.last_epoch = next; break;
    }
    default: throw new LabLedgerError("UNKNOWN_EVENT_KIND");
  }
  state.sequence = event.sequence; state.last_hash = event.hash; return state;
}

function syncDirectory(directory: string): void { const fd = openSync(directory, constants.O_RDONLY); try { fsyncSync(fd); } finally { closeSync(fd); } }
function writeAll(fd: number, bytes: Buffer): void { let offset = 0; while (offset < bytes.length) { const count = writeSync(fd, bytes, offset, bytes.length - offset); demand(count > 0, "SHORT_WRITE"); offset += count; } }
/** 新临时文件fsync → rename → 目录fsync；快照损坏/缺失不会取代日志。 */
function atomicSnapshot(root: string, state: LabState): void {
  const temp = join(root, `.snapshot-${randomUUID()}.tmp`);
  const fd = openSync(temp, "wx", 0o600);
  try { writeAll(fd, Buffer.from(canonicalJson(state) + "\n")); fsyncSync(fd); } finally { closeSync(fd); }
  try { renameSync(temp, join(root, "snapshot.json")); syncDirectory(root); } finally { if (existsSync(temp)) unlinkSync(temp); }
}

/**
 * CLI/可信宿主 → open独占锁 → 日志重放 → 显式操作 → close。
 * 同步短事务使单进程中每次追加不可交错；锁跨进程排他，不提供分布式lease。
 * 普通动作派发前应beginAction持久化意图，调用后completeAction；崩溃后只能先对账。
 * 未决动作的只读观察走未来独立collector，再交reconcileAction记录，不重发原动作。
 */
export class LabLedger {
  readonly root: string;
  readonly #lock: ReturnType<typeof acquireLabLock>;
  readonly #fd: number;
  #state: LabState;
  #closed = false;
  #poisoned = false;

  private constructor(root: string, lock: ReturnType<typeof acquireLabLock>, fd: number, state: LabState) { this.root = root; this.#lock = lock; this.#fd = fd; this.#state = state; }

  /** 只在已确认持锁后修复半写尾；完整换行但损坏的记录一律阻断，绝不静默丢弃。 */
  static open(directory: string): LabLedger {
    const path = resolve(directory); mkdirSync(path, { recursive: true, mode: 0o700 });
    demand(!lstatSync(path).isSymbolicLink(), "LEDGER_SYMLINK_REJECTED");
    const root = realpathSync(path); const lock = acquireLabLock(root);
    let fd: number | undefined;
    try {
      fd = openSync(join(root, "events.jsonl"), constants.O_CREAT | constants.O_RDWR | constants.O_APPEND | constants.O_NOFOLLOW, 0o600);
      syncDirectory(root);
      demand(lstatSync(join(root, "events.jsonl")).size <= 32 * 1024 * 1024, "LAB_LEDGER_SIZE_LIMIT");
      const bytes = readFileSync(join(root, "events.jsonl"));
      const boundary = bytes.lastIndexOf(10) + 1;
      let state = emptyState();
      const complete = new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, boundary));
      for (const line of (complete === "" ? [] : complete.slice(0, -1).split("\n"))) {
        const raw: unknown = JSON.parse(line);
        const r = obj(raw, ["schema_version", "sequence", "previous_hash", "event_id", "at", "kind", "payload", "hash"]);
        demand(r.schema_version === 1 && integer(r.sequence) === state.sequence + 1 && r.previous_hash === state.last_hash, "JOURNAL_SEQUENCE_MISMATCH");
        const event: Event = { schema_version: 1, sequence: r.sequence as number, previous_hash: state.last_hash, event_id: id(r.event_id, "event"), at: instant(r.at), kind: text(r.kind, 100), payload: r.payload, hash: hex(r.hash) };
        const { hash, ...body } = event;
        demand(hash === labDigest(body), "JOURNAL_HASH_MISMATCH");
        state = applyEvent(state, event);
      }
      if (boundary < bytes.length) {
        const quarantine = join(root, `incomplete-tail-${randomUUID()}.bin`); const tailFd = openSync(quarantine, "wx", 0o600);
        try { writeAll(tailFd, bytes.subarray(boundary)); fsyncSync(tailFd); } finally { closeSync(tailFd); }
        syncDirectory(root); ftruncateSync(fd, boundary); fsyncSync(fd);
      }
      // 日志里的PENDING意图可能尚未派发，也可能已成功但回执丢失；两个方向都不能猜。
      for (const action of Object.values(state.actions)) if (action.status === "PENDING") action.status = "UNKNOWN";
      atomicSnapshot(root, state);
      return new LabLedger(root, lock, fd, state);
    } catch (error) { if (fd !== undefined) closeSync(fd); lock.release(); throw error; }
  }

  /** 返回独立副本，调用者修改返回值不会改动账本。 */
  snapshot(): LabState { this.#ready(); return structuredClone(this.#state); }
  #ready(): void { demand(!this.#closed && !this.#poisoned, "LEDGER_CLOSED_OR_RECOVERY_REQUIRED"); this.#lock.assertOwned(); }
  #commit(kind: string, payload: unknown): void {
    this.#ready();
    const body = { schema_version: 1 as const, sequence: this.#state.sequence + 1, previous_hash: this.#state.last_hash, event_id: `event_${randomUUID()}`, at: new Date().toISOString(), kind, payload };
    const event = { ...body, hash: labDigest(body) };
    const next = applyEvent(this.#state, event); // 验证失败必须发生在写入前。
    let committed = false;
    try {
      writeAll(this.#fd, Buffer.from(canonicalJson(event) + "\n")); fsyncSync(this.#fd); committed = true;
      this.#state = next; atomicSnapshot(this.root, next);
    } catch {
      this.#poisoned = true;
      // fsync后快照失败，事件已提交；fsync前失败则结果不确定。两者都禁止盲目重试。
      throw new LabLedgerError(committed ? "EVENT_COMMITTED_SNAPSHOT_FAILED" : "EVENT_COMMIT_UNKNOWN", event.sequence);
    }
  }
  /** 登记实际操作主体与本轮授权范围并分配首个检查/尝试；尚不表示这些检查已执行。 */
  createExperiment(input: { actor: string; authorization_scope: string }): LabIdentity {
    obj(input, ["actor", "authorization_scope"]);
    const identity = parseLabIdentity({ experiment_id: `experiment_${randomUUID()}`, run_id: newRunId(), check_run_id: newCheckId(), execution_attempt_id: `attempt_${randomUUID()}`, authority_mode: "file-lab", evidence_scope: "calibration-only" });
    this.#commit("EXPERIMENT_CREATED", { identity, ...input }); return identity;
  }
  /** 同一实验沿用run，新的物理尝试必须获得新check/attempt，不能覆写历史尝试。 */
  newAttempt(experimentId: string): LabIdentity {
    this.#ready(); const experiment = this.#state.experiments[experimentId]; demand(experiment, "EXPERIMENT_NOT_REGISTERED");
    const identity = parseLabIdentity({ ...experiment.identity, check_run_id: newCheckId(), execution_attempt_id: `attempt_${randomUUID()}` });
    this.#commit("ATTEMPT_ALLOCATED", { identity }); return identity;
  }
  /** 05.1导出/校验完成后绑定来源锁；一旦绑定不可原地换源码冒充同一实验。 */
  bindSource(experimentId: string, source: SourceBinding): void { this.#commit("SOURCE_BOUND", { experiment_id: experimentId, source }); }
  /** 从账本last_epoch分配，不允许调用方填一个“看起来正确”的epoch。 */
  reserveEnvironment(experimentId: string, input: { environment_config_digest: string; source_commit: string; build_revision: string; expires_at: string }): EnvironmentReservation {
    this.#ready(); const envId = `environment_${randomUUID()}`;
    this.#commit("ENVIRONMENT_RESERVED", { ...input, experiment_id: experimentId, environment_instance_id: envId, fixture_reset_id: `fixture_${randomUUID()}`, lease_epoch: this.#state.last_epoch + 1 });
    return structuredClone(this.#state.environments[envId]!);
  }
  /** 普通动作派发先经过此入口；未决动作阻断新动作，不能通过换action ID重发。 */
  beginAction(environmentId: string, epoch: number, kind: LabAction["kind"]): LabAction {
    const actionId = `action_${randomUUID()}`;
    this.#commit("ACTION_REQUESTED", { action_id: actionId, environment_instance_id: environmentId, lease_epoch: epoch, kind });
    return structuredClone(this.#state.actions[actionId]!);
  }
  /** 可信provisioner观察真实provider ID后登记；本方法本身不创建任何资源。 */
  recordResource(environmentId: string, epoch: number, input: Pick<LabResource, "kind" | "provider_id">): LabResource {
    const resource = { ...input, resource_id: `resource_${randomUUID()}` };
    this.#commit("RESOURCE_OBSERVED", { environment_instance_id: environmentId, lease_epoch: epoch, resource }); return resource;
  }
  /** 接受provisioner实测投影，核对分配身份；不自行发现端口或启动资源。 */
  registerEnvironment(manifest: EnvironmentInstance): void { this.#commit("ENVIRONMENT_OBSERVED", { manifest }); }
  /** 仅当前进程仍明确持有PENDING动作时接收结果；UNKNOWN必须走reconcileAction。 */
  completeAction(actionId: string, epoch: number, outcome: "SUCCEEDED" | "FAILED", receiptDigest: string): void { this.#commit("ACTION_COMPLETED", { action_id: actionId, lease_epoch: epoch, outcome, receipt_digest: receiptDigest }); }
  /** 对账后追加回执；资源清单不完整或epoch过期时拒绝，不能默认把未知结果算失败。 */
  reconcileAction(actionId: string, epoch: number, outcome: "SUCCEEDED" | "FAILED" | "NOT_DISPATCHED", receipt: ReconciliationReceipt): void { this.#commit("ACTION_RECONCILED", { action_id: actionId, lease_epoch: epoch, outcome, reconciliation: receipt }); }
  /** 已完成资源/屏障对账且没有未决动作才接管；不宣称能fence绕开此入口的Docker客户端。 */
  advanceEpoch(environmentId: string, epoch: number, receipt: ReconciliationReceipt): number {
    this.#ready(); const next = this.#state.last_epoch + 1;
    this.#commit("EPOCH_ADVANCED", { environment_instance_id: environmentId, lease_epoch: epoch, new_epoch: next, reconciliation: receipt }); return next;
  }
  close(): void { if (this.#closed) return; this.#closed = true; try { closeSync(this.#fd); } finally { this.#lock.release(); } }
}
