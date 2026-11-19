import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { HISTORICAL_SOURCE, HistoricalImportError, loadHistoricalCase, parseHistoricalCase } from "./importer.js";

const repository = fileURLToPath(new URL("../../../", import.meta.url));
const source = join(repository, HISTORICAL_SOURCE.directory);
async function inputs(): Promise<[Buffer, Buffer]> { return Promise.all([readFile(join(source, "summary.json")), readFile(join(source, "checks.json"))]); }
function failure(code: string): (error: unknown) => boolean {
  return (error: unknown): boolean => error instanceof HistoricalImportError && error.code === code;
}

test("historical importer freezes real candidate, two scenarios and three unapproved review obligations", async () => {
  const imported = await loadHistoricalCase(repository);
  assert.equal(imported.candidate_sha, HISTORICAL_SOURCE.candidate_sha);
  assert.equal(imported.baseline_sha, null);
  assert.equal(imported.execution_mode, "MANUAL_FIXED_PLAN_INTEGRATION_ONLY");
  assert.equal(imported.machine_result.business_assertions_passed, 183);
  assert.equal(imported.machine_result.verifier_autonomous_execution, false);
  assert.deepEqual(imported.obligations.map(row => row.role), ["BUSINESS", "SOURCE", "QA"]);
  assert.equal(imported.evidence.length, 6);
  assert.ok(imported.limitations.includes("NO_BASELINE_DIFF_OR_PR_AVAILABLE"));
  assert.ok(imported.evidence.some(row => row.kind === "RAW_DRAFT_REVIEW_EXCERPT" && row.limitations.includes("RAW_MODEL_OUTPUT_NOT_CAPTURED")));
  assert.ok(imported.evidence.some(row => row.kind === "DELIVERY_CHECK_REPORT_EXCERPT" && row.limitations.includes("FINAL_PRIVATE_NOTE_TEXT_NOT_IMPORTED")));
  for (const evidence of imported.evidence) {
    assert.equal(evidence.sha256, createHash("sha256").update(evidence.content).digest("hex"));
    assert.ok([HISTORICAL_SOURCE.summary_sha256, HISTORICAL_SOURCE.checks_sha256].includes(evidence.source_sha256 as typeof HISTORICAL_SOURCE.summary_sha256));
  }
  const serialized = JSON.stringify(imported);
  for (const forbidden of ["private_evidence_root", "private_evidence_sha256", "artifacts/business-source", "invoice_id", "customer_id", "job_id", "private_note_id", "pid"]) {
    assert.equal(serialized.includes(`\"${forbidden}\"`), false, `${forbidden} leaked`);
  }
});

test("historical replay is deterministic and source-byte tampering fails closed", async () => {
  const [summary, checks] = await inputs();
  assert.deepEqual(parseHistoricalCase(summary, checks), parseHistoricalCase(summary, checks));
  for (const patch of [{ candidate_sha: undefined }, { scope: "AUTONOMOUS_AGENT" }, { password: "must-not-import" }]) {
    const changed = Buffer.from(JSON.stringify({ ...JSON.parse(summary.toString("utf8")), ...patch }));
    assert.throws(() => parseHistoricalCase(changed, checks), failure("HISTORICAL_SOURCE_DIGEST_MISMATCH"));
  }
  assert.throws(() => parseHistoricalCase(summary, Buffer.from("[]")), failure("HISTORICAL_SOURCE_DIGEST_MISMATCH"));
  assert.throws(() => parseHistoricalCase(Buffer.alloc(1_048_577), checks), failure("HISTORICAL_SOURCE_TOO_LARGE"));
});

test("fixed-path loading rejects symlinked files and intermediate directories without exposing paths", async t => {
  const root = await mkdtemp(join(tmpdir(), "egap-review-import-"));
  t.after(async () => { await rm(root, { recursive: true, force: true }); });
  const destination = join(root, HISTORICAL_SOURCE.directory);
  await mkdir(destination, { recursive: true });
  await symlink(join(source, "summary.json"), join(destination, "summary.json"));
  await writeFile(join(destination, "checks.json"), (await inputs())[1]);
  await assert.rejects(loadHistoricalCase(root), failure("HISTORICAL_SOURCE_LINK_REJECTED"));
  await rm(join(root, "reports"), { recursive: true });
  await symlink(resolve(repository, "reports"), join(root, "reports"));
  await assert.rejects(loadHistoricalCase(root), failure("HISTORICAL_SOURCE_LINK_REJECTED"));
  await assert.rejects(loadHistoricalCase(join(root, "missing")), error => {
    assert.ok(error instanceof HistoricalImportError);
    assert.equal(error.message, "HISTORICAL_SOURCE_UNAVAILABLE");
    assert.equal(error.message.includes(root), false);
    return true;
  });
});
