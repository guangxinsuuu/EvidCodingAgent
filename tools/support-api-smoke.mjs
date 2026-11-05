/** 已登记SMOKE动作 → 既有受控GenaiSupportApi → 真实API；不把接口PASS当业务Oracle。 */
import { readFileSync, writeFileSync, appendFileSync } from "node:fs";
import { resolve } from "node:path";
import { runSupportSmoke } from "../packages/genai-support-adapter/dist/smoke.js";
import { LabLedger } from "./environment-host/dist/lab-ledger.js";
const [environmentId, evidenceDirectory] = process.argv.slice(2);
if (!/^environment_[a-f0-9-]{36}$/.test(environmentId ?? "")) throw new Error("INVALID_ENVIRONMENT");
const root = resolve(import.meta.dirname, "..");
const directory = resolve(root, "artifacts/lab/genai-support-runtime", environmentId);
const environment = JSON.parse(readFileSync(resolve(directory, "manifest.json"), "utf8"));
const runtime = JSON.parse(readFileSync(resolve(directory, "runtime.json"), "utf8"));
const secret = JSON.parse(readFileSync(resolve(directory, "secrets.json"), "utf8"));
const ledger = LabLedger.open(resolve(root, "artifacts/lab/genai-support-runtime"));
try {
  const result = await runSupportSmoke({ environment, expectedEnvironmentId: environmentId,
    expectedLeaseEpoch: environment.lease_epoch, session: { expectedTenantId: "tenant-a", bearerToken: secret.api_token_a },
    executionMode: runtime.execution_mode === "LIVE_MODEL" ? "live" : "scripted-fake", jobTimeoutMs: 180_000,
    guard: async () => {
      const current = ledger.snapshot().environments[environmentId];
      if (!current || current.status !== "REGISTERED" || current.lease_epoch !== environment.lease_epoch || Date.parse(current.expires_at) <= Date.now()) throw new Error("ENVIRONMENT_UNAVAILABLE");
    }, evidenceSink: async observation => {
      appendFileSync(resolve(evidenceDirectory, "api-observations.jsonl"), JSON.stringify(observation) + "\n", { mode: 0o600 });
    } });
  writeFileSync(resolve(evidenceDirectory, "api-smoke.json"), JSON.stringify(result, null, 2) + "\n", { mode: 0o600 });
  process.stdout.write(JSON.stringify({ kind: result.kind, verdict: result.verdict, cases: result.cases.length }) + "\n");
  if (result.verdict !== "PASS") process.exitCode = 1;
} finally { ledger.close(); }
