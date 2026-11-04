import Fastify, { type FastifyInstance } from "fastify";
import { ContractError } from "@egap/contracts";
import type { ControlPlaneConfig } from "./config.js";

/**
 * main.ts → createServer → 注册健康路由；本函数不会调用 listen 或连接DB。
 * 测试可用 inject 验证同一路由，进程入口才有监听/停止权限。
 * 当前只提供回环上的公开健康信息，尚无需要 actor/tenant 的业务入口。
 */
export function createServer(config: ControlPlaneConfig): FastifyInstance {
  const app = Fastify({
    logger: config.logLevel === "silent" ? false : { level: config.logLevel },
    // 请求内容/身份尚无脱敏协议，因此关闭自动请求日志，也不信任代理头。
    disableRequestLogging: true,
    requestIdHeader: false,
    trustProxy: false,
    bodyLimit: 16_384,
    requestTimeout: 10_000,
    connectionTimeout: 10_000,
    keepAliveTimeout: 5_000,
    return503OnClosing: true,
    forceCloseConnections: "idle",
  });

  app.get("/health/live", async () => ({ status: "alive", mode: config.mode }));
  app.get("/health/ready", async () => ({
    status: "ready",
    mode: config.mode,
    scope: "control-plane-skeleton",
    // 本期必需条件仅为已校验配置和已注册路由；空列表不表示DB检查通过。
    checks: { configuration: "passed", http_routes: "registered" },
    required_external_dependencies: [],
    business_verification: "not_implemented",
    database_connection: "not_implemented",
  }));

  // /runs 尚未实现；不接受 body.actor 作为认证，也不产生模拟Run或成功响应。
  app.setNotFoundHandler(async (_request, reply) => {
    return reply.code(404).send({
      error_code: "ROUTE_NOT_FOUND", retryable: false, operation_ref: null,
      message: "请求的接口尚未提供。",
    });
  });
  app.setErrorHandler((error, _request, reply) => {
    // JSON解析错误可能包含原始请求片段；只输出固定安全信息。
    if (error !== null && typeof error === "object" && "statusCode" in error && typeof error.statusCode === "number" && error.statusCode >= 400 && error.statusCode < 500) {
      void reply.code(error.statusCode).send({
        error_code: "INVALID_REQUEST", retryable: false, operation_ref: null,
        message: "请求格式不合法。",
      });
      return;
    }
    app.log.error({ event: "request_failed", error_code: "INTERNAL_ERROR" }, "请求执行失败");
    void reply.code(500).send(new ContractError("INTERNAL_ERROR").toPublicError());
  });
  return app;
}
