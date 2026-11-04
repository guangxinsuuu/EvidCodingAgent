import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { createReadStream } from "node:fs";
import { chmod, lstat, mkdir, mkdtemp, open, readFile, readdir, realpath, rename, rm, writeFile } from "node:fs/promises";
import { basename, join, relative, resolve, sep } from "node:path";
import { promisify } from "node:util";

const execute = promisify(execFile);
const BUILD_REVISION = "preflight-v2-local-repositories-and-verified-distribution";
const VERSION = "8.14.5";
const PATCHED_PATHS = ["gradle.properties", "gradle/wrapper/gradle-wrapper.properties"] as const;
const HASH = /^[a-f0-9]{64}$/;

export interface BaselineSourceOptions {
  projectRoot: string;
  referenceRepository: string;
  preflightEvidenceRoot: string;
}

export interface BaselineSourceReceipt {
  status: "CREATED" | "VERIFIED_EXISTING";
  source_commit: string;
  build_revision: string;
  business_revision: "unchanged-upstream";
  baseline_path: string;
  upstream_lock_path: string;
  upstream_lock_sha256: string;
  source_manifest_sha256: string;
  file_count: number;
  verification_metadata_path: string;
  gradle_distribution_path: string;
}

/** 内部核心的测试策略；生产调用者只导入baseline-source.ts的固定策略入口。 */
export interface BaselineSourcePolicy {
  commit: string;
  fileCount: number;
  distributionSha256: string;
  wrapperSha256: string;
}

interface FileRecord { path: string; sha256: string; size: number }
type JsonObject = Record<string, unknown>;
interface CheckedInputs {
  project: string;
  reference: string;
  evidence: string;
  inspection: string;
  sourceFiles: FileRecord[];
  patchedHashes: Record<string, string>;
  archiveSha256: string;
  metadata: Buffer;
  metadataSha256: string;
  patch: Buffer;
  patchSha256: string;
  distributionRef: string;
  sourceManifestSha256: string;
  inventorySha256: string;
  evidenceHashes: Record<string, string>;
}

function fail(reason: string): never {
  // 错误码不包含原始仓库配置、文件内容或命令stderr，避免把个人路径/秘密带出。
  throw new Error(`BASELINE_SOURCE_${reason}`);
}
function object(value: unknown): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) fail("INVALID_INPUT");
  return value as JsonObject;
}
function string(value: unknown): string {
  if (typeof value !== "string") fail("INVALID_INPUT");
  return value;
}
function hash(value: unknown): string {
  const result = string(value);
  if (!HASH.test(result)) fail("INVALID_HASH");
  return result;
}
function bytesHash(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}
function json(bytes: Buffer): JsonObject {
  try { return object(JSON.parse(bytes.toString("utf8"))); } catch { return fail("INVALID_JSON"); }
}
function safeRelative(value: unknown): string {
  const path = string(value);
  if (!path || path.startsWith("/") || path.includes("\\") || path.includes("\0") || path.split("/").some((part) => part === "." || part === ".." || !part)) fail("UNSAFE_PATH");
  return path;
}
function inside(root: string, path: string): string {
  const result = relative(root, path);
  if (result.startsWith(`..${sep}`) || result === ".." || result.startsWith(sep)) fail("OUTSIDE_PROJECT");
  return result;
}

/** 输入路径逐层拒绝symlink；即使最终目标在项目内，也不隐式跟随可替换链接。 */
async function checkedPath(root: string, rel: string): Promise<string> {
  let current = root;
  for (const part of safeRelative(rel).split("/")) {
    current = join(current, part);
    if ((await lstat(current)).isSymbolicLink()) fail("SYMLINK_INPUT");
  }
  return current;
}
async function regularBytes(path: string): Promise<Buffer> {
  if (!(await lstat(path)).isFile()) fail("NOT_REGULAR_FILE");
  return readFile(path);
}
async function fileHash(path: string): Promise<string> {
  if (!(await lstat(path)).isFile()) fail("NOT_REGULAR_FILE");
  const result = createHash("sha256");
  for await (const block of createReadStream(path)) result.update(block);
  return result.digest("hex");
}
async function exists(path: string): Promise<boolean> {
  try { await lstat(path); return true; } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}
async function syncPath(path: string): Promise<void> {
  const handle = await open(path, "r");
  try { await handle.sync(); } finally { await handle.close(); }
}
async function writeNew(path: string, bytes: string | Uint8Array): Promise<void> {
  const handle = await open(path, "wx", 0o600);
  try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
}

/** 只执行本地Git读命令；禁用可选index写回、fsmonitor和hooks，不运行源码。 */
async function git(reference: string, args: string[]): Promise<string> {
  try {
    const result = await execute("git", ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", reference, ...args], {
      env: { ...process.env, GIT_OPTIONAL_LOCKS: "0", GIT_TERMINAL_PROMPT: "0" },
      maxBuffer: 8 * 1024 * 1024, timeout: 30_000,
    });
    return result.stdout;
  } catch { return fail("GIT_READ_FAILED"); }
}

async function validateInputs(options: BaselineSourceOptions, policy: BaselineSourcePolicy): Promise<CheckedInputs> {
  const project = await realpath(options.projectRoot);
  const reference = await realpath(options.referenceRepository);
  // macOS的/var与/private/var可指向同一根；先在调用方词法根下求相对路径，
  // 再从真实project逐层检查，既支持系统路径别名，也不跟随项目内部symlink。
  const evidence = await checkedPath(project, inside(resolve(options.projectRoot), resolve(options.preflightEvidenceRoot)));
  if (!(await lstat(evidence)).isDirectory()) fail("INVALID_EVIDENCE_ROOT");
  const inspection = basename(evidence);
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/.test(inspection)) fail("INVALID_INSPECTION_ID");
  const evidenceHashes: Record<string, string> = {};
  const indexed = object(json(await regularBytes(join(evidence, "evidence-index.json"))));
  if (!Array.isArray(indexed.files)) fail("INVALID_EVIDENCE_INDEX");
  const entries = new Map<string, string>();
  for (const entry of indexed.files) {
    const row = object(entry);
    const path = safeRelative(row.path);
    if (entries.has(path)) fail("DUPLICATE_EVIDENCE_PATH");
    entries.set(path, hash(row.sha256));
  }
  const loadEvidence = async (name: string): Promise<Buffer> => {
    const path = await checkedPath(project, inside(project, join(evidence, name)));
    const bytes = await regularBytes(path);
    const digest = bytesHash(bytes);
    if (entries.get(inside(project, path)) !== digest) fail("EVIDENCE_HASH_MISMATCH");
    evidenceHashes[name] = digest;
    return bytes;
  };
  const report = json(await loadEvidence("report.json"));
  const sourceBytes = await loadEvidence("source-export.json");
  const source = json(sourceBytes);
  const integrity = json(await loadEvidence("gradle-integrity.json"));
  const toolchain = json(await loadEvidence("toolchain-lock.json"));
  if (report.readiness !== "READY_FOR_BASELINE" || report.source_commit !== policy.commit || report.inspection_id !== inspection || source.upstream_commit !== policy.commit || source.source_status !== "" || source.business_revision !== "unchanged-upstream" || source.build_revision !== BUILD_REVISION || toolchain.source_commit !== policy.commit || toolchain.build_revision !== BUILD_REVISION || toolchain.inspection_id !== inspection) fail("PREFLIGHT_NOT_QUALIFIED");
  const checks = report.checks;
  if (!Array.isArray(checks) || !["gradle-compile", "gradle-bootjar", "gradle-offline-compile"].every((id) => checks.some((item) => object(item).id === id && object(item).status === "PASS"))) fail("MISSING_BUILD_CHECKS");
  const distribution = object(integrity.distribution);
  const wrapper = object(integrity.wrapper);
  if (distribution.version !== VERSION || distribution.verified !== true || wrapper.verified !== true || hash(distribution.actual_sha256) !== policy.distributionSha256 || hash(distribution.official_sha256) !== policy.distributionSha256 || hash(wrapper.local_sha256) !== policy.wrapperSha256 || hash(wrapper.official_sha256) !== policy.wrapperSha256 || hash(source.wrapper_jar_sha256) !== policy.wrapperSha256) fail("TOOLCHAIN_INTEGRITY_MISMATCH");
  if (!Array.isArray(source.exported_files) || source.file_count !== policy.fileCount || source.exported_files.length !== policy.fileCount) fail("SOURCE_COUNT_MISMATCH");
  const sourceFiles = source.exported_files.map((entry) => {
    const item = object(entry);
    const path = safeRelative(item.path);
    // Git中出现缓存/凭据路径也不能随基线带入；当前固定来源没有这些文件。
    if (path.split("/").some((part) => [".git", ".gradle", "build", "node_modules", ".aws", ".ssh"].includes(part)) || /(^|\/)\.env($|\.)/.test(path)) fail("FORBIDDEN_SOURCE_PATH");
    if (typeof item.size !== "number" || !Number.isSafeInteger(item.size) || item.size < 0) fail("INVALID_SOURCE_SIZE");
    return { path, sha256: hash(item.sha256), size: item.size };
  }).sort((a, b) => a.path.localeCompare(b.path));
  if (new Set(sourceFiles.map((item) => item.path)).size !== policy.fileCount || !sourceFiles.some((item) => item.path === "LICENSE.md")) fail("INVALID_SOURCE_MANIFEST");
  const patchMap = object(source.patched_files);
  if (Object.keys(patchMap).sort().join("|") !== [...PATCHED_PATHS].sort().join("|")) fail("UNEXPECTED_BUILD_PATCH");
  const patchedHashes = Object.fromEntries(PATCHED_PATHS.map((path) => [path, hash(patchMap[path])]));
  const dependencies = object(toolchain.dependencies);
  const metadata = await loadEvidence("verification-metadata.xml");
  const inventory = await loadEvidence("resolved-dependencies.json");
  const patch = await loadEvidence("build.redacted.patch");
  if (bytesHash(metadata) !== hash(dependencies.verification_metadata_sha256) || bytesHash(inventory) !== hash(dependencies.resolved_inventory_sha256) || bytesHash(patch) !== hash(toolchain.redacted_build_patch_sha256)) fail("BUILD_INPUT_HASH_MISMATCH");
  const distributionRef = `tmp/preflight/${inspection}/gradle-${VERSION}-bin.zip`;
  if (await fileHash(await checkedPath(project, distributionRef)) !== policy.distributionSha256) fail("DISTRIBUTION_HASH_MISMATCH");
  if ((await git(reference, ["rev-parse", "HEAD"])).trim() !== policy.commit || (await git(reference, ["status", "--porcelain=v1", "--untracked-files=all"])).trim() !== "") fail("REFERENCE_NOT_CLEAN_FIXED_COMMIT");
  return { project, reference, evidence, inspection, sourceFiles, patchedHashes,
    archiveSha256: hash(source.archive_sha256), metadata, metadataSha256: bytesHash(metadata),
    patch, patchSha256: bytesHash(patch), distributionRef, sourceManifestSha256: bytesHash(sourceBytes),
    inventorySha256: bytesHash(inventory), evidenceHashes };
}

/** 将现有树完整列出；符号链接、设备、额外缓存都不会被“只比较已知文件”漏掉。 */
async function treeFiles(root: string): Promise<string[]> {
  if (!(await lstat(root)).isDirectory()) fail("INVALID_BASELINE_DIRECTORY");
  const result: string[] = [];
  const visit = async (dir: string): Promise<void> => {
    const entries = await readdir(dir, { withFileTypes: true });
    // Git不跟踪空目录；额外的空.git/.gradle目录也不能靠“没有新增文件”蒙混过关。
    if (dir !== root && entries.length === 0) fail("BASELINE_EXTRA_OR_MISSING_FILES");
    for (const entry of entries) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) await visit(path);
      else if (entry.isFile()) result.push(relative(root, path));
      else fail("UNSUPPORTED_FILE_TYPE");
    }
  };
  await visit(root);
  return result.sort();
}

function patchProperties(path: string, text: string): string {
  const replaceExactlyOnce = (pattern: RegExp, replacement: string): void => {
    if ([...text.matchAll(pattern)].length !== 1) fail("BUILD_PATCH_PRECONDITION");
    text = text.replace(pattern, replacement);
  };
  if (path === "gradle.properties") {
    replaceExactlyOnce(/^deployUrl=file:\/\/\/Users\/[^\n]+\/\.m2\/testdeploy$/gm, "deployUrl=file:///workspace/contract-repo");
    replaceExactlyOnce(/^eventuateMavenRepoUrl=file:\/\/\/Users\/[^\n]+\/\.m2\/testdeploy,https:\/\/snapshots\.repositories\.eventuate\.io\/repository$/gm, "eventuateMavenRepoUrl=https://snapshots.repositories.eventuate.io/repository");
  } else {
    replaceExactlyOnce(/^distributionUrl=https\\:\/\/downloads\.gradle\.org\/distributions\/gradle-8\.14\.5-bin\.zip$/gm, "distributionUrl=file\\:/workspace/imports/gradle-8.14.5-bin.zip");
  }
  return text;
}

async function expectedModes(inputs: CheckedInputs, policy: BaselineSourcePolicy): Promise<Map<string, number>> {
  const tree = await git(inputs.reference, ["ls-tree", "-rz", policy.commit]);
  const modes = new Map<string, number>();
  for (const row of tree.split("\0").filter(Boolean)) {
    const match = /^(100644|100755) blob [a-f0-9]{40}\t(.+)$/.exec(row);
    if (!match?.[1] || !match[2]) fail("UNSUPPORTED_GIT_ENTRY");
    modes.set(safeRelative(match[2]), match[1] === "100755" ? 0o755 : 0o644);
  }
  if ([...modes.keys()].sort().join("\0") !== inputs.sourceFiles.map((item) => item.path).sort().join("\0")) fail("GIT_TREE_MANIFEST_MISMATCH");
  return modes;
}

/** 生产入口只提供固定策略；fixture可传自己的commit，但不会经生产模块导出。 */
export async function prepareBaselineSourceWithPolicy(options: BaselineSourceOptions, policy: BaselineSourcePolicy): Promise<BaselineSourceReceipt> {
  const inputs = await validateInputs(options, policy);
  const modes = await expectedModes(inputs, policy);
  const sut = join(inputs.project, "sut");
  const tmp = join(inputs.project, "tmp");
  await mkdir(tmp, { recursive: true, mode: 0o700 });
  await checkedPath(inputs.project, "tmp");
  const guardPath = join(tmp, "baseline-source.lock");
  let guard;
  try { guard = await open(guardPath, "wx", 0o600); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") fail("EXPORT_LOCKED");
    throw error;
  }
  const token = randomUUID();
  let stage: string | undefined;
  try {
    await guard.writeFile(JSON.stringify({ token, pid: process.pid, created_at: new Date().toISOString() }) + "\n");
    await guard.sync();
    await syncPath(tmp);
    const sourceRecords = inputs.sourceFiles.map((item) => ({ ...item, mode: modes.get(item.path), effective_sha256: inputs.patchedHashes[item.path] ?? item.sha256 }));
    const lock = {
      schema_version: 1, qualification: "SOURCE_PREPARED_NOT_BUSINESS_VALIDATED",
      upstream_repository: "https://github.com/eventuate-tram/eventuate-tram-sagas-examples-customers-and-orders",
      upstream_commit: policy.commit, build_revision: BUILD_REVISION, business_revision: "unchanged-upstream",
      source_export_method: "git archive --format=tar", archive_sha256: inputs.archiveSha256,
      preflight_inspection_id: inputs.inspection, preflight_evidence_ref: inside(inputs.project, inputs.evidence),
      preflight_source_manifest_sha256: inputs.sourceManifestSha256, preflight_input_hashes: inputs.evidenceHashes,
      baseline_path: "sut/workspaces/baseline", license_path: "LICENSE.md", file_count: sourceRecords.length,
      source_files: sourceRecords,
      build_patch: { paths: [...PATCHED_PATHS], review_patch_path: "sut/build-inputs/build.redacted.patch", review_patch_sha256: inputs.patchSha256,
        meaning: "仅显式采用已预检的两份properties变更；脱敏patch供审阅，不直接交给patch命令。" },
      build_inputs: { verification_metadata_path: "sut/build-inputs/verification-metadata.xml", verification_metadata_sha256: inputs.metadataSha256,
        dependency_inventory_sha256: inputs.inventorySha256, gradle_distribution_ref: inputs.distributionRef,
        gradle_distribution_sha256: policy.distributionSha256, wrapper_jar_sha256: policy.wrapperSha256,
        staging_required: "构建前将metadata放入隔离构建副本gradle目录，将ZIP复制到容器/workspace/imports；不在此源码目录直接运行Gradle。",
        dependency_trust: "BOOTSTRAP_INVENTORY_REQUIRES_REVIEW" },
    };
    const lockBytes = JSON.stringify(lock, null, 2) + "\n";
    const receipt = (status: BaselineSourceReceipt["status"]): BaselineSourceReceipt => ({
      status, source_commit: policy.commit, build_revision: BUILD_REVISION, business_revision: "unchanged-upstream",
      baseline_path: "sut/workspaces/baseline", upstream_lock_path: "sut/upstream.lock.json",
      upstream_lock_sha256: bytesHash(Buffer.from(lockBytes)), source_manifest_sha256: inputs.sourceManifestSha256,
      file_count: inputs.sourceFiles.length, verification_metadata_path: "sut/build-inputs/verification-metadata.xml",
      gradle_distribution_path: inputs.distributionRef,
    });
    const verifyTree = async (root: string): Promise<void> => {
      const expected = [...sourceRecords.map((item) => `workspaces/baseline/${item.path}`), "upstream.lock.json", "build-inputs/verification-metadata.xml", "build-inputs/build.redacted.patch"].sort();
      if ((await treeFiles(root)).join("\0") !== expected.join("\0")) fail("BASELINE_EXTRA_OR_MISSING_FILES");
      if (!(await regularBytes(join(root, "upstream.lock.json"))).equals(Buffer.from(lockBytes)) || await fileHash(join(root, "build-inputs/verification-metadata.xml")) !== inputs.metadataSha256 || await fileHash(join(root, "build-inputs/build.redacted.patch")) !== inputs.patchSha256) fail("EXISTING_LOCK_OR_INPUT_MISMATCH");
      for (const item of sourceRecords) {
        const path = join(root, "workspaces/baseline", item.path);
        if (await fileHash(path) !== item.effective_sha256 || ((await lstat(path)).mode & 0o777) !== item.mode) fail("BASELINE_FILE_MISMATCH");
      }
    };
    if (await exists(sut)) {
      await verifyTree(sut);
      return receipt("VERIFIED_EXISTING");
    }
    // 单次rename提交整棵sut：源码、锁与构建输入不会出现各自已提交一半的状态。
    // 崩溃留下的stage/guard不自动按时间删除，下一次会拒绝并要求先核对拥有者。
    stage = await mkdtemp(join(inputs.project, ".baseline-stage-"));
    const baseline = join(stage, "workspaces/baseline");
    await mkdir(baseline, { recursive: true, mode: 0o700 });
    await mkdir(join(stage, "build-inputs"), { mode: 0o700 });
    const archive = join(stage, "source.tar");
    await git(inputs.reference, ["archive", "--format=tar", `--output=${archive}`, policy.commit]);
    if (await fileHash(archive) !== inputs.archiveSha256) fail("ARCHIVE_HASH_MISMATCH");
    // ls-tree已拒绝symlink/submodule与不安全路径，tar只解压Git生成且摘要一致的归档。
    try { await execute("tar", ["-xf", archive, "-C", baseline], { timeout: 30_000, maxBuffer: 1024 * 1024 }); } catch { fail("ARCHIVE_EXTRACTION_FAILED"); }
    await rm(archive);
    if ((await treeFiles(baseline)).join("\0") !== inputs.sourceFiles.map((item) => item.path).sort().join("\0")) fail("ARCHIVE_FILES_MISMATCH");
    for (const item of inputs.sourceFiles) {
      const path = join(baseline, item.path);
      const original = await regularBytes(path);
      if (original.length !== item.size || bytesHash(original) !== item.sha256) fail("EXPORTED_SOURCE_HASH_MISMATCH");
      if (PATCHED_PATHS.includes(item.path as typeof PATCHED_PATHS[number])) {
        const patched = Buffer.from(patchProperties(item.path, original.toString("utf8")));
        if (bytesHash(patched) !== inputs.patchedHashes[item.path]) fail("BUILD_PATCH_RESULT_MISMATCH");
        await writeFile(path, patched);
      }
      await chmod(path, modes.get(item.path)!);
      await syncPath(path);
    }
    await writeNew(join(stage, "upstream.lock.json"), lockBytes);
    await writeNew(join(stage, "build-inputs/verification-metadata.xml"), inputs.metadata);
    await writeNew(join(stage, "build-inputs/build.redacted.patch"), inputs.patch);
    await verifyTree(stage);
    // 文件落盘后同步目录，再提交；不把进程正常退出等同于磁盘已确认写入。
    const syncDirectories = async (root: string): Promise<void> => {
      for (const entry of await readdir(root, { withFileTypes: true })) if (entry.isDirectory()) await syncDirectories(join(root, entry.name));
      await syncPath(root);
    };
    await syncDirectories(stage);
    if (await exists(sut)) fail("DESTINATION_APPEARED");
    await rename(stage, sut);
    stage = undefined;
    await syncPath(inputs.project);
    return receipt("CREATED");
  } finally {
    await guard.close();
    if (stage !== undefined) await rm(stage, { recursive: true, force: true });
    // 只删除本调用拥有的锁；未知或被替换的锁留给人工/上层恢复，不猜测清理。
    const current = json(await regularBytes(guardPath));
    if (current.token === token) { await rm(guardPath); await syncPath(tmp); }
  }
}
