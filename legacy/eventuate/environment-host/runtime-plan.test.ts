import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { API_ROLES, makeRuntimePlan, materializeSpec, planDigest, ROLES } from "./runtime-plan.js";
import type { RoleSpec, RuntimeConfig } from "./runtime-plan.js";

// 测试验证批准拓扑的关键业务/隔离不变量，不复制整个生成器输出作为快照。
const config = JSON.parse(readFileSync(new URL("../../../config/lab-eventuate-postgres.json", import.meta.url), "utf8")) as RuntimeConfig;
function memoryMiB(value: string | undefined): number {
  assert.match(value ?? "", /^[1-9][0-9]*[mg]$/); const amount = Number(value!.slice(0, -1)); return value!.endsWith("g") ? amount * 1024 : amount;
}
function tmpfsPaths(spec: RoleSpec): string[] { return (spec.tmpfs ?? []).map(value => value.split(":", 1)[0]!); }

test("两个业务数据库独立，WAL双reader与双pipeline逐个关联真实业务数据源", () => {
  const { plan } = makeRuntimePlan(config), { services } = plan;
  const databaseRoles = ["customer-service-db", "order-service-db"] as const;
  assert.notEqual(services[databaseRoles[0]].networkAlias, services[databaseRoles[1]].networkAlias);
  const cdc = services.cdc.env!;
  const readers = Object.keys(cdc).filter(key => /^EVENTUATE_CDC_READER_READER\d+_TYPE$/.test(key));
  const pipelines = Object.keys(cdc).filter(key => /^EVENTUATE_CDC_PIPELINE_PIPELINE\d+_TYPE$/.test(key));
  assert.equal(readers.length, 2); assert.equal(pipelines.length, 2);
  const sourceUrls = new Set<string>(), outboxIds = new Set<string>(), leadershipPaths = new Set<string>();
  for (const [index, role] of databaseRoles.entries()) {
    const readerPrefix = `EVENTUATE_CDC_READER_READER${index + 1}_`, pipelinePrefix = `EVENTUATE_CDC_PIPELINE_PIPELINE${index + 1}_`;
    const serviceRole = role === "customer-service-db" ? "customer-service" : "order-service";
    assert.equal(cdc[readerPrefix + "TYPE"], "postgres-wal");
    assert.equal(cdc[readerPrefix + "DATASOURCEURL"], services[serviceRole].env!.SPRING_DATASOURCE_URL);
    assert.equal(new URL(cdc[readerPrefix + "DATASOURCEURL"]!.replace(/^jdbc:/, "")).hostname, services[role].networkAlias);
    assert.equal(cdc[pipelinePrefix + "TYPE"], "eventuate-tram"); assert.equal(cdc[pipelinePrefix + "READER"], `reader${index + 1}`);
    assert.equal(cdc[pipelinePrefix + "EVENTUATEDATABASESCHEMA"], services[serviceRole].env!.EVENTUATE_DATABASE_SCHEMA);
    sourceUrls.add(cdc[readerPrefix + "DATASOURCEURL"]!); outboxIds.add(cdc[readerPrefix + "OUTBOXID"]!); leadershipPaths.add(cdc[readerPrefix + "LEADERSHIPLOCKPATH"]!);
  }
  assert.equal(sourceUrls.size, 2); assert.equal(outboxIds.size, 2); assert.equal(leadershipPaths.size, 2);
  assert.equal(services["customer-service"].env!.EVENTUATELOCAL_KAFKA_BOOTSTRAP_SERVERS, services["order-service"].env!.EVENTUATELOCAL_KAFKA_BOOTSTRAP_SERVERS);
  assert.equal(cdc.EVENTUATELOCAL_KAFKA_BOOTSTRAP_SERVERS, "kafka:9093");
});

test("持久化计划不含真实口令；只在提交Docker前替换secret引用且不污染原计划", () => {
  const { plan, password } = makeRuntimePlan(config); const before = JSON.stringify(plan); const digest = planDigest(plan);
  assert.match(password, /^[0-9a-f]{48}$/); assert.ok(!before.includes(password));
  assert.equal(plan.credential_binding_digest, createHash("sha256").update(password).digest("hex"));
  let secretBindings = 0;
  for (const role of ROLES) {
    const spec = plan.services[role], materialized = materializeSpec(spec, password);
    for (const [key, value] of Object.entries(spec.env ?? {})) {
      if (key.endsWith("PASSWORD")) { secretBindings++; assert.equal(value, "$LAB_DB_PASSWORD"); assert.equal(materialized.env?.[key], password); }
      else assert.equal(materialized.env?.[key], value);
    }
    assert.ok(!Object.values(materialized.env ?? {}).includes("$LAB_DB_PASSWORD"));
  }
  assert.equal(secretBindings, 6); assert.equal(JSON.stringify(plan), before); assert.equal(planDigest(plan), digest);
  const unrelated = materializeSpec({ image: config.images.java, env: { LITERAL: "prefix-$LAB_DB_PASSWORD-suffix" } }, password);
  assert.equal(unrelated.env?.LITERAL, "prefix-$LAB_DB_PASSWORD-suffix");
  const next = makeRuntimePlan(config); assert.notEqual(next.password, password); assert.notEqual(next.plan.deployment_key, plan.deployment_key);
  assert.notEqual(next.plan.services.cdc.env!.EVENTUATE_CDC_READER_READER1_LEADERSHIPLOCKPATH, plan.services.cdc.env!.EVENTUATE_CDC_READER_READER1_LEADERSHIPLOCKPATH);
});

test("七角色有界内存/CPU/PID；批准镜像均绑定digest并保持内网数据库和broker", () => {
  const { plan } = makeRuntimePlan(config); assert.equal(Object.keys(plan.services).length, 7);
  assert.equal(plan.config.network_internal, true); assert.equal(plan.config.authority_mode, "file-lab"); assert.equal(plan.config.evidence_scope, "calibration-only");
  let memory = 0, cpus = 0, pids = 0;
  for (const role of ROLES) {
    const spec = plan.services[role]; assert.match(spec.image, /^[a-z0-9/_.-]+@sha256:[a-f0-9]{64}$/);
    assert.ok(Object.values(config.images).includes(spec.image));
    memory += memoryMiB(spec.memory); assert.ok((spec.cpus ?? 0) > 0); cpus += spec.cpus!;
    assert.ok(Number.isInteger(spec.pidsLimit) && spec.pidsLimit! > 0); pids += spec.pidsLimit!;
    assert.ok(!("mounts" in spec) && !("volumes" in spec) && !("privileged" in spec) && !("networkMode" in spec));
  }
  // 当前本机8GiB/12CPU预算必须给Docker和宿主留余量，不允许无界默认值。
  assert.ok(memory <= 6 * 1024); assert.ok(cpus <= 8); assert.ok(pids <= 2048);
  for (const role of ["customer-service-db", "order-service-db", "kafka"] as const) assert.equal(plan.services[role].ports, undefined);
  assert.ok(config.startup_timeout_ms <= 300_000 && config.request_timeout_ms < config.order_timeout_ms && config.poll_interval_ms < config.request_timeout_ms);
  assert.ok(config.environment_ttl_ms <= 7_200_000);
});

test("PG/Kafka镜像Volume目录被tmpfs覆盖，避免产生未登记匿名volume", () => {
  const { services } = makeRuntimePlan(config).plan;
  for (const role of ["customer-service-db", "order-service-db"] as const) {
    const spec = services[role]; assert.equal(spec.user, "999:999"); assert.ok(tmpfsPaths(spec).includes("/var/lib/postgresql"));
    assert.ok(spec.env!.PGDATA!.startsWith("/var/lib/postgresql/"));
    assert.ok(spec.tmpfs!.find(value => value.startsWith("/var/lib/postgresql:"))?.includes("mode=1777"));
  }
  for (const path of ["/etc/kafka/secrets", "/mnt/shared/config"]) assert.ok(tmpfsPaths(services.kafka).includes(path));
  for (const spec of Object.values(services)) {
    assert.ok(tmpfsPaths(spec).includes("/tmp"));
    for (const value of spec.tmpfs ?? []) { assert.match(value, /(?:^|,)size=[1-9][0-9]*[mg](?:,|$)/); assert.ok(value.includes("nosuid") && value.includes("nodev")); }
  }
});

test("业务JAR由非root Java入口运行，所有角色drop ALL且没有宿主路径绑定", () => {
  const { services } = makeRuntimePlan(config).plan;
  for (const role of API_ROLES) {
    const spec = services[role]; assert.equal(spec.image, config.images.java); assert.match(spec.user!, /^[1-9][0-9]*:[1-9][0-9]*$/);
    assert.equal(spec.entrypoint, "java"); const jarFlag = spec.command!.indexOf("-jar"); assert.ok(jarFlag >= 0); assert.equal(spec.command![jarFlag + 1], "/opt/service.jar");
    assert.ok(spec.command!.some(value => /^-Xmx[1-9][0-9]*m$/.test(value))); assert.deepEqual(spec.ports, [8080]);
  }
  for (const spec of Object.values(services)) {
    assert.deepEqual(spec.capDrop, ["ALL"]);
    // 此次SUT明确需要可写容器层；不能在报告中把无宿主bind等同于read-only rootfs。
    assert.equal(spec.readOnly, false); assert.ok(!JSON.stringify(spec).includes("docker.sock")); assert.ok(!JSON.stringify(spec).includes("/Users/"));
  }
});
