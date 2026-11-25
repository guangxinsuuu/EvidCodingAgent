import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parseReviewDecisionProposal } from "./review-decision.js";
import { DecisionProposalError, parseDecisionProposal } from "./verify.js";

interface Sample { name: string; valid: boolean; value: unknown }
const corpus = JSON.parse(readFileSync(new URL("../../../protocols/verify-agent/v3/conformance.json", import.meta.url), "utf8")) as { cases: Sample[] };
for (const sample of corpus.cases) test(`ReviewDecision v3 跨语言协议 ${sample.name}`, () => {
  if (sample.valid) assert.deepEqual(parseReviewDecisionProposal(sample.value), sample.value);
  else assert.throws(() => parseReviewDecisionProposal(sample.value), DecisionProposalError);
});

test("部分草案可以进入确定性缺项反馈，但协议不替模型填入预期结果", () => {
  const parsed = parseReviewDecisionProposal(corpus.cases.find(item => item.name === "valid_partial_draft")!.value);
  assert.equal(parsed.action_kind, "PLAN_DRAFT");
  if (parsed.action_kind !== "PLAN_DRAFT") return;
  assert.deepEqual(Object.keys(parsed.payload.draft), ["objective"]);
});
test("决策框架和实验输入深层冻结，不共享调用方可变对象", () => {
  const input = structuredClone(corpus.cases.find(item => item.name === "valid_complete_draft")!.value) as Record<string, unknown>;
  const parsed = parseReviewDecisionProposal(input);
  const frame = input.decision_frame as { selected_question_id: string };
  frame.selected_question_id = "q:changed";
  assert.equal(parsed.decision_frame.selected_question_id, "q:cutoff");
  assert.equal(Object.isFrozen(parsed.decision_frame.unresolved_questions[0]!.evidence_refs), true);
  if (parsed.action_kind !== "PLAN_DRAFT") throw new Error("fixture invalid");
  assert.equal(Object.isFrozen(parsed.payload.draft.inputs![0]), true);
});
test("新决策不能被旧 v1 解析器误收；旧输入也不能缺框架进入 v3", () => {
  assert.throws(() => parseDecisionProposal(corpus.cases[0]!.value), DecisionProposalError);
  const old = JSON.parse(readFileSync(new URL("../../../protocols/verify-agent/v1/conformance.json", import.meta.url), "utf8")) as { cases: Sample[] };
  assert.throws(() => parseReviewDecisionProposal(old.cases[0]!.value), DecisionProposalError);
});
test("拒绝反馈不回显私有草案内容", () => {
  const base = structuredClone(corpus.cases[0]!.value) as Record<string, unknown>;
  assert.throws(() => parseReviewDecisionProposal({ ...base, private_marker: "private_marker" }), (error: unknown) => {
    assert.ok(error instanceof DecisionProposalError);
    assert.equal(JSON.stringify(error).includes("private_marker"), false);
    return true;
  });
});
