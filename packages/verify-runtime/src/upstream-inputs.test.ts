import assert from "node:assert/strict";
import { test } from "node:test";
import { upstreamContext, upstreamRefs, validateUpstreamInputs } from "./upstream-inputs.js";
import { sha, type Json, type ReleaseBundle, type RunState } from "./types.js";

/** 仅验证交接关联，测试对象不代表真实事故或企业批准。 */
function fixture(): ReleaseBundle {
  const candidate = { repository_url: "https://github.com/guangxinsuuu/EvidSupportSUT.git", commit: "a".repeat(40) };
  const common = { schema_version: 1, investigation_id: "INV-LAB-100", revision: 1, source_platform_id: "simulated-project1",
    authority_scope: "SYNTHETIC_LOCAL_CALIBRATION", candidate, repair_authorized: false, release_authorized: false, current_candidate_coverage: [] };
  const values: Record<string, Json> = {
    [upstreamRefs.evidence]: { ...common, kind: "PROJECT1_EVIDENCE_MANIFEST", limitations: ["reference only"], records: [{
      id: "e:1", kind: "ARCHIVED_OBSERVATION_POINTER", sha256: "b".repeat(64), source_repository: "https://github.com/guangxinsuuu/EvidCodingAgent.git",
      source_commit: "c".repeat(40), index_path: "reports/example/index.json", observed_at: "2026-11-26T08:00:00Z",
      deployment_id: "sut-lab", image_id: "sha256:" + "d".repeat(64), original_access: "REFERENCE_ONLY_NOT_INGESTED", summary: "an incomplete observation" }] },
    [upstreamRefs.diagnosis]: { ...common, kind: "PROJECT1_DIAGNOSIS", environment: { kind: "LOCAL_SYNTHETIC", deployment_id: "sut-lab",
      time_window: { start: "2026-11-26T08:00:00Z", end: "2026-11-26T08:01:00Z" } }, evidence_manifest_ref: upstreamRefs.evidence,
      classification: "UNDETERMINED", hypotheses: [{ id: "h:1", statement: "might be incomplete", status: "UNCONFIRMED", evidence_refs: ["e:1"] }],
      unresolved_gaps: ["original missing"], business_verdict: "NOT_EVALUATED" },
    [upstreamRefs.handoff]: { ...common, kind: "PROJECT1_ENGINEERING_HANDOFF", diagnosis_ref: upstreamRefs.diagnosis,
      evidence_manifest_ref: upstreamRefs.evidence, evidence_refs: ["e:1"], suggested_paths: ["src/genai_support/worker.py"],
      validation_questions: [{ requirement_ref: "R5", question: "does failure stay unavailable?" }], requested_next_steps: ["INVESTIGATE_EVIDENCE"],
      openspec_status: "HANDOFF_INPUT_NOT_GENERATED_OPENSPEC_CHANGE", unresolved_gaps: ["not authorized"] },
  };
  // 这个最小对象只进入本模块的内容验证；完整任务 Schema 由实际装配/Runtime 集成验收另测。
  return { candidate_sha: candidate.commit, product_binding: { repository_url: candidate.repository_url }, requirement_refs: ["R5"],
    external_sources: Object.keys(values).map(ref => ({ ref })), materials: Object.entries(values).map(([ref, v]) => {
      const content = JSON.stringify(v); return { material_ref: ref, external_source_ref: ref, kind: "UPSTREAM_DIAGNOSIS_INPUT",
        content, sha256: sha(content), source_byte_start: 0, source_byte_end: Buffer.byteLength(content) };
    }) } as unknown as ReleaseBundle;
}
function edit(bundle: ReleaseBundle, ref: string, mutate: (value: Json) => void): void {
  const m = bundle.materials.find(m => m.material_ref === ref)!;
  const value = JSON.parse(m.content) as Json; mutate(value); m.content = JSON.stringify(value);
  m.source_byte_end = Buffer.byteLength(m.content); m.sha256 = sha(m.content);
}
test("Project 1：关联材料可读，指针与修复建议不继承覆盖或权限", () => {
  const b = fixture(); validateUpstreamInputs(b);
  const view = upstreamContext(b, { read_receipts: [] } as unknown as RunState);
  assert.deepEqual(view.inherited_candidate_coverage, []); assert.equal(view.dispatch_qualified, false);
  assert.equal((view.inputs as Json[]).length, 3);
  assert.ok((view.inputs as Json[]).every(row => row.read_receipt_present === false));
});
test("Project 1：准确候选、原件访问等级、引用、需求和写权限分别检查", () => {
  const mutations: [string, string, (v: Json) => void][] = [
    ["PROJECT1_CANDIDATE_MISMATCH", upstreamRefs.diagnosis, v => { (v.candidate as Json).commit = "e".repeat(40); }],
    ["PROJECT1_AUTHORITY_UPGRADE_FORBIDDEN", upstreamRefs.handoff, v => { v.repair_authorized = true; }],
    ["PROJECT1_AUTHORITY_UPGRADE_FORBIDDEN", upstreamRefs.diagnosis, v => { v.current_candidate_coverage = ["PASS"]; }],
    ["PROJECT1_EVIDENCE_INVALID", upstreamRefs.evidence, v => { (v.records as Json[])[0]!.original_access = "VERIFIED_ORIGINAL"; }],
    ["PROJECT1_HYPOTHESIS_INVALID", upstreamRefs.diagnosis, v => { (v.hypotheses as Json[])[0]!.evidence_refs = ["unknown"]; }],
    ["PROJECT1_HANDOFF_INVALID", upstreamRefs.handoff, v => { v.suggested_paths = ["../../rules.md"]; }],
    ["PROJECT1_REQUIREMENT_NOT_IN_TASK", upstreamRefs.handoff, v => { (v.validation_questions as Json[])[0]!.requirement_ref = "NEW_UNAPPROVED"; }],
    ["UPSTREAM_FIELDS_INVALID", upstreamRefs.handoff, v => { v.shell_command = "run everything"; }],
  ];
  for (const [code, ref, mutate] of mutations) {
    const b = fixture(); edit(b, ref, mutate); assert.throws(() => validateUpstreamInputs(b), new RegExp(code));
  }
});
test("Project 1：缺任一关联原件或只提供摘要不形成完整交接", () => {
  const b = fixture(); b.materials = b.materials.filter(m => m.material_ref !== upstreamRefs.evidence);
  assert.throws(() => validateUpstreamInputs(b), /UPSTREAM_FULL_ORIGINAL_REQUIRED/);
  const partial = fixture(); partial.materials[0]!.source_byte_start = 10;
  assert.throws(() => validateUpstreamInputs(partial), /UPSTREAM_FULL_ORIGINAL_REQUIRED/);
});
