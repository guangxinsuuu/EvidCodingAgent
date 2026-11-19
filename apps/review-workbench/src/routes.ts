import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { ReviewStore } from "./store.js";
import { ReviewError, type Actor } from "./types.js";

declare module "fastify" { interface FastifyRequest { reviewActor: Actor | null } }

const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
function actor(request: FastifyRequest): Actor {
  if (!request.reviewActor) throw new ReviewError("UNAUTHENTICATED", 401);
  return request.reviewActor;
}
function id(request: FastifyRequest): string {
  const value = (request.params as { id?: unknown }).id;
  if (typeof value !== "string" || !ID.test(value)) throw new ReviewError("NOT_FOUND", 404);
  return value;
}
type Handler = (request: FastifyRequest, reply: FastifyReply) => Promise<unknown>;

/** 错误只返回固定代码；PG 异常、请求正文与凭据不进入浏览器。 */
function safe(handler: Handler): Handler {
  return async (request, reply) => {
    reply.header("Cache-Control", "no-store");
    try { return await handler(request, reply); }
    catch (error) {
      const status = error instanceof ReviewError ? error.status : 500;
      const error_code = error instanceof ReviewError ? error.code : "INTERNAL_ERROR";
      return reply.code(status).send({ error_code });
    }
  };
}

/**
 * createReviewServer → 会话/CSRF preHandler → 此处路由 → ReviewStore。
 * GET 永远只读；POST 只记录审查命令，不借页面刷新运行模型，也不直接派发业务动作。
 * actor 仅取服务端会话。CLI 使用同一认证/API，不能通过 body.actor 切换审查人。
 */
export function registerReviewRoutes(app: FastifyInstance, store: ReviewStore): void {
  app.get("/api/v1/me", safe(async request => ({ actor: actor(request) })));
  app.get("/api/v1/me/review-tasks", safe(async request => store.listTasks(actor(request))));
  app.get("/api/v1/review-cases/:id", safe(async request => store.getCase(id(request), actor(request))));
  app.get("/api/v1/evidence/:id", safe(async request => store.getEvidence(id(request), actor(request))));
  app.post("/api/v1/review-cases/:id/decisions", safe(async request => store.decide(id(request), actor(request), request.body)));
  app.post("/api/v1/review-cases/:id/evidence-requests", safe(async request => store.requestEvidence(id(request), actor(request), request.body)));
}
