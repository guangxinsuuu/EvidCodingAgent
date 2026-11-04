import { Ajv2020 } from "ajv/dist/2020.js";
import formatsPackage from "ajv-formats";
import { ContractError } from "./errors.js";

/** 仅定义传输信封；不携带可信 actor/tenant，也不代表命令已经获准执行。 */
export interface CommandEnvelope {
  readonly schema_version: 1;
  readonly command_id: string;
  readonly trace_id: string;
  readonly expected_version: number;
}

const canonicalUuid = Object.freeze({
  type: "string",
  format: "uuid",
  // format 之外限定规范文本：不接受 URN、大小写别名等多种表示。
  pattern:
    "^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
});

export const commandEnvelopeSchema = Object.freeze({
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "urn:egap:command-envelope:1",
  type: "object",
  additionalProperties: false,
  required: Object.freeze([
    "schema_version",
    "command_id",
    "trace_id",
    "expected_version",
  ]),
  properties: Object.freeze({
    schema_version: Object.freeze({ type: "integer", const: 1 }),
    command_id: canonicalUuid,
    trace_id: canonicalUuid,
    expected_version: Object.freeze({
      type: "integer",
      minimum: 0,
      maximum: Number.MAX_SAFE_INTEGER,
    }),
  }),
});

// compile 在 import 时只建立内存校验器，不访问外部资源或执行命令。
// 不纠正输入：错误版本、额外字段和字符串数字都必须由调用者显式修正。
const ajv = new Ajv2020({
  strict: true,
  allErrors: true,
  coerceTypes: false,
  useDefaults: false,
  removeAdditional: false,
});
// 锁定的 ajv-formats 3.x 是 CommonJS：module.exports 是插件函数，且
// exports.default 指回该函数。显式取 default 同时符合其声明和 NodeNext 运行时，
// 不通过 any 强转掩盖模块互操作问题；升级依赖时由编译与格式负例共同验证。
formatsPackage.default(ajv, ["uuid"]);
const validate = ajv.compile<CommandEnvelope>(commandEnvelopeSchema);

/**
 * 未来 HTTP/Worker 入口 → 本函数 → 领域处理；当前尚未注册业务命令路由。
 * 校验 unknown，并返回独立冻结副本，避免调用者随后修改原对象改变已验输入。
 * 校验失败只返回固定安全错误，不转发 Ajv 中可能包含输入属性名的详情。
 */
export function parseCommandEnvelope(value: unknown): Readonly<CommandEnvelope> {
  if (!validate(value)) {
    throw new ContractError("INVALID_COMMAND_ENVELOPE");
  }

  return Object.freeze({
    schema_version: value.schema_version,
    command_id: value.command_id,
    trace_id: value.trace_id,
    expected_version: value.expected_version,
  });
}
