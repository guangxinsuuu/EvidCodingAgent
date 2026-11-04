import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync,
  readdirSync, rmSync, rmdirSync, symlinkSync, unlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { TestContext } from "node:test";
import {
  acquireLabLock, LabLockError, readLabLockOwner, recoverAbandonedLabLock,
} from "./lab-lock.js";
import type { LabLockOwner } from "./lab-lock.js";

function fixture(t: TestContext): string {
  const root = mkdtempSync(join(tmpdir(), "egap-lab-lock-test-"));
  chmodSync(root, 0o700);
  // 仅清理当前测试mkdtemp生成的唯一目录；生产锁清理不使用递归rm。
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}

function hasCode(code: string): (error: unknown) => boolean {
  return error => error instanceof LabLockError && error.code === code;
}

interface ChildResult { ok: boolean; owner?: LabLockOwner; code?: string }

/** 真正独立的Node进程竞争同一个锁，避免用单进程顺序调用冒充并发验收。 */
function child(root: string, mode: "hold" | "recover", token?: string): {
  process: ChildProcess;
  result: Promise<ChildResult>;
  exited: Promise<void>;
} {
  const moduleUrl = new URL("./lab-lock.js", import.meta.url).href;
  const program = `
    import { acquireLabLock, recoverAbandonedLabLock } from ${JSON.stringify(moduleUrl)};
    try {
      if (process.env.EGAP_LOCK_TEST_MODE === "recover") {
        const receipt = recoverAbandonedLabLock(process.env.EGAP_LOCK_TEST_ROOT, process.env.EGAP_LOCK_TEST_TOKEN);
        process.send({ok:true, owner:receipt.recovered_owner});
        process.disconnect();
      } else {
        const lock = acquireLabLock(process.env.EGAP_LOCK_TEST_ROOT);
        process.send({ok:true, owner:lock.owner});
        process.on("message", message => {
          if (message === "release") { lock.release(); process.disconnect(); }
        });
      }
    } catch (error) {
      process.send({ok:false, code:error.code || error.name});
      process.disconnect();
    }
  `;
  const processHandle = spawn(process.execPath, ["--input-type=module", "-e", program], {
    env: { ...process.env, EGAP_LOCK_TEST_ROOT: root, EGAP_LOCK_TEST_MODE: mode,
      ...(token === undefined ? {} : { EGAP_LOCK_TEST_TOKEN: token }) },
    stdio: ["ignore", "ignore", "pipe", "ipc"],
  });
  const exited = new Promise<void>((resolve, reject) => {
    processHandle.once("exit", () => resolve());
    processHandle.once("error", reject);
  });
  const result = new Promise<ChildResult>((resolve, reject) => {
    const timeout = setTimeout(() => {
      processHandle.kill("SIGKILL");
      reject(new Error("Lock test child did not respond."));
    }, 8_000);
    processHandle.once("message", value => {
      clearTimeout(timeout);
      resolve(value as ChildResult);
    });
    processHandle.once("error", error => { clearTimeout(timeout); reject(error); });
    processHandle.once("exit", () => {
      clearTimeout(timeout);
      reject(new Error("Lock test child exited before a response."));
    });
  });
  return { process: processHandle, result, exited };
}

test("锁持久化身份、排他写入、释放幂等且旧handle拒绝", t => {
  const root = fixture(t);
  const first = acquireLabLock(root);
  assert.deepEqual(readLabLockOwner(root), first.owner);
  assert.equal(first.owner.pid, process.pid);
  assert.equal(Object.isFrozen(first.owner), true);
  first.assertOwned();
  assert.throws(() => acquireLabLock(root), hasCode("LAB_WRITER_BUSY"));
  first.release();
  first.release();
  assert.throws(() => first.assertOwned(), hasCode("LAB_LOCK_RELEASED"));
  const next = acquireLabLock(root);
  assert.notEqual(first.owner.token, next.owner.token);
  next.release();
});

test("两个真实写进程竞争时恰有一个取得锁", { timeout: 15_000 }, async t => {
  const root = fixture(t);
  const children = [child(root, "hold"), child(root, "hold")];
  t.after(() => children.forEach(item => item.process.kill("SIGKILL")));
  const results = await Promise.all(children.map(item => item.result));
  assert.equal(results.filter(result => result.ok).length, 1);
  assert.equal(results.find(result => !result.ok)?.code, "LAB_WRITER_BUSY");
  const index = results.findIndex(result => result.ok);
  children[index]!.process.send("release");
  await Promise.all(children.map(item => item.exited));
  assert.equal(existsSync(join(root, "writer.lock")), false);
});

test("真实进程被杀后按token恢复，旧owner与完成回执保留", { timeout: 15_000 }, async t => {
  const root = fixture(t);
  const holder = child(root, "hold");
  t.after(() => holder.process.kill("SIGKILL"));
  const result = await holder.result;
  assert.equal(result.ok, true);
  const owner = result.owner!;
  holder.process.kill("SIGKILL");
  await holder.exited;
  const recovered = recoverAbandonedLabLock(root, owner.token);
  assert.deepEqual(recovered.recovered_owner, owner);
  const receipt = JSON.parse(readFileSync(recovered.recovery_receipt_path, "utf8")) as Record<string, unknown>;
  assert.equal(receipt.status, "RECOVERED");
  assert.deepEqual(receipt.recovered_owner, owner);
  assert.equal(existsSync(join(root, "writer.lock")), false);
  assert.equal(existsSync(join(root, "recovery.lock")), false);
  const next = acquireLabLock(root);
  assert.notEqual(next.owner.token, owner.token);
  next.release();
});

test("两个恢复进程不能同时回收同一死锁", { timeout: 15_000 }, async t => {
  const root = fixture(t);
  const holder = child(root, "hold");
  t.after(() => holder.process.kill("SIGKILL"));
  const held = await holder.result;
  assert.equal(held.ok, true);
  holder.process.kill("SIGKILL");
  await holder.exited;
  const recoverers = [child(root, "recover", held.owner!.token), child(root, "recover", held.owner!.token)];
  t.after(() => recoverers.forEach(item => item.process.kill("SIGKILL")));
  const results = await Promise.all(recoverers.map(item => item.result));
  await Promise.all(recoverers.map(item => item.exited));
  assert.equal(results.filter(result => result.ok).length, 1);
  assert.equal(readdirSync(join(root, "lock-recovery")).length, 1);
  assert.equal(existsSync(join(root, "recovery.lock")), false);
});

test("活进程、错误token、异宿主或不同启动身份均不能被回收", t => {
  const root = fixture(t);
  const lock = acquireLabLock(root);
  assert.throws(() => recoverAbandonedLabLock(root, randomUUID()), hasCode("LAB_LOCK_TOKEN_MISMATCH"));
  assert.throws(() => recoverAbandonedLabLock(root, lock.owner.token), hasCode("LAB_OWNER_STILL_ALIVE"));
  const ownerPath = join(root, "writer.lock", "owner.json");
  writeFileSync(ownerPath, JSON.stringify({ ...lock.owner, hostname: "different-host" }));
  assert.throws(() => recoverAbandonedLabLock(root, lock.owner.token), hasCode("LAB_LOCK_HOST_UNVERIFIED"));
  writeFileSync(ownerPath, JSON.stringify({ ...lock.owner, boot_identity: "linux:00000000-0000-0000-0000-000000000000" }));
  assert.throws(() => recoverAbandonedLabLock(root, lock.owner.token), hasCode("LAB_LOCK_HOST_UNVERIFIED"));
  writeFileSync(ownerPath, JSON.stringify(lock.owner));
  assert.equal(existsSync(join(root, "recovery.lock")), false);
  lock.release();
});

test("恢复守卫和不完整owner保留并拒绝自动偷锁", t => {
  const root = fixture(t);
  mkdirSync(join(root, "recovery.lock"), { mode: 0o700 });
  assert.throws(() => acquireLabLock(root), hasCode("LAB_RECOVERY_IN_PROGRESS"));
  assert.throws(() => recoverAbandonedLabLock(root, randomUUID()), hasCode("LAB_RECOVERY_IN_PROGRESS"));
  rmdirSync(join(root, "recovery.lock"));
  symlinkSync(join(root, "missing-target"), join(root, "recovery.lock"));
  assert.throws(() => acquireLabLock(root), hasCode("LAB_RECOVERY_IN_PROGRESS"));
  unlinkSync(join(root, "recovery.lock"));
  mkdirSync(join(root, "writer.lock"), { mode: 0o700 });
  assert.throws(() => recoverAbandonedLabLock(root, randomUUID()));
  assert.equal(existsSync(join(root, "writer.lock")), true);
  assert.equal(existsSync(join(root, "recovery.lock")), false);
});

test("释放前核对token，拒绝删除已变化或含额外文件的目录", t => {
  const root = fixture(t);
  const lock = acquireLabLock(root);
  const ownerPath = join(root, "writer.lock", "owner.json");
  writeFileSync(ownerPath, JSON.stringify({ ...lock.owner, token: randomUUID() }));
  assert.throws(() => lock.release(), hasCode("LAB_LOCK_OWNERSHIP_CHANGED"));
  assert.equal(existsSync(ownerPath), true);
  writeFileSync(ownerPath, JSON.stringify(lock.owner));
  const extra = join(root, "writer.lock", "unrecognized-file");
  writeFileSync(extra, "preserve me");
  assert.throws(() => lock.release(), hasCode("LAB_LOCK_UNEXPECTED_CONTENT"));
  assert.equal(readFileSync(extra, "utf8"), "preserve me");
  rmSync(extra);
  lock.release();
});

test("非私有根目录被拒绝", t => {
  const root = fixture(t);
  chmodSync(root, 0o755);
  assert.throws(() => acquireLabLock(root), hasCode("LAB_DIRECTORY_NOT_PRIVATE"));
  chmodSync(root, 0o700);
});
