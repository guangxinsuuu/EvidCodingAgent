import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parseReviewRequest, ReviewRequestError, type ReviewRequest } from "./review-request.js";

interface Sample { name: string; valid: boolean; value: unknown }
const corpus = JSON.parse(readFileSync(new URL("../../../protocols/review-request/v1/conformance.json", import.meta.url), "utf8")) as { cases: Sample[] };
for (const sample of corpus.cases) test(`ReviewRequest 共享协议 ${sample.name}`, () => {
  if (sample.valid) assert.deepEqual(parseReviewRequest(sample.value), sample.value);
  else assert.throws(() => parseReviewRequest(sample.value), ReviewRequestError);
});

test("请求解析不补造要求、公司批准或执行权限", () => {
  const sample = corpus.cases.find(item => item.name === "valid_missing_requirements")!;
  const parsed = parseReviewRequest(sample.value);
  assert.deepEqual(parsed.requirements, []);
  assert.equal("dispatch_authorized" in parsed, false);
  assert.equal(Object.isFrozen(parsed.known_gaps[0]!.requirement_refs), true);
});
test("相同要求身份和缺项身份不能在一次请求中重定义", () => {
  const request = corpus.cases[0]!.value as ReviewRequest;
  assert.throws(() => parseReviewRequest({ ...request, requirements: [...request.requirements, ...request.requirements] }), ReviewRequestError);
  const gap = { id: "gap:rule", requirement_refs: ["R4"], description: "pending" };
  assert.throws(() => parseReviewRequest({ ...request, known_gaps: [gap, gap] }), ReviewRequestError);
  assert.throws(() => parseReviewRequest({ ...request, known_gaps: [{ ...gap, requirement_refs: ["R-unknown"] }] }), ReviewRequestError);
});
test("请求返回深层不可变副本；后续更改不能污染冻结审查依据", () => {
  const request = structuredClone(corpus.cases[0]!.value) as { baseline: { commit: string }; requirements: { statement: string }[] };
  const parsed = parseReviewRequest(request);
  request.baseline.commit = "c".repeat(40);
  request.requirements[0]!.statement = "改写原要求";
  assert.equal(parsed.baseline.commit, "a".repeat(40));
  assert.equal(parsed.requirements[0]!.statement, "保留原申请截止点语义");
  assert.equal(Object.isFrozen(parsed.requirements[0]), true);
});
test("错误信息不泄露额外字段值与非有限 revision", () => {
  const request = corpus.cases[0]!.value as ReviewRequest;
  assert.throws(() => parseReviewRequest({ ...request, revision: Infinity }), ReviewRequestError);
  assert.throws(() => parseReviewRequest({ ...request, private_secret_marker: "private_secret_marker" }), (error: unknown) => {
    assert.ok(error instanceof ReviewRequestError);
    assert.equal(JSON.stringify(error).includes("private_secret_marker"), false);
    return true;
  });
});
