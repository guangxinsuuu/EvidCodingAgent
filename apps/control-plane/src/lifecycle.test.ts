import assert from "node:assert/strict";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import test, { type TestContext } from "node:test";

const entryUrl = new URL("./main.js", import.meta.url);
const entryPath = fileURLToPath(entryUrl);

interface ProcessResult {
  code: number | null;
  signal: NodeJS.Signals | null;
  stdout: string;
  stderr: string;
}

interface ProcessHandle {
  child: ChildProcessWithoutNullStreams;
  exited: Promise<ProcessResult>;
  output: () => { stdout: string; stderr: string };
}

/**
 * 测试 → 独立Node进程 → dist/main.js → 真实回环HTTP → 信号关闭。
 * 环境只继承启动所需字段，避免用户的EGAP_*、NODE_OPTIONS或代理改变测试。
 */
function childEnvironment(overrides: Record<string, string>): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {};
  for (const key of ["PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP"]) {
    const value = process.env[key];
    if (value !== undefined) environment[key] = value;
  }
  return { ...environment, ...overrides };
}

/** 为每次等待设上限；结束即清理计时器，避免测试自身挂住进程。 */
async function within<T>(promise: Promise<T>, milliseconds: number, label: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`${label}超时`)), milliseconds);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** 启动立即登记清理；断言失败或超时时也杀掉本测试创建的子进程。 */
function launch(t: TestContext, argv: string[], environment: Record<string, string>): ProcessHandle {
  const child = spawn(process.execPath, argv, {
    env: childEnvironment(environment),
    stdio: ["pipe", "pipe", "pipe"],
  });
  child.stdin.end();
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8").on("data", (chunk: string) => { stdout += chunk; });
  child.stderr.setEncoding("utf8").on("data", (chunk: string) => { stderr += chunk; });
  const exited = new Promise<ProcessResult>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) => resolve({ code, signal, stdout, stderr }));
  });
  // 清理钩子也观察失败，避免spawn错误在等待监听之前成为未处理拒绝。
  void exited.catch(() => undefined);
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    await within(exited, 3_000, "子进程清理");
  });
  return { child, exited, output: () => ({ stdout, stderr }) };
}

/** 日志可能含框架行；这里只读取结构化事件，不依赖人类日志文字。 */
function events(output: string): Record<string, unknown>[] {
  return output.split("\n").flatMap((line) => {
    try {
      const value: unknown = JSON.parse(line);
      return value !== null && typeof value === "object" && !Array.isArray(value)
        ? [value as Record<string, unknown>]
        : [];
    } catch {
      return [];
    }
  });
}

class StartupFailure extends Error {
  constructor(readonly result: ProcessResult) {
    super(`监听前进程退出：${result.stderr}`);
  }
}

/** 监听事件与退出并行观察；先注册再检查缓冲，避免快速启动丢事件。 */
async function waitForListening(handle: ProcessHandle): Promise<Record<string, unknown>> {
  let onOutput: (() => void) | undefined;
  try {
    return await within(new Promise<Record<string, unknown>>((resolve, reject) => {
      const inspect = (): void => {
        const event = events(handle.output().stdout).find((item) => item["event"] === "listening");
        if (event) resolve(event);
      };
      onOutput = inspect;
      handle.child.stdout.on("data", inspect);
      inspect();
      void handle.exited.then((result) => reject(new StartupFailure(result)), reject);
    }), 5_000, "等待监听事件");
  } finally {
    if (onOutput) handle.child.stdout.off("data", onOutput);
  }
}

/** 用OS分配临时端口并立即释放；指定port时证明旧服务确实释放了该端口。 */
async function availablePort(port = 0): Promise<number> {
  const server = createServer();
  try {
    await within(new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen({ host: "127.0.0.1", port, exclusive: true }, resolve);
    }), 3_000, "分配回环端口");
    const address = server.address();
    assert.ok(address && typeof address !== "string");
    return address.port;
  } finally {
    if (server.listening) {
      await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }
  }
}

function launchServer(t: TestContext, port: number): ProcessHandle {
  return launch(t, [entryPath], {
    EGAP_MODE: "local-dev", EGAP_HOST: "127.0.0.1", EGAP_PORT: String(port), EGAP_LOG_LEVEL: "silent",
  });
}

/** 仅对释放临时端口后的真实EADDRINUSE竞态重试，其他启动错误不得掩盖。 */
async function startOnAvailablePort(t: TestContext): Promise<{ handle: ProcessHandle; port: number }> {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const port = await availablePort();
    const handle = launchServer(t, port);
    try {
      const event = await waitForListening(handle);
      assert.equal(event["address"], `http://127.0.0.1:${port}`);
      return { handle, port };
    } catch (error) {
      const addressRace = error instanceof StartupFailure
        && events(error.result.stderr).some((event) => event["reason"] === "ADDRESS_IN_USE");
      if (!addressRace || attempt === 3) throw error;
      t.diagnostic(`第${attempt}次临时端口被其他进程占用，保留失败后重新分配。`);
    }
  }
  throw new Error("无法分配测试端口");
}

test("CLI配置失败以非零退出，禁止公网绑定并隐藏原始凭据", { timeout: 15_000 }, async (t) => {
  const cases: Array<{ environment: Record<string, string>; field: string; reason: string }> = [
    { environment: {}, field: "EGAP_MODE", reason: "REQUIRED" },
    { environment: { EGAP_MODE: "local-dev", EGAP_PORT: "4317", EGAP_HOST: "0.0.0.0" }, field: "EGAP_HOST", reason: "LOOPBACK_REQUIRED" },
    { environment: { EGAP_MODE: "local-dev", EGAP_PORT: "4317", EGAP_DATABASE_REF: "postgres://user:lifecycle-secret@host/db" }, field: "EGAP_DATABASE_REF", reason: "INVALID_REFERENCE" },
  ];
  for (const scenario of cases) {
    const handle = launch(t, [entryPath], scenario.environment);
    const result = await within(handle.exited, 4_000, "配置拒绝进程退出");
    assert.equal(result.code, 1);
    assert.equal(result.signal, null);
    assert.equal(events(result.stdout).some((event) => event["event"] === "listening"), false);
    assert.deepEqual(events(result.stderr), [{ event: "startup_failed", error_code: "INVALID_CONFIGURATION", field: scenario.field, reason: scenario.reason }]);
    assert.equal((result.stdout + result.stderr).includes("lifecycle-secret"), false);
  }
});

test("导入main不读配置、不挂信号处理器且进程可自然退出", { timeout: 6_000 }, async (t) => {
  const script = `const before = [process.listenerCount('SIGINT'), process.listenerCount('SIGTERM')];
    await import(${JSON.stringify(entryUrl.href)});
    process.stdout.write(JSON.stringify({event:'imported', before,
      after:[process.listenerCount('SIGINT'),process.listenerCount('SIGTERM')]})+String.fromCharCode(10));`;
  const handle = launch(t, ["--input-type=module", "--eval", script], {});
  const result = await within(handle.exited, 4_000, "无副作用导入");
  assert.equal(result.code, 0);
  assert.equal(result.signal, null);
  assert.equal(result.stderr, "");
  const imported = events(result.stdout);
  assert.equal(imported.length, 1);
  assert.equal(imported[0]?.["event"], "imported");
  assert.deepEqual(imported[0]?.["before"], imported[0]?.["after"]);
});

test("真实HTTP及SIGTERM关闭释放端口，同端口重启后SIGINT可退出", { timeout: 25_000 }, async (t) => {
  const { handle, port } = await startOnAvailablePort(t);
  const base = `http://127.0.0.1:${port}`;
  const live = await fetch(`${base}/health/live`, { signal: AbortSignal.timeout(3_000) });
  assert.equal(live.status, 200);
  assert.deepEqual(await live.json(), { status: "alive", mode: "local-dev" });
  const ready = await fetch(`${base}/health/ready`, { signal: AbortSignal.timeout(3_000) });
  assert.equal(ready.status, 200);
  const readiness = await ready.json() as Record<string, unknown>;
  assert.equal(readiness["scope"], "control-plane-skeleton");
  assert.equal(readiness["business_verification"], "not_implemented");
  const missing = await fetch(`${base}/runs`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ actor: "admin", tenant_id: "other-tenant" }), signal: AbortSignal.timeout(3_000),
  });
  assert.equal(missing.status, 404);
  assert.equal((await missing.json() as Record<string, unknown>)["error_code"], "ROUTE_NOT_FOUND");
  t.diagnostic(`真实回环地址=${base}；live=${live.status}，ready=${ready.status}，POST /runs=${missing.status}。`);

  assert.equal(handle.child.kill("SIGTERM"), true);
  const first = await within(handle.exited, 7_000, "SIGTERM优雅退出");
  assert.equal(first.code, 0);
  assert.equal(first.signal, null);
  assert.deepEqual(events(first.stdout).filter((event) => event["event"] === "stopped"), [{ event: "stopped", signal: "SIGTERM" }]);
  assert.equal(await availablePort(port), port);
  t.diagnostic(`SIGTERM退出码=${first.code}；端口${port}可重新绑定。`);

  const restarted = launchServer(t, port);
  await waitForListening(restarted);
  assert.equal(restarted.child.kill("SIGINT"), true);
  const second = await within(restarted.exited, 7_000, "SIGINT优雅退出");
  assert.equal(second.code, 0);
  assert.equal(second.signal, null);
  assert.deepEqual(events(second.stdout).filter((event) => event["event"] === "stopped"), [{ event: "stopped", signal: "SIGINT" }]);
  assert.equal(await availablePort(port), port);
  t.diagnostic(`同端口重启后的SIGINT退出码=${second.code}；端口${port}再次释放。`);
});
