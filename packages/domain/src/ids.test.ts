import assert from "node:assert/strict";
import { test } from "node:test";
import {
  newCandidateId,
  newCheckId,
  newRunId,
  parseCandidateId,
  parseCheckId,
  parseRunId,
} from "./ids.js";

test("每种生成器产出可由自身解析器读取的标识", () => {
  const run = newRunId();
  const candidate = newCandidateId();
  const check = newCheckId();
  assert.equal(parseRunId(run), run);
  assert.equal(parseCandidateId(candidate), candidate);
  assert.equal(parseCheckId(check), check);
});

test("实体类型混用与非法 UUID 在运行时被拒绝", () => {
  const candidate = newCandidateId();
  assert.throws(() => parseRunId(candidate), TypeError);
  assert.throws(() => parseCheckId(candidate), TypeError);
  assert.throws(() => parseCandidateId(newRunId()), TypeError);
  assert.throws(() => parseRunId("run_not-a-uuid"), TypeError);
  assert.throws(() => parseRunId(null), TypeError);
  assert.throws(() => parseRunId("run_b700a836-eb63-1b59-a9f8-bc37f3011275"), TypeError);
});
