import { createHash, randomBytes } from "node:crypto";
import { mkdir, writeFile, lstat, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { DEFAULT_CONFIG, privateText, assertLocalDatabase, type ReviewConfig } from "./config.js";

/**
 * 操作者一次性配置 → 本地身份的随机码/哈希 → 私密配置。
 * 不覆盖已有身份或历史决定；数据库必须已由操作者提供，代码不隐式复用 SUT 凭据。
 */
export async function setup(args: string[]): Promise<void> {
  const [flag, input, portRaw = "4174"] = args;
  if (flag !== "--database-url-file" || !input || args.length > 3) throw new Error("USAGE: review:setup --database-url-file PRIVATE_FILE [PORT]");
  const database_url = (await privateText(input)).trim();
  assertLocalDatabase(database_url);
  const port = Number(portRaw);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("VALID_PORT_REQUIRED");
  const folder = dirname(resolve(DEFAULT_CONFIG));
  await mkdir(folder, { recursive: true, mode: 0o700 });
  const stat = await lstat(folder);
  if (stat.isSymbolicLink() || (stat.mode & 0o077) !== 0) throw new Error("PRIVATE_DIRECTORY_REQUIRED");
  const roles = [ ["BUSINESS", "业务审查"], ["SOURCE", "数据来源审查"], ["QA", "QA 审查"] ] as const;
  // 登录码先落地，配置最后作为完成标记；中断后沿用已有随机码，绝不轮换身份。
  const codesPath = resolve(folder, "login-codes.private.json");
  let logins: { role: typeof roles[number][0]; name: string; code: string }[];
  try {
    const saved = JSON.parse(await privateText(codesPath)) as { logins: typeof logins };
    logins = saved.logins;
    if (!Array.isArray(logins) || logins.length !== 3 || roles.some(([role]) => logins.filter(i => i.role === role && /^[a-f0-9]{48}$/.test(i.code)).length !== 1)) throw new Error("INVALID_EXISTING_CODES");
  } catch (error) {
    if (!(error instanceof Error && "code" in error && error.code === "ENOENT")) throw error;
    logins = roles.map(([role, name]) => ({ role, name, code: randomBytes(24).toString("hex") }));
    await writeFile(codesPath, JSON.stringify({ mode: "LOCAL_EXPERIMENTAL_IDENTITIES", logins }, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  }
  const config: ReviewConfig = { database_url, port, identities: logins.map(i => ({
    actor: { id: `local-${i.role.toLowerCase()}`, display_name: `${i.name}（实验身份）`, role: i.role, tenant_id: "tenant-b" },
    code_hash: createHash("sha256").update(i.code).digest("hex"),
  })) };
  for (const login of logins) {
    const path = resolve(folder, `${login.role.toLowerCase()}.code`);
    try { await writeFile(path, login.code + "\n", { mode: 0o600, flag: "wx" }); }
    catch (error) {
      if (!(error instanceof Error && "code" in error && error.code === "EEXIST") || (await privateText(path)).trim() !== login.code) throw error;
    }
  }
  // wx 保证已有配置不被覆盖；重复的完整 setup 仅在字节一致时成功。
  const serialized = JSON.stringify(config, null, 2) + "\n";
  try { await writeFile(DEFAULT_CONFIG, serialized, { mode: 0o600, flag: "wx" }); }
  catch (error) {
    if (!(error instanceof Error && "code" in error && error.code === "EEXIST") || await readFile(DEFAULT_CONFIG, "utf8") !== serialized) throw error;
  }
  process.stdout.write(JSON.stringify({ status: "CONFIGURED", config: DEFAULT_CONFIG, login_codes: "artifacts/review-workbench/login-codes.private.json" }) + "\n");
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void setup(process.argv.slice(2)).catch(() => { process.stderr.write("Review setup failed: private input, port, or existing configuration requires attention.\n"); process.exitCode = 1; });
}
