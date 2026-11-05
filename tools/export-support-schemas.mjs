import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { supportRequestSchema, supportSubmissionSchema, supportJobSchema } from "../packages/contracts/dist/support.js";

/**
 * pnpm build → 此入口 → Python 包内的协议快照。
 * TypeScript exports 是唯一 Schema 源；Python 只消费该产物，不手抄第二套约束。
 * --check 仅校验、不写文件，供测试发现已修改 TS 却忘记更新快照的漂移。
 */
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const target = resolve(root, "services/genai-support-sut/src/genai_support/schemas/support-v1.json");
const expected = `${JSON.stringify({
  schema_version: 1,
  source_ref: "packages/contracts/src/support.ts",
  schemas: { request: supportRequestSchema, submission: supportSubmissionSchema, job: supportJobSchema },
}, null, 2)}\n`;
const args = process.argv.slice(2);
if (args.length > 1 || (args.length === 1 && args[0] !== "--check")) {
  throw new Error("Usage: node tools/export-support-schemas.mjs [--check]");
}
if (args[0] === "--check") {
  let actual;
  try { actual = readFileSync(target, "utf8"); } catch { throw new Error("SUPPORT_SCHEMA_SNAPSHOT_MISSING"); }
  if (actual !== expected) throw new Error("SUPPORT_SCHEMA_SNAPSHOT_DRIFT");
  process.stdout.write("Support protocol snapshot matches TypeScript schemas.\n");
} else {
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, expected, "utf8");
  process.stdout.write("Exported support protocol snapshot.\n");
}
