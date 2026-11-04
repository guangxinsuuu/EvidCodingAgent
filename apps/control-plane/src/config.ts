/** 第04章配置边界：main.ts 在创建任何监听句柄之前调用 parseConfig。 */
export type LogLevel = "silent" | "fatal" | "error" | "warn" | "info" | "debug" | "trace";

export interface ControlPlaneConfig {
  readonly mode: "local-dev";
  readonly host: "127.0.0.1" | "::1";
  readonly port: number;
  readonly logLevel: LogLevel;
  /** 逻辑引用预留给后续持久层；本期不解析凭据或连接数据库。 */
  readonly databaseRef: string | null;
}

type ConfigReason = "REQUIRED" | "UNSUPPORTED_MODE" | "LOOPBACK_REQUIRED" | "INVALID_PORT" | "INVALID_LOG_LEVEL" | "INVALID_REFERENCE" | "UNKNOWN_KEY";
const allowedKeys = new Set(["EGAP_MODE", "EGAP_HOST", "EGAP_PORT", "EGAP_LOG_LEVEL", "EGAP_DATABASE_REF"]);
const logLevels = new Set<LogLevel>(["silent", "fatal", "error", "warn", "info", "debug", "trace"]);

/** 只携带字段与固定原因，日志不能回显可能含凭据的配置原值。 */
export class ConfigurationError extends Error {
  readonly error_code = "INVALID_CONFIGURATION";
  constructor(readonly field: string, readonly reason: ConfigReason) {
    super(`配置校验失败：${field}（${reason}）`);
    this.name = "ConfigurationError";
  }
}

/**
 * 把进程环境转换成不可变配置；不读文件、改环境或建立外部连接。
 * MODE/PORT 必须显式给定，HOST 默认回环。拒绝未知 EGAP_* 可抓住拼写
 * 错误；普通系统环境变量不属于本服务配置，不能把整个 process.env 拒绝。
 */
export function parseConfig(environment: Readonly<Record<string, string | undefined>>): ControlPlaneConfig {
  for (const key of Object.keys(environment)) {
    if (key.startsWith("EGAP_") && !allowedKeys.has(key)) {
      throw new ConfigurationError("EGAP_*", "UNKNOWN_KEY");
    }
  }
  const mode = environment["EGAP_MODE"];
  if (!mode) throw new ConfigurationError("EGAP_MODE", "REQUIRED");
  if (mode !== "local-dev") throw new ConfigurationError("EGAP_MODE", "UNSUPPORTED_MODE");

  const host = environment["EGAP_HOST"] ?? "127.0.0.1";
  // 使用明确IP而不是localhost解析结果；不能通过DNS或0.0.0.0扩大监听面。
  if (host !== "127.0.0.1" && host !== "::1") {
    throw new ConfigurationError("EGAP_HOST", "LOOPBACK_REQUIRED");
  }
  const rawPort = environment["EGAP_PORT"];
  if (!rawPort) throw new ConfigurationError("EGAP_PORT", "REQUIRED");
  if (!/^[1-9][0-9]{0,4}$/.test(rawPort) || Number(rawPort) > 65_535) {
    throw new ConfigurationError("EGAP_PORT", "INVALID_PORT");
  }
  const logLevel = environment["EGAP_LOG_LEVEL"] ?? "info";
  if (!logLevels.has(logLevel as LogLevel)) {
    throw new ConfigurationError("EGAP_LOG_LEVEL", "INVALID_LOG_LEVEL");
  }
  const databaseRef = environment["EGAP_DATABASE_REF"] ?? null;
  if (databaseRef !== null && !/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(databaseRef)) {
    throw new ConfigurationError("EGAP_DATABASE_REF", "INVALID_REFERENCE");
  }
  return Object.freeze({ mode, host, port: Number(rawPort), logLevel: logLevel as LogLevel, databaseRef });
}
