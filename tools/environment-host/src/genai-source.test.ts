import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { assertActiveSourceBindings, loadActiveBusiness } from "./business-profile.js";
import { GENAI_SOURCE_BINDING, prepareGenAiReferenceSource, prepareGenAiSourceWithPolicy } from "./genai-source.js";
import type { GenAiSourcePolicy } from "./genai-source.js";

/** 真实微型Git对象验证来源协议；文件中含不可执行标记，测试从不启动任何Python/LLM。 */
function fixture(t: test.TestContext) {
  const root = mkdtempSync(join(tmpdir(), "egap-genai-source-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const project = join(root, "project"); const repo = join(root, "repo"); mkdirSync(project); mkdirSync(repo);
  const git = (...args: string[]): Buffer => execFileSync("git", ["-c", "core.hooksPath=/dev/null", "-C", repo, ...args], {
    stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1" },
  });
  git("init", "-q"); writeFileSync(join(repo, "LICENSE"), "MIT synthetic fixture\n");
  writeFileSync(join(repo, "agent.py"), "raise RuntimeError('must never execute or call model')\n");
  git("add", "."); git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture");
  const files = git("ls-tree", "-rz", "HEAD").toString().split("\0").filter(Boolean).map(row => {
    const [head, path] = row.split("\t"); const oid = head!.split(" ")[2]!; const bytes = git("cat-file", "blob", oid);
    return { path: path!, git_blob: oid, mode: 0o644, size: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") };
  });
  const policy: GenAiSourcePolicy = { schema_version: 1, business_id: "genai-support", repository: "https://example.invalid/fixture",
    commit: git("rev-parse", "HEAD").toString().trim(), tree: git("rev-parse", "HEAD^{tree}").toString().trim(), file_count: files.length, files };
  return { project, repo, policy };
}

const profile = {
  schema_version: 1, business_id: "genai-support", task_mode: "RELEASE_VERIFICATION",
  source_policy_ref: "config/genai-support-source-policy.json", source_lock_ref: "sut/genai-support/upstream.lock.json",
  reference_ref: "sut/genai-support/workspaces/reference", lab_ledger_ref: "artifacts/lab/genai-support",
  api_adapter: "@egap/genai-support-adapter", source_role: "NON_SAAS_DEMO_REFERENCE",
  runtime_qualification: "NOT_RUN", sut_implementation_status: "NOT_IMPLEMENTED",
};

test("GenAI参考源离线复核不冒充SaaS基线，也不改变旧ML来源", t => {
  const f = fixture(t); mkdirSync(join(f.project, "sut/ml-inference"), { recursive: true });
  writeFileSync(join(f.project, "sut/ml-inference/upstream.lock.json"), "historical ML lock\n");
  const first = prepareGenAiSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy);
  assert.equal(first.status, "CREATED"); assert.equal(first.reference_path, "sut/genai-support/workspaces/reference");
  assert.equal(first.qualification, "SOURCE_REFERENCE_ONLY_NOT_RUNTIME_VALIDATED");
  assert.equal(first.reference_role, "NON_SAAS_DEMO_REFERENCE");
  assert.equal("baseline_path" in first, false);
  const lock = JSON.parse(readFileSync(join(f.project, first.upstream_lock_path), "utf8")) as Record<string, unknown>;
  assert.equal(lock.llm_status, "NOT_CALLED"); assert.equal(lock.derived_sut_status, "NOT_IMPLEMENTED");
  assert.equal(lock.runtime_qualification, "NOT_RUN"); assert.equal(lock.dependency_status, "NOT_RESOLVED");
  rmSync(f.repo, { recursive: true });
  const second = prepareGenAiSourceWithPolicy({ projectRoot: f.project }, f.policy);
  assert.equal(second.status, "VERIFIED_EXISTING"); assert.equal(first.upstream_lock_sha256, second.upstream_lock_sha256);
  assert.equal(readFileSync(join(f.project, "sut/ml-inference/upstream.lock.json"), "utf8"), "historical ML lock\n");
});

test("既有参考锁不能被改成已运行资格，也不能补写SaaS文件冒充同一来源", t => {
  const f = fixture(t); const receipt = prepareGenAiSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, f.policy);
  const path = join(f.project, receipt.upstream_lock_path); const original = readFileSync(path, "utf8");
  writeFileSync(path, original.replace('"runtime_qualification": "NOT_RUN"', '"runtime_qualification": "PASS"'));
  assert.throws(() => prepareGenAiSourceWithPolicy({ projectRoot: f.project }, f.policy), /LOCK_MISMATCH/);
  writeFileSync(path, original); writeFileSync(join(f.project, receipt.reference_path, "saas.py"), "unreviewed extension\n");
  assert.throws(() => prepareGenAiSourceWithPolicy({ projectRoot: f.project }, f.policy), /EXTRA_OR_MISSING/);
});

test("正式GenAI入口拒绝仅修改JSON的来源替换，不创建导出目录", t => {
  const f = fixture(t); mkdirSync(join(f.project, "config"));
  writeFileSync(join(f.project, "config/genai-support-source-policy.json"), JSON.stringify(f.policy));
  assert.throws(() => prepareGenAiReferenceSource({ projectRoot: f.project, referenceRepository: f.repo }), /POLICY_DIGEST_MISMATCH/);
  assert.throws(() => readFileSync(join(f.project, "sut/genai-support/upstream.lock.json")), /ENOENT/);
});

test("GenAI门面拒绝ML策略，公共导出器不能跨业务选择命名空间", t => {
  const f = fixture(t); const incorrect = { ...f.policy, business_id: "async-ml-inference" } as unknown as GenAiSourcePolicy;
  assert.throws(() => prepareGenAiSourceWithPolicy({ projectRoot: f.project, referenceRepository: f.repo }, incorrect), /GENAI_SOURCE_INVALID_POLICY/);
});

test("GenAI active profile拒绝旧来源/账本、任意适配器以及虚构运行资格", t => {
  const f = fixture(t); mkdirSync(join(f.project, "config")); const path = join(f.project, "config/active-business.json");
  writeFileSync(path, JSON.stringify(profile)); assert.equal(loadActiveBusiness(f.project).businessId, "genai-support");
  for (const change of [
    { business_id: "async-ml-inference" }, { lab_ledger_ref: "artifacts/lab/ml-inference" },
    { lab_ledger_ref: "artifacts/lab/local" }, { source_lock_ref: "sut/ml-inference/upstream.lock.json" },
    { api_adapter: "arbitrary-plugin" }, { runtime_qualification: "PASS" }, { sut_implementation_status: "READY" },
  ]) {
    writeFileSync(path, JSON.stringify({ ...profile, ...change }));
    assert.throws(() => loadActiveBusiness(f.project), /ACTIVE_BUSINESS_INVALID/);
  }
});

test("账本来源必须绑定GenAI的commit、构建阶段和完整锁摘要", () => {
  const source = { ...GENAI_SOURCE_BINDING };
  assert.doesNotThrow(() => assertActiveSourceBindings([null, source]));
  for (const change of [
    { source_commit: "c200bcb4721c4428a9bfe808ca8a61648a2b16f6" }, { build_revision: "ml-upstream-unbuilt-v1" },
    { business_revision: "healthy-saas" }, { upstream_lock_sha256: "a".repeat(64) },
  ]) assert.throws(() => assertActiveSourceBindings([{ ...source, ...change }]), /ACTIVE_BUSINESS_SOURCE_MISMATCH/);
});
