import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { prepareMlSourceWithPolicy } from "./ml-source-core.js";
import type { MlSourcePolicy } from "./ml-source-core.js";
import { assertActiveSourceBindings, loadActiveBusiness } from "./business-profile.js";

/** 微型真实 Git fixture 验证导出边界；不执行或模拟 ML 推理。 */
function fixture(t: test.TestContext) {
  const root = mkdtempSync(join(tmpdir(), "egap-ml-source-")); t.after(() => rmSync(root, { recursive: true, force: true }));
  const project = join(root, "project"); const repo = join(root, "repo"); mkdirSync(project); mkdirSync(repo);
  const git = (...args: string[]): Buffer => execFileSync("git", ["-c", "core.hooksPath=/dev/null", "-C", repo, ...args], { stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1" } });
  git("init", "-q"); writeFileSync(join(repo, "LICENSE"), "fixture only\n"); writeFileSync(join(repo, "worker.py"), "raise RuntimeError('must never execute')\n");
  git("add", "."); git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture");
  const files = git("ls-tree", "-rz", "HEAD").toString().split("\0").filter(Boolean).map(row => {
    const [head, path] = row.split("\t"); const oid = head!.split(" ")[2]!; const bytes = git("cat-file", "blob", oid);
    return { path: path!, git_blob: oid, mode: 0o644, size: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") };
  });
  const policy: MlSourcePolicy = { schema_version: 1, business_id: "async-ml-inference", repository: "https://example.invalid/fixture", commit: git("rev-parse", "HEAD").toString().trim(), tree: git("rev-parse", "HEAD^{tree}").toString().trim(), file_count: files.length, files };
  return { project, repo, policy, baseline: join(project, "sut/ml-inference/workspaces/baseline") };
}

test("原子导出固定Git对象，既有ML副本离线可复核，旧订单来源保持原位", t => {
  const f = fixture(t); mkdirSync(join(f.project, "sut")); writeFileSync(join(f.project, "sut/upstream.lock.json"), "legacy\n");
  const first = prepareMlSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy);
  assert.equal(first.status, "CREATED"); assert.equal(first.qualification, "SOURCE_ONLY_NOT_RUNTIME_VALIDATED");
  const second = prepareMlSourceWithPolicy({ projectRoot: f.project }, f.policy);
  assert.equal(second.status, "VERIFIED_EXISTING"); assert.equal(first.upstream_lock_sha256, second.upstream_lock_sha256);
  assert.equal(readFileSync(join(f.project, "sut/upstream.lock.json"), "utf8"), "legacy\n");
  assert.equal(readdirSync(join(f.project, "sut")).some(p => p.startsWith(".ml-source")), false);
});
test("拒绝脏参考仓库，即使被篡改文件不影响Git对象", t => {
  const f = fixture(t); writeFileSync(join(f.repo, "worker.py"), "different\n");
  assert.throws(() => prepareMlSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy), /REFERENCE_NOT_CLEAN/);
});
test("来源SHA256不符时不提交半成品，且清理本调用拥有的stage与锁", t => {
  const f = fixture(t); f.policy.files[0]!.sha256 = "0".repeat(64);
  assert.throws(() => prepareMlSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy), /GIT_BLOB_MISMATCH/);
  assert.deepEqual(readdirSync(join(f.project, "sut")), []);
});
test("既有副本增删改或mode漂移均拒绝覆盖", t => {
  const f = fixture(t); prepareMlSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy);
  writeFileSync(join(f.baseline, "extra.py"), "x");
  assert.throws(() => prepareMlSourceWithPolicy({ projectRoot: f.project }, f.policy), /EXTRA_OR_MISSING/);
  rmSync(join(f.baseline, "extra.py")); chmodSync(join(f.baseline, "worker.py"), 0o600);
  assert.throws(() => prepareMlSourceWithPolicy({ projectRoot: f.project }, f.policy), /FILE_MISMATCH/);
  chmodSync(join(f.baseline, "worker.py"), 0o644); writeFileSync(join(f.baseline, "worker.py"), "tampered");
  assert.throws(() => prepareMlSourceWithPolicy({ projectRoot: f.project }, f.policy), /FILE_MISMATCH/);
});
test("拒绝来源树中的链接与目的父目录链接", t => {
  const f = fixture(t); symlinkSync(f.repo, join(f.project, "sut"));
  assert.throws(() => prepareMlSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy), /SYMLINK/);
  rmSync(join(f.project, "sut")); prepareMlSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy);
  rmSync(join(f.baseline, "worker.py")); symlinkSync(join(f.repo, "worker.py"), join(f.baseline, "worker.py"));
  assert.throws(() => prepareMlSourceWithPolicy({ projectRoot: f.project }, f.policy), /NON_REGULAR_FILE/);
});
test("遗留导出锁必须显式核实，不能按时间自动接管", t => {
  const f = fixture(t); mkdirSync(join(f.project, "sut")); writeFileSync(join(f.project, "sut/.ml-source.lock"), "owned elsewhere");
  assert.throws(() => prepareMlSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy), /EXPORT_LOCKED/);
  assert.equal(readFileSync(join(f.project, "sut/.ml-source.lock"), "utf8"), "owned elsewhere");
});
test("active profile 不能把ML命令路由回旧账本", t => {
  const f = fixture(t); mkdirSync(join(f.project, "config"));
  const profile = { schema_version: 1, business_id: "async-ml-inference", task_mode: "RELEASE_VERIFICATION", source_policy_ref: "config/ml-source-policy.json", source_lock_ref: "sut/ml-inference/upstream.lock.json", baseline_ref: "sut/ml-inference/workspaces/baseline", lab_ledger_ref: "artifacts/lab/ml-inference", api_adapter: "@egap/ml-inference-adapter" };
  const path = join(f.project, "config/active-business.json"); writeFileSync(path, JSON.stringify(profile));
  assert.equal(loadActiveBusiness(f.project).businessId, "async-ml-inference");
  writeFileSync(path, JSON.stringify({ ...profile, lab_ledger_ref: "artifacts/lab/local" }));
  assert.throws(() => loadActiveBusiness(f.project), /ACTIVE_BUSINESS_INVALID/);
});
test("即使旧账本被复制到ML目录，也在新尝试/状态输出前拒绝其来源资格", () => {
  const source = { source_commit: "c200bcb4721c4428a9bfe808ca8a61648a2b16f6", build_revision: "ml-upstream-unbuilt-v1", business_revision: "unchanged-upstream", upstream_lock_sha256: "a".repeat(64) };
  assert.doesNotThrow(() => assertActiveSourceBindings([null, source]));
  assert.throws(() => assertActiveSourceBindings([{ ...source, source_commit: "894f2dbbe9aba2be6129748b6bbf5f30356deb07" }]), /ACTIVE_BUSINESS_SOURCE_MISMATCH/);
  assert.throws(() => assertActiveSourceBindings([{ ...source, build_revision: "legacy-build" }]), /ACTIVE_BUSINESS_SOURCE_MISMATCH/);
});
