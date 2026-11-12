import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { DecisionProposalError, parseDecisionProposal } from "./verify.js";

interface ConformanceCase { name: string; valid: boolean; value: unknown }
const corpus = JSON.parse(readFileSync(new URL(
  "../../../protocols/verify-agent/v1/conformance.json", import.meta.url,
), "utf8")) as { cases: ConformanceCase[] };

// 同一静态 corpus 由 TS 和 Python 消费；不是测试中重建一份不同的协议规则。
for (const sample of corpus.cases) {
  test(`Verify 跨语言协议 ${sample.name}`, () => {
    if (sample.valid) assert.deepEqual(parseDecisionProposal(sample.value), sample.value);
    else assert.throws(() => parseDecisionProposal(sample.value), DecisionProposalError);
  });
}

test("Verify 不接受 JS 非有限数字；JSON 序列化之前也不会变成 null 后被接受", () => {
  const base = corpus.cases[0]!.value as Record<string, unknown>;
  for (const value of [NaN, Infinity, -Infinity]) {
    assert.throws(() => parseDecisionProposal({ ...base, based_on_decision_revision: value }),
      DecisionProposalError);
  }
});

test("Verify 输入和深层 payload 隔离，消费方不能修改已验材料引用", () => {
  const input = structuredClone(corpus.cases[0]!.value) as Record<string, unknown>;
  const parsed = parseDecisionProposal(input);
  assert.equal(parsed.action_kind, "READ");
  (input["payload"] as Record<string, unknown>)["material_ref"] = "changed:after-parse";
  assert.equal(parsed.payload !== input["payload"], true);
  assert.equal(Object.isFrozen(parsed.payload), true);
  assert.equal(Object.isFrozen(parsed.hypothesis_updates[0]!), true);
  assert.equal(Object.isFrozen(parsed.hypothesis_updates[0]!.evidence_refs), true);
  if (parsed.action_kind === "READ") assert.notEqual(parsed.payload.material_ref, "changed:after-parse");
});

test("Verify 拒绝错误不反射请求里的身份或秘密字段", () => {
  assert.throws(() => parseDecisionProposal({ secret: "must-never-leak" }), (error: unknown) => {
    assert.ok(error instanceof DecisionProposalError);
    assert.equal(error.error_code, "INVALID_DECISION_PROPOSAL");
    assert.equal(error.message.includes("must-never-leak"), false);
    return true;
  });
});

test("Verify 校验反馈只含keyword/path，不含Ajv params、原始输入或额外字段名", () => {
  const base = structuredClone(corpus.cases[0]!.value) as Record<string, unknown>;
  const sentinel = "private-sensitive-marker";
  const input = { ...base, requirement_refs: [sentinel, sentinel], [sentinel]: { credential: sentinel } };
  assert.throws(() => parseDecisionProposal(input), (error: unknown) => {
    assert.ok(error instanceof DecisionProposalError);
    assert.ok(error.issues.some((issue) => issue.keyword === "uniqueItems" && issue.path === "/requirement_refs"));
    assert.ok(error.issues.length > 0 && error.issues.length <= 8);
    for (const issue of error.issues) assert.deepEqual(Object.keys(issue).sort(), ["keyword", "path"]);
    assert.equal(JSON.stringify(error.issues).includes(sentinel), false);
    assert.equal(JSON.stringify(error.issues).includes("credential"), false);
    assert.equal(error.message.includes(sentinel), false);
    return true;
  });
});
