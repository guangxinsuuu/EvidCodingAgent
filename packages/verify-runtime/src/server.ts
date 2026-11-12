import Fastify from "fastify";
import { DecisionProposalError } from "@egap/contracts";
import { Runtime } from "./runtime.js";
import { RuntimeError, requireThat } from "./types.js";

/** 独立的 V0.1 Runtime 入口；不把原 control-plane 骨架的 ready 改成业务已通过。 */
export function createVerifyServer(runtime: Runtime) {
  const app = Fastify({ logger: false, disableRequestLogging: true, trustProxy: false,
    bodyLimit: 24_576, requestTimeout: 200_000, connectionTimeout: 200_000 });
  app.addHook("preHandler", async request => {
    const params = request.params as { id?: string };
    requireThat(params.id, "ROUTE_NOT_FOUND", 404);
    const header = request.headers.authorization ?? "";
    requireThat(header.startsWith("Bearer "), "ACCESS_DENIED", 403);
    await runtime.authorize(params.id, header.slice(7));
  });
  app.get<{ Params: { id: string } }>("/runs/:id", request => runtime.projection(request.params.id));
  app.post<{ Params: { id: string }; Body: { command_id: string; based_on_decision_revision: number } }>("/runs/:id/decide", request => {
    requireThat(request.body && Object.keys(request.body).length === 2, "INVALID_DECISION_COMMAND", 400);
    return runtime.decide(request.params.id, request.body);
  });
  app.post<{ Params: { id: string } }>("/runs/:id/proposals", request => runtime.submit(request.params.id, request.body));
  app.get<{ Params: { id: string; operationId: string } }>("/runs/:id/operations/:operationId", request => runtime.operation(request.params.id, request.params.operationId));
  // 调用者可停止本次调查，不能通过同一 token 开新 run、改候选、改预算或提交运行回执。
  app.post<{ Params: { id: string } }>("/runs/:id/cancel", request => runtime.cancel(request.params.id));
  app.setErrorHandler((error, _request, reply) => {
    if (error instanceof RuntimeError) return reply.code(error.statusCode).send({ error_code: error.code });
    if (error instanceof DecisionProposalError) return reply.code(400).send({ error_code: error.error_code });
    const status = error && typeof error === "object" && "statusCode" in error && typeof error.statusCode === "number" ? error.statusCode : 500;
    return reply.code(status >= 400 && status < 500 ? status : 500).send({ error_code: status < 500 ? "INVALID_REQUEST" : "INTERNAL_ERROR" });
  });
  return app;
}
