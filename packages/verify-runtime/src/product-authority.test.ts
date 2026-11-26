import assert from "node:assert/strict";
import { test } from "node:test";
import { sha, validateBundle, type ReleaseBundle } from "./types.js";

function fixture(): ReleaseBundle {
  const repository = "https://github.com/guangxinsuuu/EvidSupportSUT.git";
  const baseline = "a".repeat(40), candidate = "b".repeat(40);
  const prefix = "services/genai-support-sut/", physical = "src/example.py";
  return { schema_version: 2, task_mode: "RELEASE_VERIFICATION", scope: "LOCAL_SYNTHETIC_PLANNING",
    input_provenance: "REGISTERED_EXTERNAL_PRODUCT", goal: "审查独立产品变化", baseline_sha: baseline, candidate_sha: candidate,
    catalog_commit: "c".repeat(40), catalog_basis_commit: "d".repeat(40), requirement_refs: [],
    changes: [{ path: prefix + physical }], materials: [{ material_ref: "candidate:example", kind: "CANDIDATE_SOURCE",
      title: "候选源码", git_commit: candidate, path: prefix + physical, source_path: physical,
      repository_id: "evid-support", content: "value=2\n", sha256: sha("value=2\n") }],
    catalog: { status: "DRAFT", dispatch_authorized: false }, gaps: [], current_candidate_coverage: [], evaluation_status: "NOT_RUN",
    review_request: { schema_version: 1, request_id: "review:external", revision: 1, requester_ref: "local:requester", owner_ref: "local:qa",
      goal: "审查独立产品变化", non_goals: [], baseline: { commit: baseline, reason: "已固定比较提交", source_ref: repository },
      candidate: { commit: candidate, source_ref: repository }, requirements: [], known_gaps: [], predecessor_run_id: null },
    product_binding: { schema_version: 1, product_id: "evid-support", repository_url: repository, logical_prefix: prefix,
      baseline: { commit: baseline, tree: "e".repeat(40) }, candidate: { commit: candidate, tree: "f".repeat(40) },
      egap_commit: "1".repeat(40), authority: "OPERATOR_REGISTERED_LOCAL_CHECKOUT", remote_attestation: false } };
}

test("独立产品包保留两套版本身份和物理来源，不增加执行资格", () => {
  const value = validateBundle(fixture());
  assert.notEqual(value.product_binding?.egap_commit, value.candidate_sha);
  assert.equal(value.materials[0]?.source_path, "src/example.py");
  assert.deepEqual(value.current_candidate_coverage, []);
});
test("产品来源、提交和路径不能混用，旧包不能夹带新身份", () => {
  const edits: [string, (b: ReleaseBundle) => void][] = [
    ["PRODUCT_BINDING_REQUIRED", b => { delete b.product_binding; }],
    ["PRODUCT_REGISTRATION_MISMATCH", b => { b.product_binding!.repository_url = "https://example.invalid/fake"; }],
    ["PRODUCT_REVISION_MISMATCH", b => { b.product_binding!.candidate.commit = "9".repeat(40); }],
    ["PRODUCT_MATERIAL_PATH_MISMATCH", b => { b.materials[0]!.source_path = "../private"; }],
    ["PRODUCT_MATERIAL_REVISION_MISMATCH", b => { b.materials[0]!.git_commit = b.baseline_sha; }],
    ["REVIEW_PRODUCT_SOURCE_MISMATCH", b => { b.review_request = { ...b.review_request!, candidate: { commit: b.candidate_sha, source_ref: "other:repo" } }; }],
    ["UNEXPECTED_PRODUCT_BINDING", b => { b.input_provenance = "CALIBRATION_SYNTHETIC"; }],
  ];
  for (const [code, edit] of edits) { const b = fixture(); edit(b); assert.throws(() => validateBundle(b), new RegExp(code)); }
});
