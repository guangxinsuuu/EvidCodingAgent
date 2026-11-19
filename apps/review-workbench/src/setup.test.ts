import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile, rm, chmod } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { setup } from "./setup.js";
import { loadConfig, privateText } from "./config.js";

test("interrupted setup resumes original codes; complete replay cannot rotate identity", async () => {
  const folder = await mkdtemp(join(tmpdir(), "egap-review-setup-"));
  const cwd = process.cwd();
  try {
    process.chdir(folder);
    await mkdir("artifacts/review-workbench", { recursive: true, mode: 0o700 });
    const logins = (["BUSINESS", "SOURCE", "QA"] as const).map((role, i) => ({ role, name: role, code: String(i + 1).repeat(48) }));
    await writeFile("artifacts/review-workbench/login-codes.private.json", JSON.stringify({ logins }), { mode: 0o600 });
    await writeFile("database.private", "postgres://127.0.0.1/review_test", { mode: 0o600 });
    await setup(["--database-url-file", "database.private"]);
    const config = await loadConfig();
    for (const login of logins) {
      assert.equal(config.identities.find(i => i.actor.role === login.role)?.code_hash, createHash("sha256").update(login.code).digest("hex"));
      assert.equal((await privateText(`artifacts/review-workbench/${login.role.toLowerCase()}.code`)).trim(), login.code);
    }
    const before = await readFile("artifacts/review-workbench/config.private.json", "utf8");
    await setup(["--database-url-file", "database.private"]);
    assert.equal(await readFile("artifacts/review-workbench/config.private.json", "utf8"), before);
    await assert.rejects(setup(["--database-url-file", "database.private", "4175"]));
    await chmod("database.private", 0o644);
    await assert.rejects(privateText("database.private"));
  } finally { process.chdir(cwd); await rm(folder, { recursive: true, force: true }); }
});
