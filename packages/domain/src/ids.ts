import { randomUUID } from "node:crypto";

declare const entityIdBrand: unique symbol;
type IdKind = "run" | "candidate" | "check";
type EntityId<Kind extends IdKind> = `${Kind}_${string}` & {
  readonly [entityIdBrand]: Kind;
};

export type RunId = EntityId<"run">;
export type CandidateId = EntityId<"candidate">;
export type CheckId = EntityId<"check">;

const UUID_V4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/**
 * 外部存储/接口读到字符串后，经对应 parseXId 才能进入领域函数。
 * 实体前缀防止运行时串用，brand 防止编译期误传；ID 本身不是访问凭据。
 */
function parseId<Kind extends IdKind>(kind: Kind, value: unknown): EntityId<Kind> {
  const prefix = `${kind}_`;
  if (
    typeof value !== "string" ||
    !value.startsWith(prefix) ||
    !UUID_V4.test(value.slice(prefix.length))
  ) {
    // 不把原始值拼进异常，调用方可能传入错误字段或秘密。
    throw new TypeError(`Invalid ${kind} identifier.`);
  }
  return value as EntityId<Kind>;
}

/** 生成新的 Run 身份；随机性只在显式调用时发生，import 无创建副作用。 */
export function newRunId(): RunId {
  return parseRunId(`run_${randomUUID()}`);
}

/** 生成新候选身份；候选内容摘要与这个记录 ID 是两个不同概念。 */
export function newCandidateId(): CandidateId {
  return parseCandidateId(`candidate_${randomUUID()}`);
}

/** 生成检查记录身份；检查幂等键将在后续 Runtime 中独立实现。 */
export function newCheckId(): CheckId {
  return parseCheckId(`check_${randomUUID()}`);
}

/** 校验规范的 run_ + 小写 UUIDv4；失败抛出不含原始值的 TypeError。 */
export function parseRunId(value: unknown): RunId {
  return parseId("run", value);
}

/** 校验 Candidate 身份，拒绝误传 Run/Check 标识。 */
export function parseCandidateId(value: unknown): CandidateId {
  return parseId("candidate", value);
}

/** 校验 Check 身份，拒绝误传 Run/Candidate 标识。 */
export function parseCheckId(value: unknown): CheckId {
  return parseId("check", value);
}
