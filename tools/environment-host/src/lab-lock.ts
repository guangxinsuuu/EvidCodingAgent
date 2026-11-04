import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  closeSync, constants, fstatSync, fsyncSync, lstatSync,
  mkdirSync, openSync, readFileSync, readdirSync, renameSync, rmdirSync,
  unlinkSync, writeFileSync,
} from "node:fs";
import { hostname, platform } from "node:os";
import { dirname, join, resolve } from "node:path";

/** 锁身份用于单机进程核对；token和PID均不是外部工具访问凭据。 */
export interface LabLockOwner {
  readonly schema_version: 1;
  readonly token: string;
  readonly pid: number;
  readonly hostname: string;
  readonly boot_identity: string;
  readonly created_at: string;
}

export interface LabLock {
  readonly owner: LabLockOwner;
  assertOwned(): void;
  release(): void;
}

export class LabLockError extends Error {
  constructor(readonly code: string) {
    super(code);
    this.name = "LabLockError";
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const OWNER_KEYS = ["schema_version", "token", "pid", "hostname", "boot_identity", "created_at"];

function fail(code: string): never {
  throw new LabLockError(code);
}

function isErrno(error: unknown, code: string): boolean {
  return error instanceof Error && "code" in error && error.code === code;
}

/** lstat把断开的symlink也视为占位，不能将未知guard条目当作不存在。 */
function entryExists(path: string): boolean {
  try { lstatSync(path); return true; }
  catch (error) {
    if (isErrno(error, "ENOENT")) return false;
    throw error;
  }
}

/** 不支持不可验证的宿主时直接拒绝，不能用时间戳猜测机器已经重启。 */
function queryBootIdentity(): string {
  if (platform() === "linux") {
    const id = readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim();
    if (!/^[0-9a-f-]{36}$/.test(id)) fail("LAB_BOOT_ID_UNVERIFIABLE");
    return `linux:${id}`;
  }
  if (platform() === "darwin") {
    const result = execFileSync("/usr/sbin/sysctl", ["-n", "kern.boottime"], {
      encoding: "utf8", timeout: 3_000, maxBuffer: 4_096,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const match = /^\{\s*sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)\s*\}/.exec(result);
    if (!match) fail("LAB_BOOT_ID_UNVERIFIABLE");
    return `darwin:${match[1]}:${match[2]}`;
  }
  return fail("LAB_HOST_UNSUPPORTED");
}

function currentBootIdentity(): string {
  try { return queryBootIdentity(); }
  catch (error) {
    if (error instanceof LabLockError) throw error;
    // 沙箱或宿主拒绝查询时，不能退化成hostname或“当前时间减uptime”。
    return fail("LAB_BOOT_ID_UNVERIFIABLE");
  }
}

function newOwner(): LabLockOwner {
  return Object.freeze({
    schema_version: 1, token: randomUUID(), pid: process.pid,
    hostname: hostname(), boot_identity: currentBootIdentity(),
    created_at: new Date().toISOString(),
  });
}

/** 所有目录由可信宿主创建；候选工作区不得具有写入本目录的权限。 */
function privateDirectory(path: string): void {
  const stat = lstatSync(path);
  const uid = process.getuid?.();
  if (uid === undefined || !stat.isDirectory() || stat.isSymbolicLink() ||
      stat.uid !== uid || (stat.mode & 0o077) !== 0) {
    fail("LAB_DIRECTORY_NOT_PRIVATE");
  }
}

function rootPath(root: string): string {
  const path = resolve(root);
  privateDirectory(path);
  return path;
}

/** rename/mkdir之后同步父目录；不把原子可见性误当作已持久化的目录项。 */
function syncDirectory(path: string): void {
  const fd = openSync(path, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { fsyncSync(fd); } finally { closeSync(fd); }
}

function exclusiveJson(path: string, value: unknown): void {
  const fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    writeFileSync(fd, `${JSON.stringify(value)}\n`, "utf8");
    fsyncSync(fd);
  } finally { closeSync(fd); }
}

function readOwnerAt(directory: string): LabLockOwner {
  privateDirectory(directory);
  const fd = openSync(join(directory, "owner.json"), constants.O_RDONLY | constants.O_NOFOLLOW);
  let value: unknown;
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.nlink !== 1 || stat.size > 16_384 || stat.size === 0) {
      fail("LAB_LOCK_OWNER_INVALID");
    }
    value = JSON.parse(readFileSync(fd, "utf8"));
  } finally { closeSync(fd); }
  if (!value || typeof value !== "object" || Array.isArray(value)) fail("LAB_LOCK_OWNER_INVALID");
  const owner = value as Record<string, unknown>;
  if (Object.keys(owner).length !== OWNER_KEYS.length || OWNER_KEYS.some(key => !(key in owner)) ||
      owner.schema_version !== 1 || typeof owner.token !== "string" || !UUID.test(owner.token) ||
      !Number.isSafeInteger(owner.pid) || Number(owner.pid) < 1 || Number(owner.pid) > 2_147_483_647 ||
      typeof owner.hostname !== "string" || !/^[A-Za-z0-9._-]{1,253}$/.test(owner.hostname) ||
      typeof owner.boot_identity !== "string" || !/^(linux:[0-9a-f-]{36}|darwin:\d+:\d+)$/.test(owner.boot_identity) ||
      typeof owner.created_at !== "string" || !Number.isFinite(Date.parse(owner.created_at)) ||
      new Date(owner.created_at).toISOString() !== owner.created_at) {
    fail("LAB_LOCK_OWNER_INVALID");
  }
  return Object.freeze({ ...owner }) as unknown as LabLockOwner;
}

function sameOwner(actual: LabLockOwner, expected: LabLockOwner): boolean {
  return OWNER_KEYS.every(key => actual[key as keyof LabLockOwner] === expected[key as keyof LabLockOwner]);
}

/** 只允许删除自身唯一owner文件与空锁目录，不使用递归删除或按时间清理。 */
function removeOwnedDirectory(directory: string, expected: LabLockOwner): void {
  if (!sameOwner(readOwnerAt(directory), expected)) fail("LAB_LOCK_OWNERSHIP_CHANGED");
  const children = readdirSync(directory);
  if (children.length !== 1 || children[0] !== "owner.json") fail("LAB_LOCK_UNEXPECTED_CONTENT");
  unlinkSync(join(directory, "owner.json"));
  rmdirSync(directory);
  syncDirectory(dirname(directory));
}

function createOwnedDirectory(directory: string, owner: LabLockOwner, conflict: string): void {
  try { mkdirSync(directory, { mode: 0o700 }); }
  catch (error) {
    if (isErrno(error, "EEXIST")) fail(conflict);
    throw error;
  }
  // mkdir成功、owner持久化之前若进程崩溃，会留下无法自动确认的锁。
  // 此时选择人工核查，不能把缺owner当成“无人持锁”并删掉。
  exclusiveJson(join(directory, "owner.json"), owner);
  syncDirectory(directory);
  syncDirectory(dirname(directory));
}

/** 只读返回当前锁身份；缺失或损坏直接抛错，不尝试推断/回收。 */
export function readLabLockOwner(root: string): LabLockOwner {
  return readOwnerAt(join(rootPath(root), "writer.lock"));
}

/**
 * LabLedger.open → acquireLabLock → owner持久化；同一宿主只允许一个写者。
 * 恢复守卫前后各检查一次：消除“检查时没有恢复，mkdir时恢复已开始”的窗口。
 * 本锁只约束可信合作进程，不是分布式锁，也不能围栏外部Docker操作。
 */
export function acquireLabLock(root: string): LabLock {
  const path = rootPath(root);
  const writer = join(path, "writer.lock");
  const guard = join(path, "recovery.lock");
  if (entryExists(guard)) fail("LAB_RECOVERY_IN_PROGRESS");
  const owner = newOwner();
  createOwnedDirectory(writer, owner, "LAB_WRITER_BUSY");
  if (entryExists(guard)) {
    removeOwnedDirectory(writer, owner);
    fail("LAB_RECOVERY_IN_PROGRESS");
  }
  let released = false;
  return Object.freeze({
    owner,
    assertOwned(): void {
      if (released) fail("LAB_LOCK_RELEASED");
      if (entryExists(guard)) fail("LAB_RECOVERY_IN_PROGRESS");
      if (!sameOwner(readOwnerAt(writer), owner)) fail("LAB_LOCK_OWNERSHIP_CHANGED");
    },
    release(): void {
      if (released) return;
      removeOwnedDirectory(writer, owner);
      released = true;
    },
  });
}

/**
 * 显式恢复入口：expectedToken → 独占恢复守卫 → 同宿主/同启动周期/PID已死核验。
 * 只接受kill(pid,0)的ESRCH；PID复用、EPERM、宿主不同、缺失owner都拒绝。
 * 原锁整体移入唯一审计目录，保存确认意图和完成回执；不会递归删除旧锁。
 * 若恢复者自身崩溃留下recovery.lock，需人工核查，不自动偷取恢复守卫。
 */
export function recoverAbandonedLabLock(root: string, expectedToken: string): {
  recovered_owner: LabLockOwner;
  recovery_receipt_path: string;
} {
  if (!UUID.test(expectedToken)) fail("LAB_EXPECTED_LOCK_TOKEN_INVALID");
  const path = rootPath(root);
  const writer = join(path, "writer.lock");
  const guard = join(path, "recovery.lock");
  if (entryExists(guard)) fail("LAB_RECOVERY_IN_PROGRESS");
  const recoveryOwner = newOwner();
  createOwnedDirectory(guard, recoveryOwner, "LAB_RECOVERY_IN_PROGRESS");
  try {
    const owner = readOwnerAt(writer);
    if (owner.token !== expectedToken) fail("LAB_LOCK_TOKEN_MISMATCH");
    if (owner.hostname !== recoveryOwner.hostname || owner.boot_identity !== recoveryOwner.boot_identity) {
      fail("LAB_LOCK_HOST_UNVERIFIED");
    }
    try { process.kill(owner.pid, 0); }
    catch (error) {
      if (!isErrno(error, "ESRCH")) fail("LAB_OWNER_LIVENESS_UNKNOWN");
      return archiveAbandonedOwner(path, writer, owner, recoveryOwner);
    }
    return fail("LAB_OWNER_STILL_ALIVE");
  } finally {
    removeOwnedDirectory(guard, recoveryOwner);
  }
}

/** 调用者持有恢复守卫且已证明PID不存在；最后一次token比较后原子移走旧锁。 */
function archiveAbandonedOwner(root: string, writer: string, owner: LabLockOwner, recoveryOwner: LabLockOwner): {
  recovered_owner: LabLockOwner;
  recovery_receipt_path: string;
} {
  if (!sameOwner(readOwnerAt(writer), owner)) fail("LAB_LOCK_OWNERSHIP_CHANGED");
  const children = readdirSync(writer);
  if (children.length !== 1 || children[0] !== "owner.json") fail("LAB_LOCK_UNEXPECTED_CONTENT");
  const history = join(root, "lock-recovery");
  try { mkdirSync(history, { mode: 0o700 }); }
  catch (error) { if (!isErrno(error, "EEXIST")) throw error; }
  privateDirectory(history);
  const archive = join(history, recoveryOwner.token);
  mkdirSync(archive, { mode: 0o700 });
  syncDirectory(history);
  const context = {
    schema_version: 1, recovered_owner: owner, recovery_owner: recoveryOwner,
    observed_liveness: "ESRCH", observed_at: new Date().toISOString(),
  };
  exclusiveJson(join(archive, "intent.json"), { ...context, status: "DEAD_OWNER_CONFIRMED" });
  syncDirectory(archive);
  renameSync(writer, join(archive, "abandoned.lock"));
  syncDirectory(root);
  syncDirectory(archive);
  const receipt = join(archive, "receipt.json");
  exclusiveJson(receipt, { ...context, status: "RECOVERED", recovered_at: new Date().toISOString() });
  syncDirectory(archive);
  return { recovered_owner: owner, recovery_receipt_path: receipt };
}
