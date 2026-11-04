/**
 * 第05章文件型实验身份；只用于单机校准，不获得第10章数据库Runtime资格。
 * lab-ledger分配身份 → 本模块校验持久化投影 → provisioner/后续adapter消费。
 * ID与摘要描述记录身份，不是权限凭据，也不证明实验已经执行或通过。
 */
export interface LabIdentity {
  readonly experiment_id: string;
  readonly run_id: string;
  readonly check_run_id: string;
  readonly execution_attempt_id: string;
  readonly authority_mode: "file-lab";
  readonly evidence_scope: "calibration-only";
}

/**
 * provisioner实测并登记的环境投影；不能用规划端口/镜像填充真实运行记录。
 * 本阶段尚无第06章Profile，environment_profile_digest必须保留null。
 * image_digests与内容摘要均使用裸64位小写hex，Docker适配器负责转换其前缀。
 */
export interface EnvironmentInstance {
  readonly environment_instance_id: string;
  readonly authority_mode: "file-lab";
  readonly evidence_scope: "calibration-only";
  readonly environment_config_digest: string;
  readonly environment_profile_digest: null;
  readonly source_commit: string;
  readonly build_revision: string;
  readonly fixture_reset_id: string;
  readonly lease_epoch: number;
  readonly endpoints: Readonly<Record<string, string>>;
  readonly image_digests: Readonly<Record<string, string>>;
  readonly observed_topology_ref: string;
  readonly schema_mapping_ref: string | null;
  readonly resource_manifest_ref: string;
  readonly expires_at: string;
}

const UUID_V4 = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}";
const SHA256 = /^[0-9a-f]{64}$/;
const RECORD_KEY = /^[a-z][a-z0-9_-]{0,63}$/;
const IDENTITY_KEYS = [
  "experiment_id", "run_id", "check_run_id", "execution_attempt_id",
  "authority_mode", "evidence_scope",
] as const;
const ENVIRONMENT_KEYS = [
  "environment_instance_id", "authority_mode", "evidence_scope",
  "environment_config_digest", "environment_profile_digest", "source_commit",
  "build_revision", "fixture_reset_id", "lease_epoch", "endpoints", "image_digests",
  "observed_topology_ref", "schema_mapping_ref", "resource_manifest_ref", "expires_at",
] as const;

/** 固定错误不拼接原始值，避免URL、未知字段或错误输入中的秘密进入日志。 */
function invalid(): never {
  throw new TypeError("Invalid laboratory contract.");
}

/**
 * 只接受JSON式普通对象；拒绝访问器、symbol与隐藏字段，避免读取字段时触发getter。
 * 不删除额外字段或转换类型，协议变化必须显式更新双方而不能静默兼容。
 */
function record(value: unknown, keys?: readonly string[]): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) invalid();
  const prototype: unknown = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) invalid();
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const actualKeys = Reflect.ownKeys(value);
  if (actualKeys.some((key) => typeof key !== "string")) invalid();
  if (Object.values(descriptors).some((descriptor) =>
    !descriptor.enumerable || !("value" in descriptor))) invalid();
  if (keys && (actualKeys.length !== keys.length || actualKeys.some((key) => !keys.includes(String(key))))) invalid();
  return value as Record<string, unknown>;
}

function text(value: unknown, pattern: RegExp): string {
  // JS正则的$可匹配末尾换行之前；额外拒绝首尾空白，确保身份/摘要只有一种文本形式。
  if (typeof value !== "string" || value.trim() !== value || !pattern.test(value)) invalid();
  return value;
}

function identifier(value: unknown, prefix: string): string {
  return text(value, new RegExp(`^${prefix}_${UUID_V4}$`));
}

function scope(value: Record<string, unknown>): void {
  if (value.authority_mode !== "file-lab" || value.evidence_scope !== "calibration-only") invalid();
}

/**
 * 引用仅标识项目内证据位置；此函数不访问文件，也不证明引用已存在。
 * 后续读取方仍须校验实际根目录、symlink和内容摘要，不能把此形状检查当文件授权。
 */
function evidenceRef(value: unknown): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 512 ||
      value.trim() !== value || !/^[A-Za-z0-9_./-]+$/.test(value) || value.startsWith("/")) invalid();
  if (value.split("/").some((part) => part === "" || part === "." || part === "..")) invalid();
  return value;
}

function endpoint(value: unknown): string {
  if (typeof value !== "string" || value.length > 2048 || !/^https?:\/\//.test(value) || /\s/.test(value)) invalid();
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return invalid();
  }
  // endpoint是服务定位符，不承载认证信息；不能把URL中秘密拷入持久化快照。
  if (!parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash) invalid();
  return value;
}

function stringMap(value: unknown, parseValue: (item: unknown) => string): Readonly<Record<string, string>> {
  const source = record(value);
  if (Object.keys(source).length > 256) invalid();
  const entries = Object.entries(source).map(([key, item]) => {
    if (key.trim() !== key || !RECORD_KEY.test(key) || key === "constructor" || key === "prototype") invalid();
    return [key, parseValue(item)] as const;
  });
  // 独立复制再冻结，不能让原对象在校验后变更已登记端口或镜像摘要。
  return Object.freeze(Object.fromEntries(entries));
}

/** 采用Date.toISOString()规范UTC形式；到期与否由持锁的环境动作入口判断。 */
function instant(value: unknown): string {
  const result = text(value, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  const timestamp = Date.parse(result);
  if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString() !== result) invalid();
  return result;
}

/**
 * 文件账本恢复/调用方输入 → 本函数 → 不可变实验身份。
 * 拒绝前缀串用、伪造authority mode和额外字段；不检查该身份是否已登记。
 */
export function parseLabIdentity(value: unknown): Readonly<LabIdentity> {
  const source = record(value, IDENTITY_KEYS);
  scope(source);
  return Object.freeze({
    experiment_id: identifier(source.experiment_id, "experiment"),
    run_id: identifier(source.run_id, "run"),
    check_run_id: identifier(source.check_run_id, "check"),
    execution_attempt_id: identifier(source.execution_attempt_id, "attempt"),
    authority_mode: "file-lab",
    evidence_scope: "calibration-only",
  });
}

/**
 * provisioner登记/账本恢复 → 严格形状校验 → 独立冻结的EnvironmentInstance。
 * 此函数无IO；不判定端口归属、资源实际存在、环境健康或当前epoch资格。
 * ledger的环境动作入口必须另外核对登记身份、期望epoch和expires_at。
 */
export function parseEnvironmentInstance(value: unknown): Readonly<EnvironmentInstance> {
  const source = record(value, ENVIRONMENT_KEYS);
  scope(source);
  if (source.environment_profile_digest !== null ||
      typeof source.lease_epoch !== "number" || !Number.isSafeInteger(source.lease_epoch) ||
      source.lease_epoch < 1) invalid();
  return Object.freeze({
    environment_instance_id: identifier(source.environment_instance_id, "environment"),
    authority_mode: "file-lab",
    evidence_scope: "calibration-only",
    environment_config_digest: text(source.environment_config_digest, SHA256),
    environment_profile_digest: null,
    source_commit: text(source.source_commit, /^[0-9a-f]{40}$/),
    build_revision: text(source.build_revision, /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/),
    fixture_reset_id: identifier(source.fixture_reset_id, "fixture"),
    lease_epoch: source.lease_epoch,
    endpoints: stringMap(source.endpoints, endpoint),
    image_digests: stringMap(source.image_digests, (item) => text(item, SHA256)),
    observed_topology_ref: evidenceRef(source.observed_topology_ref),
    schema_mapping_ref: source.schema_mapping_ref === null ? null : evidenceRef(source.schema_mapping_ref),
    resource_manifest_ref: evidenceRef(source.resource_manifest_ref),
    expires_at: instant(source.expires_at),
  });
}
