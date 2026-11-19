import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { LocalSessions, sessionCookie } from "./auth.js";
import { createReviewServer } from "./server.js";
import type { ReviewStore } from "./store.js";
import { assertLocalDatabase } from "./config.js";

const code = "only-test-login-code";
const identity = { actor: { id: "qa-test", display_name: "实验QA", role: "QA" as const, tenant_id: "tenant-b" }, code_hash: createHash("sha256").update(code).digest("hex") };
const origin = "http://127.0.0.1:4174";
const headers = { host: "127.0.0.1:4174", origin, "content-type": "application/json" };

test("local DB validation rejects pg query host override", () => {
  assert.doesNotThrow(() => assertLocalDatabase("postgres://127.0.0.1/test"));
  for (const url of ["postgres://127.0.0.1/test?host=example.invalid", "postgres://127.0.0.1/test?host=/tmp/foreign", "postgres://example.invalid/test", "postgres://127.0.0.1/test#fragment"]) assert.throws(() => assertLocalDatabase(url));
});

test("sessions expire, logout revokes, repeated cookie names fail closed", () => {
  let now = 0;
  const sessions = new LocalSessions([identity], () => now);
  const first = sessions.login(code, "local")!;
  assert.equal(sessions.get(first.token)?.actor.id, identity.actor.id);
  assert.equal(sessions.csrfMatches(first.session, "wrong"), false);
  sessions.logout(first.token); assert.equal(sessions.get(first.token), null);
  const second = sessions.login(code, "local")!;
  now = 9 * 60 * 60_000; assert.equal(sessions.get(second.token), null);
  assert.equal(sessionCookie("egap_review_session=a; egap_review_session=b"), undefined);
});

test("login is rate limited and accepts no arbitrary role", () => {
  const sessions = new LocalSessions([identity]);
  for (let i = 0; i < 10; i++) assert.equal(sessions.login("wrong", "local"), null);
  assert.equal(sessions.login(code, "local"), null);
  assert.ok(sessions.login(code, "other"));
});

test("HTTP host, origin, auth, csrf, logout and static boundaries", async t => {
  // 不使用数据库：这些拒绝必须发生在任何业务 Store 方法之前。
  const store = new Proxy({}, { get: () => () => { throw new Error("UNEXPECTED_STORE_ACCESS"); } }) as ReviewStore;
  const app = createReviewServer(store, { origin, identities: [identity] });
  t.after(() => app.close());
  assert.equal((await app.inject({ url: "/api/v1/me", headers })).statusCode, 401);
  assert.equal((await app.inject({ url: "/", headers: { host: "attacker.example" } })).statusCode, 403);
  assert.equal((await app.inject({ method: "POST", url: "/api/v1/session", headers: { ...headers, origin: "https://attacker.example" }, payload: { code } })).statusCode, 403);
  assert.equal((await app.inject({ method: "POST", url: "/api/v1/session", headers, payload: { code, role: "BUSINESS" } })).statusCode, 400);
  const login = await app.inject({ method: "POST", url: "/api/v1/session", headers, payload: { code } });
  assert.equal(login.statusCode, 200);
  assert.match(String(login.headers["set-cookie"]), /HttpOnly; SameSite=Strict/);
  const cookie = String(login.headers["set-cookie"]).split(";")[0]!;
  const authenticated = { ...headers, cookie };
  const me = await app.inject({ url: "/api/v1/me", headers: authenticated });
  assert.equal(me.json().actor.role, "QA");
  const denied = await app.inject({ method: "POST", url: "/api/v1/review-cases/example/decisions", headers: authenticated, payload: {} });
  assert.equal(denied.statusCode, 403); assert.equal(denied.json().error_code, "CSRF_REJECTED");
  const csrf = login.json().csrf_token as string;
  assert.equal((await app.inject({ method: "POST", url: "/api/v1/logout", headers: { ...authenticated, "x-csrf-token": csrf }, payload: {} })).statusCode, 200);
  assert.equal((await app.inject({ url: "/api/v1/session", headers: authenticated })).statusCode, 401);
  const index = await app.inject({ url: "/", headers }); assert.equal(index.statusCode, 200);
  assert.match(index.headers["content-security-policy"] as string, /frame-ancestors 'none'/);
  assert.equal(index.headers["cache-control"], "no-store");
  assert.equal((await app.inject({ url: "/artifacts/review-workbench/config.private.json", headers })).statusCode, 401);
});
