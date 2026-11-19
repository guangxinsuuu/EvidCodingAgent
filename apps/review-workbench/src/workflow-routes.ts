import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { ReviewError, type Actor } from "./types.js";
import type { WorkflowStore } from "./workflow-store.js";

function actor(request: FastifyRequest): Actor { if (!request.reviewActor) throw new ReviewError("UNAUTHENTICATED", 401); return request.reviewActor; }
function parameter(request: FastifyRequest, key = "id"): string {
  const value = (request.params as Record<string, unknown>)[key];
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/.test(value)) throw new ReviewError("NOT_FOUND", 404); return value;
}
type Handler = (request: FastifyRequest, reply: FastifyReply) => Promise<unknown>;
function safe(handler: Handler): Handler { return async (request, reply) => {
  reply.header("Cache-Control", "no-store");
  try { return await handler(request, reply); } catch (error) { return reply.code(error instanceof ReviewError ? error.status : 500).send({ error_code: error instanceof ReviewError ? error.code : "INTERNAL_ERROR" }); }
}; }
/** createReviewServer 的 session/CSRF/Origin 前置钩子保护所有 v2 路由；请求正文不能指定 actor。 */
export function registerWorkflowRoutes(app: FastifyInstance, store: WorkflowStore): void {
  app.get("/api/v2/me/review-tasks", safe(async r => store.listTasks(actor(r))));
  app.get("/api/v2/review-tasks/:id", safe(async r => store.getTask(parameter(r), actor(r))));
  app.get("/api/v2/review-cases/:id/materials/:materialRef", safe(async r => store.getMaterial(parameter(r), parameter(r, "materialRef"), actor(r))));
  app.post("/api/v2/review-tasks/:id/claim", safe(async r => store.claim(parameter(r), actor(r), r.body)));
  app.post("/api/v2/review-tasks/:id/reassign", safe(async r => store.reassign(parameter(r), actor(r), r.body)));
  app.post("/api/v2/review-tasks/:id/evidence-requests", safe(async r => store.requestEvidence(parameter(r), actor(r), r.body)));
  app.post("/api/v2/evidence-requests/:id/accept", safe(async r => store.acceptResponse(parameter(r), actor(r), r.body)));
  app.post("/api/v2/evidence-requests/:id/withdraw", safe(async r => store.withdraw(parameter(r), actor(r), r.body)));
  app.post("/api/v2/evidence-requests/:id/clarify", safe(async r => store.clarify(parameter(r), actor(r), r.body)));
}
