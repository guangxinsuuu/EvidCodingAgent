import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { promisify } from "node:util";
import { test } from "node:test";
import { prepareBaselineSource } from "./baseline-source.js";
import { prepareBaselineSourceWithPolicy } from "./baseline-source-core.js";
import type { BaselineSourceOptions, BaselineSourcePolicy } from "./baseline-source-core.js";

const exec = promisify(execFile);
const sha = (bytes: Uint8Array | string) => createHash("sha256").update(bytes).digest("hex");
const BUILD_REVISION = "preflight-v2-local-repositories-and-verified-distribution";

/** 独立小仓库测试完整导出协议；不依赖用户真实仓库、缓存或Docker。 */
async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "egap-baseline-source-"));
  const projectRoot = join(root, "project");
  const referenceRepository = join(root, "reference");
  const inspection = "fixture-01";
  const evidence = join(projectRoot, "artifacts/evidence/preflight", inspection);
  const distributionPath = join(projectRoot, "tmp/preflight", inspection, "gradle-8.14.5-bin.zip");
  await mkdir(evidence, { recursive: true });
  await mkdir(referenceRepository);
  await mkdir(join(projectRoot, "tmp/preflight", inspection), { recursive: true });
  const zip = Buffer.from("fixture distribution bytes; no executable archive");
  const wrapper = Buffer.from("fixture wrapper bytes; never executed");
  const originalProperties = "deployUrl=file:///Users/fixture/.m2/testdeploy\n\neventuateMavenRepoUrl=file:///Users/fixture/.m2/testdeploy,https://snapshots.repositories.eventuate.io/repository\n\nversion=0.1.0-SNAPSHOT\n";
  const effectiveProperties = "deployUrl=file:///workspace/contract-repo\n\neventuateMavenRepoUrl=https://snapshots.repositories.eventuate.io/repository\n\nversion=0.1.0-SNAPSHOT\n";
  const originalWrapperProperties = `distributionUrl=https\\://downloads.gradle.org/distributions/gradle-8.14.5-bin.zip\ndistributionSha256Sum=${sha(zip)}\n`;
  const effectiveWrapperProperties = `distributionUrl=file\\:/workspace/imports/gradle-8.14.5-bin.zip\ndistributionSha256Sum=${sha(zip)}\n`;
  const files = new Map<string, Buffer>([
    ["LICENSE.md", Buffer.from("fixture license\n")],
    ["gradle.properties", Buffer.from(originalProperties)],
    ["gradle/wrapper/gradle-wrapper.properties", Buffer.from(originalWrapperProperties)],
    ["gradle/wrapper/gradle-wrapper.jar", wrapper],
    ["src/Business.java", Buffer.from("class Business {}\n")],
    ["gradlew", Buffer.from("#!/bin/sh\nexit 99\n")],
  ]);
  for (const [path, bytes] of files) {
    await mkdir(join(referenceRepository, path, ".."), { recursive: true });
    await writeFile(join(referenceRepository, path), bytes);
  }
  await chmod(join(referenceRepository, "gradlew"), 0o755);
  const git = async (args: string[]) => (await exec("git", ["-C", referenceRepository, ...args], { env: { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null", GIT_OPTIONAL_LOCKS: "0" } })).stdout.trim();
  await git(["init", "--quiet"]);
  await git(["add", "."]);
  await git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture source"]);
  const commit = await git(["rev-parse", "HEAD"]);
  const archive = join(root, "source.tar");
  await git(["archive", "--format=tar", `--output=${archive}`, commit]);
  await writeFile(distributionPath, zip);
  const metadata = Buffer.from("<verification-metadata/>\n");
  const inventory = Buffer.from('{"files":[]}\n');
  const patch = Buffer.from("fixture redacted build patch\n");
  const contents: Record<string, unknown> = {
    "report.json": { readiness: "READY_FOR_BASELINE", source_commit: commit, inspection_id: inspection,
      checks: ["gradle-compile", "gradle-bootjar", "gradle-offline-compile"].map((id) => ({ id, status: "PASS" })) },
    "source-export.json": { upstream_commit: commit, source_status: "", business_revision: "unchanged-upstream", build_revision: BUILD_REVISION,
      file_count: files.size, exported_files: [...files].map(([path, bytes]) => ({ path, sha256: sha(bytes), size: bytes.length })),
      archive_sha256: sha(await readFile(archive)), wrapper_jar_sha256: sha(wrapper),
      patched_files: { "gradle.properties": sha(effectiveProperties), "gradle/wrapper/gradle-wrapper.properties": sha(effectiveWrapperProperties) } },
    "gradle-integrity.json": { distribution: { version: "8.14.5", verified: true, actual_sha256: sha(zip), official_sha256: sha(zip) },
      wrapper: { verified: true, local_sha256: sha(wrapper), official_sha256: sha(wrapper) } },
    "toolchain-lock.json": { source_commit: commit, build_revision: BUILD_REVISION, inspection_id: inspection,
      redacted_build_patch_sha256: sha(patch), dependencies: { verification_metadata_sha256: sha(metadata), resolved_inventory_sha256: sha(inventory) } },
  };
  for (const [name, value] of Object.entries(contents)) await writeFile(join(evidence, name), JSON.stringify(value));
  await writeFile(join(evidence, "verification-metadata.xml"), metadata);
  await writeFile(join(evidence, "resolved-dependencies.json"), inventory);
  await writeFile(join(evidence, "build.redacted.patch"), patch);
  const refreshIndex = async () => {
    const paths = (await readdir(evidence)).filter((name) => name !== "evidence-index.json").sort();
    const records = await Promise.all(paths.map(async (name) => ({ path: relative(projectRoot, join(evidence, name)), sha256: sha(await readFile(join(evidence, name))) })));
    await writeFile(join(evidence, "evidence-index.json"), JSON.stringify({ files: records }));
  };
  await refreshIndex();
  const options: BaselineSourceOptions = { projectRoot, referenceRepository, preflightEvidenceRoot: evidence };
  const policy: BaselineSourcePolicy = { commit, fileCount: files.size, distributionSha256: sha(zip), wrapperSha256: sha(wrapper) };
  return { root, projectRoot, evidence, distributionPath, referenceRepository, options, policy, files,
    originalProperties, effectiveProperties, effectiveWrapperProperties, contents, refreshIndex,
    run: () => prepareBaselineSourceWithPolicy(options, policy), git,
    cleanup: () => rm(root, { recursive: true, force: true }) };
}

test("固定来源重新导出、仅两构建补丁、模式与许可证保留；重跑只核对", async () => {
  const f = await fixture();
  try {
    const result = await f.run();
    assert.equal(result.status, "CREATED");
    assert.equal(result.file_count, f.files.size);
    assert.equal(await readFile(join(f.projectRoot, result.baseline_path, "gradle.properties"), "utf8"), f.effectiveProperties);
    assert.equal(await readFile(join(f.projectRoot, result.baseline_path, "gradle/wrapper/gradle-wrapper.properties"), "utf8"), f.effectiveWrapperProperties);
    assert.deepEqual(await readFile(join(f.projectRoot, result.baseline_path, "LICENSE.md")), f.files.get("LICENSE.md"));
    assert.equal(await readFile(join(f.referenceRepository, "gradle.properties"), "utf8"), f.originalProperties);
    assert.equal(await f.git(["status", "--porcelain"]), "");
    assert.equal(sha(await readFile(join(f.projectRoot, result.upstream_lock_path))), result.upstream_lock_sha256);
    assert.equal((await f.run()).status, "VERIFIED_EXISTING");
    assert.deepEqual((await readdir(join(f.projectRoot, "sut/workspaces/baseline"))).sort(), ["LICENSE.md", "gradle", "gradle.properties", "gradlew", "src"].sort());
  } finally { await f.cleanup(); }
});

test("生产入口拒绝fixture commit，不能通过替换preflight改变固定来源", async () => {
  const f = await fixture();
  try { await assert.rejects(prepareBaselineSource(f.options), /PREFLIGHT_NOT_QUALIFIED/); }
  finally { await f.cleanup(); }
});

test("证据或分发ZIP损坏时先拒绝，不创建sut", async () => {
  const f = await fixture();
  try {
    await writeFile(join(f.evidence, "verification-metadata.xml"), "tampered metadata");
    await assert.rejects(f.run(), /EVIDENCE_HASH_MISMATCH/);
    assert.equal((await readdir(f.projectRoot)).includes("sut"), false);
    await writeFile(join(f.evidence, "verification-metadata.xml"), "<verification-metadata/>\n");
    await writeFile(f.distributionPath, "tampered ZIP");
    await assert.rejects(f.run(), /DISTRIBUTION_HASH_MISMATCH/);
    assert.equal((await readdir(f.projectRoot)).includes("sut"), false);
  } finally { await f.cleanup(); }
});

test("脏基线文件不会被幂等重跑覆盖", async () => {
  const f = await fixture();
  try {
    await f.run();
    const business = join(f.projectRoot, "sut/workspaces/baseline/src/Business.java");
    await writeFile(business, "unreviewed local change\n");
    await assert.rejects(f.run(), /BASELINE_FILE_MISMATCH/);
    assert.equal(await readFile(business, "utf8"), "unreviewed local change\n");
  } finally { await f.cleanup(); }
});

test("已有半完成目录、额外缓存与symlink均拒绝", async () => {
  const f = await fixture();
  try {
    await mkdir(join(f.projectRoot, "sut"));
    await assert.rejects(f.run(), /BASELINE_EXTRA_OR_MISSING_FILES/);
    await rm(join(f.projectRoot, "sut"), { recursive: true });
    await f.run();
    const extra = join(f.projectRoot, "sut/workspaces/baseline/cache.bin");
    await writeFile(extra, "untracked cache");
    await assert.rejects(f.run(), /BASELINE_EXTRA_OR_MISSING_FILES/);
    await rm(extra);
    const emptyCache = join(f.projectRoot, "sut/workspaces/baseline/.gradle");
    await mkdir(emptyCache);
    await assert.rejects(f.run(), /BASELINE_EXTRA_OR_MISSING_FILES/);
    await rm(emptyCache, { recursive: true });
    await symlink(join(f.referenceRepository, "src/Business.java"), extra);
    await assert.rejects(f.run(), /UNSUPPORTED_FILE_TYPE/);
  } finally { await f.cleanup(); }
});

test("归档摘要失败清理自有stage和锁，不留下半完成sut", async () => {
  const f = await fixture();
  try {
    const source = f.contents["source-export.json"] as Record<string, unknown>;
    source.archive_sha256 = "0".repeat(64);
    await writeFile(join(f.evidence, "source-export.json"), JSON.stringify(source));
    await f.refreshIndex();
    await assert.rejects(f.run(), /ARCHIVE_HASH_MISMATCH/);
    assert.equal((await readdir(f.projectRoot)).some((name) => name === "sut" || name.startsWith(".baseline-stage-")), false);
    assert.equal((await readdir(join(f.projectRoot, "tmp"))).includes("baseline-source.lock"), false);
  } finally { await f.cleanup(); }
});

test("已有导出锁拒绝第二写者，不按时间删除外部锁", async () => {
  const f = await fixture();
  try {
    const path = join(f.projectRoot, "tmp/baseline-source.lock");
    const owner = JSON.stringify({ token: "other-owner", pid: 1, created_at: "1970-01-01T00:00:00Z" });
    await writeFile(path, owner);
    await assert.rejects(f.run(), /EXPORT_LOCKED/);
    assert.equal(await readFile(path, "utf8"), owner);
  } finally { await f.cleanup(); }
});

test("参考工作树存在业务修改时拒绝且原修改保留", async () => {
  const f = await fixture();
  try {
    const path = join(f.referenceRepository, "src/Business.java");
    await writeFile(path, "dirty reference\n");
    await assert.rejects(f.run(), /REFERENCE_NOT_CLEAN_FIXED_COMMIT/);
    assert.equal(await readFile(path, "utf8"), "dirty reference\n");
  } finally { await f.cleanup(); }
});

test("两个并发导出调用只会创建一份正式树", async () => {
  const f = await fixture();
  try {
    const results = await Promise.allSettled([f.run(), f.run()]);
    const successful = results.filter((result) => result.status === "fulfilled");
    assert.equal(successful.filter((result) => result.value.status === "CREATED").length, 1);
    for (const result of results) {
      if (result.status === "rejected") assert.match(String(result.reason), /EXPORT_LOCKED/);
    }
    assert.equal((await f.run()).status, "VERIFIED_EXISTING");
    assert.equal((await readdir(f.projectRoot)).filter((name) => name.startsWith(".baseline-stage-")).length, 0);
  } finally { await f.cleanup(); }
});
