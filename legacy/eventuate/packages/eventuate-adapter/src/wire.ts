/**
 * 固定基线的 Money JSON 是 {amount: number}，输入也接受十进制字符串。
 * 已用预检 bootJAR 内 Money 与 Jackson 2/3 实际执行往返探针确认；
 * 这里不把 BigDecimal 或 Java Long 先转成 JS number，以免不可恢复地舍入。
 */
export function decimalMoney(value: unknown): string {
  if (typeof value !== "string" || !/^(0|[1-9][0-9]{0,17})(\.[0-9]{1,18})?$/.test(value)) {
    throw new TypeError("Invalid exact decimal money.");
  }
  const [whole, fraction = ""] = value.split(".");
  // 当前四个 API smoke 只用两位小数；多余的零可以规范化，非零精度不能截断。
  if (/[^0]/.test(fraction.slice(2))) throw new TypeError("Unsupported monetary precision.");
  return `${whole}.${fraction.slice(0, 2).padEnd(2, "0")}`;
}

/** 只接受正 Java Long 的十进制文本，既可安全拼接路径，也可发送原始 JSON 整数。 */
export function longId(value: unknown): string {
  if (typeof value !== "string" || !/^[1-9][0-9]{0,18}$/.test(value) ||
      BigInt(value) > 9_223_372_036_854_775_807n) {
    throw new TypeError("Invalid positive Java Long identifier.");
  }
  return value;
}

/**
 * JSON.parse 的新 reviver source 能保留数值原文；Node 25 是本仓库固定运行环境。
 * 若宿主缺少 source 支持则拒绝数值，不静默回退到已经舍入的 JS number。
 * 返回的所有 JSON 数字都是原始十进制文本；字段解析仍须单独验证其业务类型。
 */
export function parseExactJson(text: string): unknown {
  type SourceContext = { source?: string };
  const parse = JSON.parse as (source: string, reviver: (
    key: string, value: unknown, context?: SourceContext,
  ) => unknown) => unknown;
  return parse(text, (_key, value, context) => {
    if (typeof value !== "number") return value;
    if (typeof context?.source !== "string") throw new TypeError("Exact JSON number source is unavailable.");
    return context.source;
  });
}

export function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError("Expected response object.");
  }
  return value as Record<string, unknown>;
}
