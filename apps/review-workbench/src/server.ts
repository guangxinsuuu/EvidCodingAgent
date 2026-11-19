import Fastify, { type FastifyInstance } from "fastify";
import { readFile } from "node:fs/promises";
import { LocalSessions, sessionCookie, type LocalIdentity } from "./auth.js";
import { registerReviewRoutes } from "./routes.js";
import type { ReviewStore } from "./store.js";
import type { WorkflowStore } from "./workflow-store.js";
import { registerWorkflowRoutes } from "./workflow-routes.js";

export interface WebOptions { origin: string; identities: LocalIdentity[]; assets?: URL; workflow?: WorkflowStore }

/**
 * main → createReviewServer → 认证/CSRF → registerReviewRoutes → ReviewStore。
 * 静态页面和 API 同源；GET 仅查询审查投影，绝不调用会 drainReads 的 Runtime.projection。
 * 独立入口使现有 Run token 协议与控制面健康接口保持原语义。
 */
export function createReviewServer(store: ReviewStore, options: WebOptions): FastifyInstance {
  const origin = new URL(options.origin);
  if (origin.protocol !== "http:" || origin.hostname !== "127.0.0.1" || origin.pathname !== "/") throw new Error("LOCAL_LOOPBACK_ORIGIN_REQUIRED");
  const app = Fastify({ logger: false, trustProxy: false,
    bodyLimit: 16_384, requestTimeout: 10_000, connectionTimeout: 10_000, forceCloseConnections: "idle" });
  const sessions = new LocalSessions(options.identities);
  const assets = options.assets ?? new URL("../public/", import.meta.url);
  app.decorateRequest("reviewActor", null);
  const publicPaths = new Set(["/", "/app.js", "/styles.css", "/health/live"]);

  app.addHook("onRequest", async (request, reply) => {
    reply.header("cache-control", "no-store").header("x-content-type-options", "nosniff")
      .header("referrer-policy", "no-referrer").header("x-frame-options", "DENY")
      .header("content-security-policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
    // Host 固定，阻止指向回环服务的 DNS rebinding；不信任 X-Forwarded-*。
    if (request.headers.host !== origin.host) return reply.code(403).send({ error_code: "INVALID_HOST" });
    if (request.method !== "GET" && request.method !== "HEAD") {
      if (request.headers.origin !== origin.origin || request.headers["sec-fetch-site"] === "cross-site") {
        return reply.code(403).send({ error_code: "ORIGIN_REJECTED" });
      }
      if (!request.headers["content-type"]?.startsWith("application/json")) return reply.code(415).send({ error_code: "JSON_REQUIRED" });
    }
  });

  app.addHook("preHandler", async (request, reply) => {
    const pathname = request.url.split("?")[0]!;
    if (publicPaths.has(pathname) || (pathname === "/api/v1/session" && request.method === "POST")) return;
    const session = sessions.get(sessionCookie(request.headers.cookie));
    if (!session) return reply.code(401).send({ error_code: "LOGIN_REQUIRED" });
    request.reviewActor = session.actor;
    if (request.method !== "GET" && request.method !== "HEAD" && !sessions.csrfMatches(session, request.headers["x-csrf-token"])) {
      return reply.code(403).send({ error_code: "CSRF_REJECTED" });
    }
  });

  app.post("/api/v1/session", async (request, reply) => {
    const body = request.body;
    if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).length !== 1 || !("code" in body) || typeof body.code !== "string") {
      return reply.code(400).send({ error_code: "INVALID_LOGIN" });
    }
    const result = sessions.login(body.code, request.ip);
    if (!result) return reply.code(401).send({ error_code: "LOGIN_REJECTED" });
    sessions.logout(sessionCookie(request.headers.cookie));
    reply.header("set-cookie", `egap_review_session=${result.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800`);
    return { actor: result.session.actor, csrf_token: result.session.csrf_token, mode: "LOCAL_EXPERIMENTAL_IDENTITIES" };
  });
  app.get("/api/v1/session", async request => {
    const session = sessions.get(sessionCookie(request.headers.cookie))!;
    return { actor: session.actor, csrf_token: session.csrf_token, mode: "LOCAL_EXPERIMENTAL_IDENTITIES" };
  });
  app.post("/api/v1/logout", async (request, reply) => {
    sessions.logout(sessionCookie(request.headers.cookie));
    reply.header("set-cookie", "egap_review_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0");
    return { logged_out: true };
  });
  app.get("/health/live", async () => ({ status: "alive", scope: "local-review-workbench", agent_dispatch: false, release_authority: false }));
  for (const [path, file, type] of [["/", "index.html", "text/html; charset=utf-8"], ["/app.js", "app.js", "text/javascript; charset=utf-8"], ["/styles.css", "styles.css", "text/css; charset=utf-8"]]) {
    app.get(path!, async (_request, reply) => reply.type(type!).send(await readFile(new URL(file!, assets))));
  }
  registerReviewRoutes(app, store);
  if (options.workflow) registerWorkflowRoutes(app, options.workflow);
  app.setErrorHandler((error, _request, reply) => {
    const status = typeof (error as { statusCode?: unknown }).statusCode === "number" ? (error as { statusCode: number }).statusCode : 500;
    void reply.code(status >= 400 && status < 500 ? status : 500).send({ error_code: status < 500 ? "INVALID_REQUEST" : "INTERNAL_ERROR" });
  });
  return app;
}
