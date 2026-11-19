import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import type { Actor } from "./types.js";

export interface LocalIdentity { actor: Actor; code_hash: string }
interface Session { actor: Actor; csrf_token: string; expires_at: number }
const digest = (value: string): Buffer => createHash("sha256").update(value).digest();

/**
 * 本地三身份实验登录，不伪装为企业 SSO。server 的认证钩子只接受服务端签发的
 * 随机会话；actor/role/tenant 永不来自请求 body。重启使会话失效，数据库决定不丢失。
 * 高熵登录码只保存 SHA256；浏览器只拿 HttpOnly cookie，不接触数据库或 Run token。
 */
export class LocalSessions {
  private readonly sessions = new Map<string, Session>();
  private readonly attempts = new Map<string, { count: number; reset: number }>();
  constructor(private readonly identities: readonly LocalIdentity[], private readonly now = Date.now) {}

  login(code: string, address: string): { token: string; session: Session } | null {
    const now = this.now();
    // 回环监听仍限速；每个地址每分钟 10 次，清理过期项避免进程长期增长。
    for (const [key, value] of this.attempts) if (value.reset <= now) this.attempts.delete(key);
    const attempt = this.attempts.get(address) ?? { count: 0, reset: now + 60_000 };
    attempt.count++; this.attempts.set(address, attempt);
    if (attempt.count > 10 || code.length > 128) return null;
    const incoming = digest(code);
    const identity = this.identities.find(value => timingSafeEqual(incoming, Buffer.from(value.code_hash, "hex")));
    if (!identity) return null;
    for (const [key, value] of this.sessions) if (value.expires_at <= now) this.sessions.delete(key);
    if (this.sessions.size >= 100) return null;
    const token = randomBytes(32).toString("hex");
    const session = { actor: structuredClone(identity.actor), csrf_token: randomBytes(32).toString("hex"), expires_at: now + 8 * 60 * 60_000 };
    this.sessions.set(digest(token).toString("hex"), session);
    return { token, session };
  }

  get(token: string | undefined): Session | null {
    if (!token || !/^[a-f0-9]{64}$/.test(token)) return null;
    const key = digest(token).toString("hex");
    const session = this.sessions.get(key);
    if (!session) return null;
    if (session.expires_at <= this.now()) { this.sessions.delete(key); return null; }
    return session;
  }
  logout(token: string | undefined): void { if (token) this.sessions.delete(digest(token).toString("hex")); }
  csrfMatches(session: Session, value: unknown): boolean {
    return typeof value === "string" && timingSafeEqual(digest(value), digest(session.csrf_token));
  }
}

/** 只解析本应用 cookie，重复同名 cookie 拒绝，避免路径混淆。 */
export function sessionCookie(raw: string | undefined): string | undefined {
  const matches = (raw ?? "").split(";").map(s => s.trim()).filter(s => s.startsWith("egap_review_session="));
  return matches.length === 1 ? matches[0]!.slice("egap_review_session=".length) : undefined;
}
