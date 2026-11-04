import assert from "node:assert/strict";
import { test } from "node:test";
import { ContractError } from "./errors.js";
import { parseCommandEnvelope } from "./envelope.js";

const envelope = {
  schema_version: 1,
  command_id: "b700a836-eb63-4b59-a9f8-bc37f3011275",
  trace_id: "b411e833-bd88-4ac5-86cf-d88b06ed310f",
  expected_version: 0,
};

test("合法信封返回冻结副本，原输入后续变更不改变已验值", () => {
  const input = { ...envelope };
  const parsed = parseCommandEnvelope(input);
  input.expected_version = 9;
  assert.equal(parsed.expected_version, 0);
  assert.equal(Object.isFrozen(parsed), true);
});

test("严格拒绝错误版本、缺字段、额外身份、非法 UUID 和非安全版本号", () => {
  const { trace_id: _omitted, ...missingTrace } = envelope;
  const invalidInputs: unknown[] = [
    null,
    [],
    missingTrace,
    { ...envelope, schema_version: 2 },
    { ...envelope, schema_version: "1" },
    { ...envelope, actor: "admin-secret" },
    { ...envelope, trace_id: "not-a-uuid" },
    { ...envelope, command_id: `urn:uuid:${envelope.command_id}` },
    ...["0", -1, 0.5, Number.MAX_SAFE_INTEGER + 1, Infinity, NaN].map(
      (expected_version) => ({ ...envelope, expected_version }),
    ),
  ];

  for (const value of invalidInputs) {
    assert.throws(() => parseCommandEnvelope(value), (error: unknown) => {
      assert.ok(error instanceof ContractError);
      assert.equal(error.error_code, "INVALID_COMMAND_ENVELOPE");
      assert.equal(error.message.includes("admin-secret"), false);
      return true;
    });
  }
});

test("拒绝输入时不静默删除额外字段或转换数值类型", () => {
  const input = { ...envelope, expected_version: "0", actor: "operator" };
  assert.throws(() => parseCommandEnvelope(input), ContractError);
  assert.equal(input.expected_version, "0");
  assert.equal(input.actor, "operator");
});

test("公开错误不含堆栈与内部字段；非法关联值不能进入响应", () => {
  const error = new ContractError("INTERNAL_ERROR");
  assert.deepEqual(error.toPublicError(), {
    error_code: "INTERNAL_ERROR",
    message: "请求未能完成。",
    retryable: false,
    operation_ref: null,
  });
  assert.equal(error.toPublicError(envelope.command_id).operation_ref, envelope.command_id);
  assert.throws(() => error.toPublicError("https://host/?token=secret"), {
    name: "ContractError",
    message: "操作关联标识不符合协议。",
  });
});
