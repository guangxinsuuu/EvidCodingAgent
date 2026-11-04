import { pathToFileURL } from "node:url";
import { ConfigurationError, parseConfig } from "./config.js";
import { createServer } from "./server.js";

/**
 * 唯一进程入口：parseConfig → createServer → listen → SIGINT/SIGTERM → close。
 * 导入本模块不启动任何服务。配置失败发生在建服务之前，监听失败清理已建句柄。
 */
export async function main(): Promise<void> {
  const config = parseConfig(process.env);
  const app = createServer(config);
  let address: string;
  try {
    address = await app.listen({ host: config.host, port: config.port });
  } catch (error) {
    await app.close();
    throw error;
  }

  let closing: Promise<void> | undefined;
  /** 两种信号可能相继到达；共享关闭Promise，确保close和回执只执行一次。 */
  const stop = (signal: NodeJS.Signals): void => {
    if (closing) return;
    closing = (async () => {
      const timeout = setTimeout(() => {
        process.stderr.write(JSON.stringify({ event: "shutdown_failed", error_code: "SHUTDOWN_TIMEOUT" }) + "\n");
        process.exit(1);
      }, 5_000);
      timeout.unref();
      try {
        await app.close();
        process.stdout.write(JSON.stringify({ event: "stopped", signal }) + "\n");
      } catch {
        process.stderr.write(JSON.stringify({ event: "shutdown_failed", error_code: "SHUTDOWN_FAILED" }) + "\n");
        process.exitCode = 1;
      } finally {
        clearTimeout(timeout);
        process.off("SIGINT", onSigint);
        process.off("SIGTERM", onSigterm);
      }
    })();
  };
  const onSigint = (): void => stop("SIGINT");
  const onSigterm = (): void => stop("SIGTERM");
  process.on("SIGINT", onSigint);
  process.on("SIGTERM", onSigterm);
  process.stdout.write(JSON.stringify({ event: "listening", address, mode: config.mode, scope: "control-plane-skeleton" }) + "\n");
}

/** 只保留安全原因分类，禁止将数据库URL、环境原值或任意异常全文写进日志。 */
function reportStartupFailure(error: unknown): void {
  const details = error instanceof ConfigurationError
    ? { error_code: error.error_code, field: error.field, reason: error.reason }
    : { error_code: "STARTUP_FAILED", reason: error instanceof Error && "code" in error && error.code === "EADDRINUSE" ? "ADDRESS_IN_USE" : "LISTEN_OR_INITIALIZATION_FAILED" };
  process.stderr.write(JSON.stringify({ event: "startup_failed", ...details }) + "\n");
  process.exitCode = 1;
}

// 路径经URL规范化后比较；测试/其它模块import不注册信号、不读取配置、不监听端口。
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void main().catch(reportStartupFailure);
}
