import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { LabLedger, LabLedgerError, labDigest } from "./lab-ledger.js";
import { readLabLockOwner, recoverAbandonedLabLock } from "./lab-lock.js";
import type { EnvironmentInstance } from "@egap/contracts";

const source = { source_commit: "a".repeat(40), build_revision: "test-build", business_revision: "unchanged-upstream", upstream_lock_sha256: "b".repeat(64) };
const actor = { actor: "test-process", authorization_scope: "temporary synthetic ledger tests only; no external resources" };
const digest = "c".repeat(64);
test("配置摘要拒绝稀疏数组、getter和额外数组属性，避免不稳定序列化", () => {
  assert.throws(() => labDigest(new Array(2)), isCode("NON_JSON_VALUE"));
  const getter: unknown[] = []; Object.defineProperty(getter, "0", { get() { throw new Error("must not execute"); }, enumerable: true });
  assert.throws(() => labDigest(getter), isCode("NON_JSON_VALUE"));
  assert.throws(() => labDigest(Object.assign([1], { extra: true })), isCode("NON_JSON_VALUE"));
  assert.equal(labDigest({ b: [1, 2], a: true }), labDigest({ a: true, b: [1, 2] }));
});
function root(t: { after(fn: () => void): void }): string { const p = mkdtempSync(join(tmpdir(), "egap-ledger-test-")); t.after(() => rmSync(p, { recursive: true, force: true })); return p; }
function setup(directory: string) {
  const ledger = LabLedger.open(directory); const identity = ledger.createExperiment(actor); ledger.bindSource(identity.experiment_id, source);
  const env = ledger.reserveEnvironment(identity.experiment_id, { source_commit: source.source_commit, build_revision: source.build_revision, environment_config_digest: digest, expires_at: new Date(Date.now() + 600_000).toISOString() });
  return { ledger, identity, env };
}
function isCode(code: string) { return (error: unknown) => error instanceof LabLedgerError && error.code === code; }

test("每个实验/物理尝试有独立身份，快照丢失后从日志恢复来源绑定", t => {
  const directory = root(t); let ledger = LabLedger.open(directory);
  const first = ledger.createExperiment(actor); const second = ledger.createExperiment(actor);
  const retry = ledger.newAttempt(first.experiment_id); ledger.bindSource(first.experiment_id, source);
  assert.notEqual(first.experiment_id, second.experiment_id); assert.notEqual(first.run_id, second.run_id);
  assert.equal(first.run_id, retry.run_id); assert.notEqual(first.check_run_id, retry.check_run_id); assert.notEqual(first.execution_attempt_id, retry.execution_attempt_id);
  const seq = ledger.snapshot().sequence; ledger.close();
  writeFileSync(join(directory, "snapshot.json"), "corrupt snapshot ignored");
  ledger = LabLedger.open(directory);
  try { assert.equal(ledger.snapshot().sequence, seq); assert.equal(ledger.snapshot().experiments[first.experiment_id]?.source?.source_commit, source.source_commit); }
  finally { ledger.close(); }
});

test("半写尾先隔离后截断，已提交事件保留，后续追加仍可恢复", t => {
  const directory = root(t); let ledger = LabLedger.open(directory); ledger.createExperiment(actor); ledger.close();
  const tail = '{"sequence":2,"payload":"interrupted'; appendFileSync(join(directory, "events.jsonl"), tail);
  ledger = LabLedger.open(directory);
  assert.equal(ledger.snapshot().sequence, 1); ledger.createExperiment(actor); ledger.close();
  const quarantine = readdirSync(directory).find(name => name.startsWith("incomplete-tail-")); assert.ok(quarantine);
  assert.equal(readFileSync(join(directory, quarantine), "utf8"), tail);
  ledger = LabLedger.open(directory); try { assert.equal(ledger.snapshot().sequence, 2); } finally { ledger.close(); }
});

test("完整坏hash或空白记录不能当半写尾丢弃，拒绝后锁被释放", t => {
  const directory = root(t); let ledger = LabLedger.open(directory); ledger.createExperiment(actor); ledger.close();
  const path = join(directory, "events.jsonl"); const original = readFileSync(path, "utf8");
  const record = JSON.parse(original) as Record<string, unknown>; record.hash = "0".repeat(64); writeFileSync(path, JSON.stringify(record) + "\n");
  assert.throws(() => LabLedger.open(directory), isCode("JOURNAL_HASH_MISMATCH"));
  writeFileSync(path, original + "\n"); assert.throws(() => LabLedger.open(directory));
  writeFileSync(path, original); ledger = LabLedger.open(directory); ledger.close();
});

test("即使重算hash，未知事件类型和非法状态转换仍被拒绝", t => {
  const directory = root(t); const ledger = LabLedger.open(directory); ledger.createExperiment(actor); ledger.close();
  const path = join(directory, "events.jsonl"); const event = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
  event.kind = "MODEL_GRANTS_PRODUCTION_PASS"; delete event.hash; event.hash = labDigest(event); writeFileSync(path, JSON.stringify(event) + "\n");
  assert.throws(() => LabLedger.open(directory), isCode("UNKNOWN_EVENT_KIND"));
});

test("事件提交后快照写入失败必须报告已提交，当前handle不得继续写", t => {
  const directory = root(t); let ledger = LabLedger.open(directory);
  rmSync(join(directory, "snapshot.json")); mkdirSync(join(directory, "snapshot.json"));
  assert.throws(() => ledger.createExperiment(actor), (error: unknown) => error instanceof LabLedgerError && error.code === "EVENT_COMMITTED_SNAPSHOT_FAILED" && error.sequence === 1);
  assert.throws(() => ledger.createExperiment(actor), isCode("LEDGER_CLOSED_OR_RECOVERY_REQUIRED")); ledger.close();
  rmSync(join(directory, "snapshot.json"), { recursive: true });
  ledger = LabLedger.open(directory); try { assert.equal(Object.keys(ledger.snapshot().experiments).length, 1); } finally { ledger.close(); }
});

test("真实子进程在日志fsync后/快照rename前被SIGKILL，恢复锁后不丢已提交身份", t => {
  const directory = root(t);
  // 精确在持久化边界杀进程，而不是用正常close冒充崩溃；只影响这个测试子进程。
  const code = `
    import fs from 'node:fs'; import {syncBuiltinESMExports} from 'node:module';
    const original = fs.renameSync;
    fs.renameSync = (from,to) => {
      if (String(to).endsWith('/snapshot.json') && fs.readFileSync(${JSON.stringify(join(directory, "events.jsonl"))},'utf8').includes('EXPERIMENT_CREATED')) process.kill(process.pid,'SIGKILL');
      return original(from,to);
    }; syncBuiltinESMExports();
    const {LabLedger} = await import(${JSON.stringify(new URL("./lab-ledger.js", import.meta.url).href)});
    const ledger = LabLedger.open(${JSON.stringify(directory)}); ledger.createExperiment(${JSON.stringify(actor)});
  `;
  const child = spawnSync(process.execPath, ["--input-type=module", "--eval", code], { encoding: "utf8", timeout: 10_000 });
  assert.equal(child.signal, "SIGKILL", child.stderr);
  assert.equal(JSON.parse(readFileSync(join(directory, "snapshot.json"), "utf8")).sequence, 0);
  const owner = readLabLockOwner(directory); recoverAbandonedLabLock(directory, owner.token);
  const ledger = LabLedger.open(directory); try { assert.equal(ledger.snapshot().sequence, 1); assert.equal(Object.keys(ledger.snapshot().experiments).length, 1); } finally { ledger.close(); }
});

test("未确认动作重开变UNKNOWN；新动作、普通完成和接管均先要求对账", t => {
  const directory = root(t); const first = setup(directory); let ledger = first.ledger;
  const { env } = first; assert.equal(env.status, "PLANNED"); assert.equal(env.manifest, null);
  const action = ledger.beginAction(env.environment_instance_id, env.lease_epoch, "PROVISION"); ledger.close();
  ledger = LabLedger.open(directory);
  try {
    assert.equal(ledger.snapshot().actions[action.action_id]?.status, "UNKNOWN");
    assert.throws(() => ledger.beginAction(env.environment_instance_id, env.lease_epoch, "PROVISION"), isCode("UNRESOLVED_ACTION_REQUIRES_RECONCILIATION"));
    assert.throws(() => ledger.completeAction(action.action_id, env.lease_epoch, "FAILED", digest), isCode("ACTION_RECONCILIATION_REQUIRED_OR_TERMINAL"));
    const receipt = { receipt_digest: digest, observed_resource_ids: [], barrier_checked: true as const };
    assert.throws(() => ledger.advanceEpoch(env.environment_instance_id, env.lease_epoch, receipt), isCode("UNRESOLVED_ACTION_REQUIRES_RECONCILIATION"));
    ledger.reconcileAction(action.action_id, env.lease_epoch, "NOT_DISPATCHED", receipt);
    const next = ledger.advanceEpoch(env.environment_instance_id, env.lease_epoch, receipt); assert.ok(next > env.lease_epoch);
    assert.throws(() => ledger.beginAction(env.environment_instance_id, env.lease_epoch, "PROVISION"), isCode("STALE_ENVIRONMENT_EPOCH"));
    const fresh = ledger.beginAction(env.environment_instance_id, next, "PROVISION");
    ledger.close(); ledger = LabLedger.open(directory);
    assert.equal(ledger.snapshot().actions[action.action_id]?.status, "NOT_DISPATCHED");
    assert.equal(ledger.snapshot().actions[fresh.action_id]?.status, "UNKNOWN");
    assert.equal(ledger.snapshot().environments[env.environment_instance_id]?.lease_epoch, next);
  } finally { ledger.close(); }
});

test("接管对账必须覆盖完整资源清单，epoch从账本分配且拒绝旧资源动作", t => {
  const { ledger, env } = setup(root(t));
  try {
    const resource = ledger.recordResource(env.environment_instance_id, env.lease_epoch, { kind: "container", provider_id: "synthetic-test-container" });
    const receipt = { receipt_digest: digest, observed_resource_ids: [], barrier_checked: true as const };
    assert.throws(() => ledger.advanceEpoch(env.environment_instance_id, env.lease_epoch, receipt), isCode("RESOURCE_RECONCILIATION_INCOMPLETE"));
    const next = ledger.advanceEpoch(env.environment_instance_id, env.lease_epoch, { ...receipt, observed_resource_ids: [resource.resource_id] });
    assert.equal(next, env.lease_epoch + 1);
    assert.throws(() => ledger.recordResource(env.environment_instance_id, env.lease_epoch, { kind: "volume", provider_id: "synthetic" }), isCode("STALE_ENVIRONMENT_EPOCH"));
    assert.throws(() => ledger.beginAction(env.environment_instance_id, next, "PROVISION"), isCode("EXISTING_RESOURCES_REQUIRE_INSPECTION"));
  } finally { ledger.close(); }
});

test("实际环境投影需归属和实测资源；快照不能被调用方改写", t => {
  const { ledger, env } = setup(root(t));
  try {
    const manifest: EnvironmentInstance = { environment_instance_id: env.environment_instance_id, authority_mode: "file-lab", evidence_scope: "calibration-only", environment_config_digest: env.environment_config_digest, environment_profile_digest: null, source_commit: env.source_commit, build_revision: env.build_revision, fixture_reset_id: env.fixture_reset_id, lease_epoch: env.lease_epoch, endpoints: { gateway: "http://127.0.0.1:12345" }, image_digests: { gateway: digest }, observed_topology_ref: "tests/synthetic-topology.json", schema_mapping_ref: null, resource_manifest_ref: "tests/synthetic-resources.json", expires_at: env.expires_at };
    assert.throws(() => ledger.registerEnvironment(manifest), isCode("RESOURCE_MANIFEST_REQUIRED"));
    ledger.recordResource(env.environment_instance_id, env.lease_epoch, { kind: "container", provider_id: "synthetic-only" });
    assert.throws(() => ledger.registerEnvironment({ ...manifest, environment_config_digest: "d".repeat(64) }), isCode("ENVIRONMENT_IDENTITY_MISMATCH"));
    ledger.registerEnvironment(manifest);
    const copy = ledger.snapshot(); delete copy.environments[env.environment_instance_id];
    assert.equal(ledger.snapshot().environments[env.environment_instance_id]?.status, "REGISTERED");
  } finally { ledger.close(); }
});

test("过期环境禁止新工作，但仍允许有界检查/清理入口", async t => {
  const directory = root(t); const ledger = LabLedger.open(directory); const identity = ledger.createExperiment(actor); ledger.bindSource(identity.experiment_id, source);
  const env = ledger.reserveEnvironment(identity.experiment_id, { source_commit: source.source_commit, build_revision: source.build_revision, environment_config_digest: digest, expires_at: new Date(Date.now() + 100).toISOString() });
  await new Promise(r => setTimeout(r, 150));
  try { assert.throws(() => ledger.beginAction(env.environment_instance_id, env.lease_epoch, "PROVISION"), isCode("ENVIRONMENT_EXPIRED")); ledger.beginAction(env.environment_instance_id, env.lease_epoch, "DESTROY"); }
  finally { ledger.close(); }
});
