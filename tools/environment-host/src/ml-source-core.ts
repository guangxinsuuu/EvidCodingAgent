import { createHash, randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { chmodSync, closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, mkdtempSync, openSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";

export interface MlSourceFile { path: string; git_blob: string; mode: number; size: number; sha256: string }
/** 核心允许测试注入微型 Git 仓库；生产入口在 ml-source.ts 固定完整策略摘要。 */
export interface MlSourcePolicy {
  schema_version: 1; business_id: "async-ml-inference"; repository: string;
  commit: string; tree: string; file_count: number; files: MlSourceFile[];
}
export interface MlSourceOptions { projectRoot: string; referenceRepository?: string }
export interface MlSourceReceipt {
  status: "CREATED" | "VERIFIED_EXISTING"; source_commit: string; build_revision: "ml-upstream-unbuilt-v1";
  business_revision: "unchanged-upstream"; baseline_path: string; upstream_lock_path: string;
  upstream_lock_sha256: string; file_count: number; qualification: "SOURCE_ONLY_NOT_RUNTIME_VALIDATED";
}
const sha = (bytes: Uint8Array): string => createHash("sha256").update(bytes).digest("hex");
function requireFact(ok: unknown, code: string): asserts ok { if (!ok) throw new Error(`ML_SOURCE_${code}`); }

/** 项目内部逐层拒绝符号链接；不会因目标目前还不存在而跳过已有父目录。 */
export function checkedMlPath(project: string, ref: string): string {
  requireFact(/^[A-Za-z0-9_./-]+$/.test(ref) && !ref.startsWith("/") && !ref.split("/").some(p => ["", ".", ".."].includes(p)), "UNSAFE_PATH");
  let current = project;
  for (const part of ref.split("/")) {
    current = join(current, part);
    try { requireFact(!lstatSync(current).isSymbolicLink(), "SYMLINK"); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  }
  return current;
}
function sync(path: string): void { const fd = openSync(path, "r"); try { fsyncSync(fd); } finally { closeSync(fd); } }
function writeNew(path: string, bytes: Uint8Array, mode = 0o600): void {
  const fd = openSync(path, "wx", mode);
  try { writeFileSync(fd, bytes); chmodSync(path, mode); fsyncSync(fd); } finally { closeSync(fd); }
}
function filesUnder(root: string): string[] {
  const result: string[] = [];
  const visit = (dir: string): void => {
    const entries = readdirSync(dir, { withFileTypes: true });
    requireFact(dir === root || entries.length > 0, "EXTRA_EMPTY_DIRECTORY");
    for (const entry of entries) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) visit(path);
      else { requireFact(entry.isFile(), "NON_REGULAR_FILE"); result.push(relative(root, path)); }
    }
  };
  visit(root); return result.sort();
}
function git(repo: string, args: string[]): Buffer {
  try {
    // 仅读 Git 对象，不运行仓库脚本/钩子/构建；不读取用户 credential helper。
    return execFileSync("git", ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-C", repo, ...args], {
      env: { PATH: process.env.PATH ?? "/usr/bin:/bin", GIT_OPTIONAL_LOCKS: "0", GIT_TERMINAL_PROMPT: "0", GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null" },
      timeout: 30_000, maxBuffer: 8 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"],
    });
  } catch { throw new Error("ML_SOURCE_GIT_READ_FAILED"); }
}

/**
 * prepareMlBaselineSource → 本函数 → 校验 Git blob → 原子提交来源锁和完整副本。
 * 不执行 Python/uv/Docker，也不把源码完整性提升为模型或运行资格。
 * 已存在副本只核验，不覆盖；重试无需网络或原 Git 缓存，脏文件/额外文件均拒绝。
 */
export function prepareMlSourceWithPolicy(options: MlSourceOptions, policy: MlSourcePolicy): MlSourceReceipt {
  const project = realpathSync(options.projectRoot);
  requireFact(policy.schema_version === 1 && policy.business_id === "async-ml-inference" && /^[a-f0-9]{40}$/.test(policy.commit) && /^[a-f0-9]{40}$/.test(policy.tree), "INVALID_POLICY");
  requireFact(policy.files.length === policy.file_count && policy.file_count > 0 && new Set(policy.files.map(f => f.path)).size === policy.file_count, "INVALID_FILE_SET");
  for (const f of policy.files) {
    checkedMlPath(project, f.path);
    requireFact(/^[a-f0-9]{40}$/.test(f.git_blob) && /^[a-f0-9]{64}$/.test(f.sha256) && [0o644, 0o755].includes(f.mode) && Number.isSafeInteger(f.size) && f.size >= 0, "INVALID_FILE_RECORD");
    requireFact(!f.path.split("/").some(p => [".git", ".venv", "node_modules", "__pycache__", ".ssh", ".aws"].includes(p)) && !/(^|\/)\.env($|\.)/.test(f.path), "FORBIDDEN_SOURCE_PATH");
  }
  requireFact(policy.files.some(f => f.path === "LICENSE"), "LICENSE_MISSING");
  const baselineRef = "sut/ml-inference/workspaces/baseline";
  const lockRef = "sut/ml-inference/upstream.lock.json";
  const root = checkedMlPath(project, "sut/ml-inference");
  const lock = { schema_version: 1, business_id: policy.business_id, qualification: "SOURCE_ONLY_NOT_RUNTIME_VALIDATED",
    upstream_repository: policy.repository, upstream_commit: policy.commit, upstream_tree: policy.tree,
    build_revision: "ml-upstream-unbuilt-v1", business_revision: "unchanged-upstream", baseline_path: baselineRef,
    source_export_method: "verified-git-blob-by-blob", file_count: policy.file_count, source_files: policy.files,
    dependency_status: "LOCK_PRESENT_RESOLUTION_NOT_RUN", model_artifact_status: "NOT_DOWNLOADED",
    runtime_qualification: "NOT_RUN", business_verification: "NOT_RUN" };
  const lockBytes = Buffer.from(JSON.stringify(lock, null, 2) + "\n");
  const receipt = (status: MlSourceReceipt["status"]): MlSourceReceipt => ({ status, source_commit: policy.commit,
    build_revision: "ml-upstream-unbuilt-v1", business_revision: "unchanged-upstream", baseline_path: baselineRef,
    upstream_lock_path: lockRef, upstream_lock_sha256: sha(lockBytes), file_count: policy.file_count,
    qualification: "SOURCE_ONLY_NOT_RUNTIME_VALIDATED" });
  const verify = (dir: string): void => {
    requireFact(lstatSync(dir).isDirectory() && !lstatSync(dir).isSymbolicLink(), "INVALID_DESTINATION");
    const expected = ["upstream.lock.json", ...policy.files.map(f => `workspaces/baseline/${f.path}`)].sort();
    requireFact(JSON.stringify(filesUnder(dir)) === JSON.stringify(expected), "EXTRA_OR_MISSING_FILES");
    requireFact(readFileSync(join(dir, "upstream.lock.json")).equals(lockBytes), "LOCK_MISMATCH");
    for (const f of policy.files) {
      const path = join(dir, "workspaces/baseline", f.path); const bytes = readFileSync(path);
      requireFact(bytes.length === f.size && sha(bytes) === f.sha256 && (lstatSync(path).mode & 0o777) === f.mode, "FILE_MISMATCH");
    }
  };
  if (existsSync(root)) { verify(root); return receipt("VERIFIED_EXISTING"); }
  requireFact(options.referenceRepository, "REFERENCE_REPOSITORY_REQUIRED");
  const reference = realpathSync(options.referenceRepository);
  requireFact(git(reference, ["rev-parse", "HEAD"]).toString().trim() === policy.commit && git(reference, ["status", "--porcelain=v1", "--untracked-files=all"]).length === 0, "REFERENCE_NOT_CLEAN_FIXED_COMMIT");
  requireFact(git(reference, ["rev-parse", `${policy.commit}^{tree}`]).toString().trim() === policy.tree, "TREE_MISMATCH");
  const actualTree = git(reference, ["ls-tree", "-rz", policy.commit]).toString().split("\0").filter(Boolean).sort();
  const expectedTree = policy.files.map(f => `${f.mode === 0o755 ? "100755" : "100644"} blob ${f.git_blob}\t${f.path}`).sort();
  requireFact(JSON.stringify(actualTree) === JSON.stringify(expectedTree), "TREE_ENTRIES_MISMATCH");
  const parent = checkedMlPath(project, "sut"); mkdirSync(parent, { recursive: true, mode: 0o700 });
  const guard = join(parent, ".ml-source.lock");
  const token = randomUUID(); let guardFd: number;
  try { guardFd = openSync(guard, "wx", 0o600); } catch { throw new Error("ML_SOURCE_EXPORT_LOCKED"); }
  let stage: string | undefined;
  try {
    writeFileSync(guardFd, JSON.stringify({ token, pid: process.pid }) + "\n"); fsyncSync(guardFd); sync(parent);
    stage = mkdtempSync(join(parent, ".ml-source-stage-"));
    for (const f of policy.files) {
      const bytes = git(reference, ["cat-file", "blob", f.git_blob]);
      requireFact(bytes.length === f.size && sha(bytes) === f.sha256, "GIT_BLOB_MISMATCH");
      const path = join(stage, "workspaces/baseline", f.path);
      mkdirSync(join(path, ".."), { recursive: true, mode: 0o700 }); writeNew(path, bytes, f.mode);
    }
    writeNew(join(stage, "upstream.lock.json"), lockBytes); verify(stage);
    const syncDirs = (dir: string): void => { for (const e of readdirSync(dir, { withFileTypes: true })) if (e.isDirectory()) syncDirs(join(dir, e.name)); sync(dir); };
    syncDirs(stage);
    requireFact(!existsSync(root), "DESTINATION_APPEARED"); renameSync(stage, root); stage = undefined; sync(parent);
    return receipt("CREATED");
  } finally {
    closeSync(guardFd);
    if (stage) rmSync(stage, { recursive: true, force: true });
    // 只清理本次拥有的导出锁，崩溃遗留不会被按时间猜测删除。
    if (JSON.parse(readFileSync(guard, "utf8")).token === token) { rmSync(guard); sync(parent); }
  }
}
