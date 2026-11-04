import { createHash, randomBytes, randomUUID } from "node:crypto";
import { readFile, lstat } from "node:fs/promises";
import { join, resolve, relative } from "node:path";
import { labDigest } from "./lab-ledger.js";
import type { DockerContainerSpec } from "./docker-host.js";

export const ROLES = ["customer-service-db", "order-service-db", "kafka", "customer-service", "order-service", "api-gateway-service", "cdc"] as const;
export type RuntimeRole = typeof ROLES[number];
export const API_ROLES = ["customer-service", "order-service", "api-gateway-service"] as const;
export interface RuntimeConfig {
  schema_version: 1; runtime_revision: string; source_commit: string; upstream_lock_sha256: string;
  build_revision: string; platform: "linux/arm64"; images: Record<"java" | "postgres" | "kafka" | "cdc", string>;
  artifacts: Record<typeof API_ROLES[number], { path: string; sha256: string }>;
  startup_timeout_ms: number; order_timeout_ms: number; request_timeout_ms: number; poll_interval_ms: number;
  environment_ttl_ms: number; network_internal: true; authority_mode: "file-lab"; evidence_scope: "calibration-only";
}
export type RoleSpec = Omit<DockerContainerSpec, "name" | "networkId">;
export interface RuntimePlan {
  config: RuntimeConfig; deployment_key: string; credential_binding_digest: string;
  services: Record<RuntimeRole, RoleSpec>;
}
const PASSWORD_REF = "$LAB_DB_PASSWORD";
export function sha256(bytes: Uint8Array): string { return createHash("sha256").update(bytes).digest("hex"); }
export function runtimeAssert(condition: unknown, code: string): asserts condition { if (!condition) throw new Error(code); }

/** 路径只能来自项目内已批准配置；逐层拒绝链接，不允许把用户目录作为构建输入。 */
export async function checkedProjectFile(project: string, ref: string): Promise<string> {
  runtimeAssert(/^[A-Za-z0-9_./-]+$/.test(ref) && !ref.startsWith("/") && ref.split("/").every(p => p !== ".." && p !== "." && p !== ""), "INVALID_RUNTIME_PATH");
  let current = resolve(project);
  for (const part of ref.split("/")) { current = join(current, part); runtimeAssert(!(await lstat(current)).isSymbolicLink(), "RUNTIME_SYMLINK_REJECTED"); }
  runtimeAssert((await lstat(current)).isFile() && !relative(resolve(project), current).startsWith(".."), "INVALID_RUNTIME_FILE");
  return current;
}

/** 入口校验来源锁/完整基线和已冻结JAR；不重新运行候选Gradle或上游Dockerfile。 */
export async function loadRuntimeConfig(project: string, ref: string): Promise<RuntimeConfig> {
  runtimeAssert(ref === "config/lab-eventuate-postgres.json", "UNREVIEWED_RUNTIME_CONFIG");
  const config = JSON.parse(await readFile(await checkedProjectFile(project, ref), "utf8")) as RuntimeConfig;
  runtimeAssert(config.schema_version === 1 && config.platform === "linux/arm64" && config.network_internal === true && config.authority_mode === "file-lab" && config.evidence_scope === "calibration-only", "INVALID_RUNTIME_CONFIG");
  runtimeAssert(config.source_commit === "894f2dbbe9aba2be6129748b6bbf5f30356deb07", "UNREVIEWED_SOURCE");
  for (const key of ["java", "postgres", "kafka", "cdc"] as const) runtimeAssert(typeof config.images[key] === "string" && /^[a-z0-9/_.-]+@sha256:[0-9a-f]{64}$/.test(config.images[key]), "IMMUTABLE_RUNTIME_IMAGE_REQUIRED");
  for (const key of ["startup_timeout_ms", "order_timeout_ms", "request_timeout_ms", "poll_interval_ms", "environment_ttl_ms"] as const) runtimeAssert(Number.isSafeInteger(config[key]) && config[key] > 0 && config[key] <= (key === "environment_ttl_ms" ? 7_200_000 : 300_000), "INVALID_RUNTIME_BUDGET");
  const lockBytes = await readFile(await checkedProjectFile(project, "sut/upstream.lock.json"));
  runtimeAssert(sha256(lockBytes) === config.upstream_lock_sha256, "SOURCE_LOCK_MISMATCH");
  const lock = JSON.parse(lockBytes.toString()) as { upstream_commit: string; build_revision: string; source_files: { path: string; effective_sha256: string }[] };
  runtimeAssert(lock.upstream_commit === config.source_commit && lock.build_revision === config.build_revision && lock.source_files.length === 190, "SOURCE_IDENTITY_MISMATCH");
  for (const file of lock.source_files) runtimeAssert(sha256(await readFile(await checkedProjectFile(project, `sut/workspaces/baseline/${file.path}`))) === file.effective_sha256, "BASELINE_SOURCE_DRIFT");
  // JAR身份同时与第03章生成记录对应，不能仅靠手改当前配置换掉业务实现。
  const originals = JSON.parse(await readFile(join(project, "artifacts/evidence/preflight/20261104T020503Z-build-preflight/artifact-inspection.json"), "utf8")) as { artifacts: { service: string; path: string; sha256: string }[] };
  for (const role of API_ROLES) {
    const artifact = config.artifacts[role]; const original = originals.artifacts.find(a => a.service === role);
    runtimeAssert(original && original.path === artifact.path && original.sha256 === artifact.sha256, "JAR_ORIGIN_MISMATCH");
    runtimeAssert(sha256(await readFile(await checkedProjectFile(project, artifact.path))) === artifact.sha256, "JAR_DIGEST_MISMATCH");
  }
  return config;
}

/**
 * 固定拓扑的可信生成器。保留上游PG WAL+双reader+双pipeline，不修改业务源码。
 * 环境独立凭据只写受保护secret文件；计划中用引用和不可逆摘要绑定实际凭据。
 * 所有镜像VOLUME路径显式tmpfs覆盖，不创建未登记匿名卷；测试数据随容器回收。
 */
export function makeRuntimePlan(config: RuntimeConfig): { plan: RuntimePlan; password: string } {
  const password = randomBytes(24).toString("hex"); const deployment = randomUUID();
  const common = { readOnly: false, capDrop: ["ALL"], cpus: 1, pidsLimit: 256, tmpfs: ["/tmp:rw,nosuid,nodev,size=256m,mode=1777"] };
  const db: RoleSpec = { ...common, image: config.images.postgres, user: "999:999", memory: "768m", env: { POSTGRES_DB: "eventuate", POSTGRES_USER: "postgresuser", POSTGRES_PASSWORD: PASSWORD_REF, PGDATA: "/var/lib/postgresql/18/docker" }, tmpfs: [...common.tmpfs, "/var/lib/postgresql:rw,nosuid,nodev,size=512m,mode=1777"] };
  const service = (role: "customer-service" | "order-service"): RoleSpec => ({ ...common, image: config.images.java, user: "10001:10001", memory: "768m", ports: [8080], entrypoint: "java", command: ["-Xms64m", "-Xmx384m", "-jar", "/opt/service.jar"], env: {
    SPRING_PROFILES_ACTIVE: "postgres", SPRING_DATASOURCE_URL: `jdbc:postgresql://${role}-db:5432/eventuate`, SPRING_DATASOURCE_USERNAME: "postgresuser", SPRING_DATASOURCE_PASSWORD: PASSWORD_REF,
    SPRING_DATASOURCE_DRIVER_CLASS_NAME: "org.postgresql.Driver", EVENTUATE_DATABASE_SCHEMA: "public", EVENTUATELOCAL_KAFKA_BOOTSTRAP_SERVERS: "kafka:9093", SERVER_PORT: "8080",
    MANAGEMENT_TRACING_EXPORT_ENABLED: "false", LOGGING_LEVEL_ROOT: "INFO", LOGGING_LEVEL_IO_EVENTUATE: "INFO", LOGGING_LEVEL_ORG_HIBERNATE_SQL: "WARN", LOGGING_LEVEL_ORG_SPRINGFRAMEWORK_BOOT_AUTOCONFIGURE: "WARN",
  } });
  const cdcEnv: Record<string, string> = { SPRING_PROFILES_ACTIVE: "KafkaLeadership", EVENTUATELOCAL_KAFKA_BOOTSTRAP_SERVERS: "kafka:9093", JAVA_OPTS: "-Xms64m -Xmx384m", MANAGEMENT_TRACING_EXPORT_ENABLED: "false", LOGGING_LEVEL_ROOT: "INFO" };
  ["customer-service-db", "order-service-db"].forEach((dbRole, index) => {
    const i = index + 1; const prefix = `EVENTUATE_CDC_READER_READER${i}_`;
    const values = { DATASOURCEURL: `jdbc:postgresql://${dbRole}:5432/eventuate`, DATASOURCEUSERNAME: "postgresuser", DATASOURCEPASSWORD: PASSWORD_REF, DATASOURCEDRIVERCLASSNAME: "org.postgresql.Driver", LEADERSHIPLOCKPATH: `/eventuate/cdc/leader/${deployment}/${dbRole}`, OUTBOXID: String(i), MONITORINGSCHEMA: "public", OFFSETSTORAGETOPICNAME: "db.history.common", TYPE: "postgres-wal" };
    for (const [key, value] of Object.entries(values)) cdcEnv[prefix + key] = value;
    cdcEnv[`EVENTUATE_CDC_PIPELINE_PIPELINE${i}_TYPE`] = "eventuate-tram";
    cdcEnv[`EVENTUATE_CDC_PIPELINE_PIPELINE${i}_READER`] = `reader${i}`;
    cdcEnv[`EVENTUATE_CDC_PIPELINE_PIPELINE${i}_EVENTUATEDATABASESCHEMA`] = "public";
  });
  const plan: RuntimePlan = { config, deployment_key: deployment, credential_binding_digest: sha256(Buffer.from(password)), services: {
    "customer-service-db": { ...db, networkAlias: "customer-service-db" }, "order-service-db": { ...db, networkAlias: "order-service-db" },
    kafka: { ...common, image: config.images.kafka, memory: "768m", networkAlias: "kafka", tmpfs: [...common.tmpfs, "/etc/kafka/secrets:rw,nosuid,nodev,size=16m,mode=1777", "/mnt/shared/config:rw,nosuid,nodev,size=16m,mode=1777"], env: {
      CLUSTER_ID: "4L6g3nShT-eMCtK--X86sw", KAFKA_NODE_ID: "1", KAFKA_PROCESS_ROLES: "broker,controller", KAFKA_LISTENERS: "PLAINTEXT://0.0.0.0:9092,BROKER://0.0.0.0:9093,CONTROLLER://0.0.0.0:9094",
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: "BROKER:PLAINTEXT,PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT", KAFKA_INTER_BROKER_LISTENER_NAME: "BROKER", KAFKA_CONTROLLER_LISTENER_NAMES: "CONTROLLER", KAFKA_CONTROLLER_QUORUM_VOTERS: "1@localhost:9094", KAFKA_ADVERTISED_LISTENERS: "PLAINTEXT://kafka:9092,BROKER://kafka:9093",
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: "1", KAFKA_OFFSETS_TOPIC_NUM_PARTITIONS: "1", KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: "1", KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: "1", KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: "0", KAFKA_LOG_FLUSH_INTERVAL_MESSAGES: "9223372036854775807", KAFKA_NUM_PARTITIONS: "1", KAFKA_LOG_SEGMENT_BYTES: "16777216", KAFKA_LOG_INDEX_SIZE_MAX_BYTES: "1048576",
    } },
    "customer-service": { ...service("customer-service"), networkAlias: "customer-service" },
    "order-service": { ...service("order-service"), networkAlias: "order-service" },
    "api-gateway-service": { ...common, image: config.images.java, memory: "768m", user: "10001:10001", ports: [8080], networkAlias: "api-gateway-service", entrypoint: "java", command: ["-Xms64m", "-Xmx384m", "-jar", "/opt/service.jar"], env: { ORDER_DESTINATIONS_ORDERSERVICEURL: "http://order-service:8080", CUSTOMER_DESTINATIONS_CUSTOMERSERVICEURL: "http://customer-service:8080", APIGATEWAY_TIMEOUT_MILLIS: "1000", MANAGEMENT_TRACING_EXPORT_ENABLED: "false", LOGGING_LEVEL_ORG_SPRINGFRAMEWORK_WEB: "INFO" } },
    cdc: { ...common, image: config.images.cdc, memory: "768m", user: "10001:10001", ports: [8080], networkAlias: "cdc", env: cdcEnv },
  } };
  return { plan, password };
}
/** 实际提交Docker之前替换唯一已知secret引用；此返回值不能写公开日志。 */
export function materializeSpec(spec: RoleSpec, password: string): RoleSpec {
  return { ...spec, env: Object.fromEntries(Object.entries(spec.env ?? {}).map(([key, value]) => [key, value === PASSWORD_REF ? password : value])) };
}
export const planDigest = (plan: RuntimePlan): string => labDigest(plan);
