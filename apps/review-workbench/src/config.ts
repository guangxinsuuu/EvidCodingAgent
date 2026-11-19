import { lstat, readFile } from "node:fs/promises";
import { REVIEW_ROLES } from "./types.js";
import type { LocalIdentity } from "./auth.js";

export const DEFAULT_CONFIG = "artifacts/review-workbench/config.private.json";
export interface ReviewConfig { database_url: string; port: number; identities: LocalIdentity[] }

/** pg 的 query.host 会覆盖 URL hostname；本地版禁用参数/fragment，校验实际连接边界。 */
export function assertLocalDatabase(value: string): void {
  const url = new URL(value);
  if (!["postgres:", "postgresql:"].includes(url.protocol) || url.hostname !== "127.0.0.1" || url.search || url.hash) throw new Error("LOCAL_DATABASE_REQUIRED");
}

/** 启动/CLI 共用的私密文件校验。拒绝 symlink 和组/其他用户可读文件，不输出原文或连接串。 */
export async function privateText(path: string): Promise<string> {
  const stat = await lstat(path);
  if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0 || stat.size > 65_536) throw new Error("PRIVATE_FILE_REQUIRED");
  return readFile(path, "utf8");
}

export async function loadConfig(path = DEFAULT_CONFIG): Promise<ReviewConfig> {
  const data = JSON.parse(await privateText(path)) as ReviewConfig;
  assertLocalDatabase(data.database_url);
  if (!Number.isInteger(data.port) || data.port < 1024 || data.port > 65535) throw new Error("INVALID_PORT");
  if (!Array.isArray(data.identities) || data.identities.length !== 3) throw new Error("THREE_IDENTITIES_REQUIRED");
  for (const role of REVIEW_ROLES) {
    const ids = data.identities.filter(i => i.actor?.role === role);
    if (ids.length !== 1 || !ids[0]!.actor.id || !ids[0]!.actor.display_name || !ids[0]!.actor.tenant_id || !/^[a-f0-9]{64}$/.test(ids[0]!.code_hash)) throw new Error("INVALID_IDENTITY");
  }
  if (new Set(data.identities.map(i => i.actor.id)).size !== 3 || new Set(data.identities.map(i => i.code_hash)).size !== 3) throw new Error("DUPLICATE_IDENTITY");
  return data;
}
