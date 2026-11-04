const ERROR_DEFINITIONS = {
  INVALID_COMMAND_ENVELOPE: {
    message: "命令信封不符合当前协议。",
    retryable: false,
  },
  INVALID_OPERATION_REFERENCE: {
    message: "操作关联标识不符合协议。",
    retryable: false,
  },
  INVALID_CONFIGURATION: {
    message: "控制面配置无效。",
    retryable: false,
  },
  INTERNAL_ERROR: {
    message: "请求未能完成。",
    // 未知异常并不证明操作没有执行，不能鼓励客户端盲目重试。
    retryable: false,
  },
} as const;

export type ErrorCode = keyof typeof ERROR_DEFINITIONS;

/** 允许跨进程返回的最小错误对象；不包含堆栈、原始请求或内部异常。 */
export interface PublicError {
  readonly error_code: ErrorCode;
  readonly message: string;
  readonly retryable: boolean;
  readonly operation_ref: string | null;
}

const OPERATION_REFERENCE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/**
 * 由协议解析器或控制面创建，再通过 toPublicError 形成对外安全投影。
 * 调用者只选择固定错误码，不能将输入值或异常 message 拼进公开信息。
 */
export class ContractError extends Error {
  readonly error_code: ErrorCode;
  readonly retryable: boolean;

  constructor(code: ErrorCode) {
    super(ERROR_DEFINITIONS[code].message);
    this.name = "ContractError";
    this.error_code = code;
    this.retryable = ERROR_DEFINITIONS[code].retryable;
  }

  /**
   * operationRef 必须是可信服务分配的规范 UUID；缺省为 null。
   * 非法关联值显式拒绝，避免把 URL、token 等任意字符串带入错误响应。
   */
  toPublicError(operationRef: string | null = null): Readonly<PublicError> {
    if (operationRef !== null && !OPERATION_REFERENCE.test(operationRef)) {
      throw new ContractError("INVALID_OPERATION_REFERENCE");
    }

    return Object.freeze({
      error_code: this.error_code,
      message: ERROR_DEFINITIONS[this.error_code].message,
      retryable: this.retryable,
      operation_ref: operationRef,
    });
  }
}
