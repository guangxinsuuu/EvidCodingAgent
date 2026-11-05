/**
 * support-lab.py → 本入口 → 独立运行账本 + DockerHost。
 * 来源参考账本保持原样；自有SUT以实际文件摘要绑定新的实验。创建意图先落盘，
 * 中断后不会自动重建；inspect/down先检查真实owner与资源身份。
 */
import { createHash, randomUUID } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync, readdirSync, lstatSync, existsSync, realpathSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { LabLedger, labDigest } from "./lab-ledger.js";
import { DockerHost } from "./docker-host.js";
import type { DockerContainer, DockerAudit } from "./docker-host.js";
import type { EnvironmentInstance } from "@egap/contracts";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const ledgerRoot = join(root, "artifacts/lab/genai-support-runtime");
const images = {
  postgres: "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
  redis: "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499",
} as const;
function demand(value: unknown, code: string): asserts value { if (!value) throw new Error(code); }
function save(path: string, value: unknown): void { writeFileSync(path, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 }); }
function sourceManifest(): Record<string, string> {
  const paths = ["services/genai-support-sut/src", "services/genai-support-sut/migrations", "datasets/genai-support/synthetic-v1/sut"];
  const files: string[] = ["services/genai-support-sut/pyproject.toml", "services/genai-support-sut/uv.lock"];
  function visit(path: string): void {
    if (!existsSync(join(root, path))) return;
    for (const name of readdirSync(join(root, path)).sort()) {
      if (name.startsWith(".") || name === "__pycache__") continue;
      const child = join(path, name); const stat = lstatSync(join(root, child)); demand(!stat.isSymbolicLink(), "SOURCE_SYMLINK_FORBIDDEN");
      if (stat.isDirectory()) visit(child); else if (/\.(?:py|json|sql)$/.test(name)) files.push(child);
    }
  }
  paths.forEach(visit);
  return Object.fromEntries(files.sort().map(path => [path, createHash("sha256").update(readFileSync(join(root, path))).digest("hex")]));
}
function observedPort(container: DockerContainer, port: number): number {
  const mapping = container.ports[`${port}/tcp`];
  demand(mapping?.length === 1 && mapping[0]!.hostIp === "127.0.0.1", "PORT_NOT_OBSERVED");
  return mapping[0]!.hostPort;
}

/** 独立只读收集：先核对容器/卷的归属和PID签名，才允许跨CLI恢复UNKNOWN动作。 */
async function reconciliation(ledger: LabLedger, id: string, directory: string, receiptDigest: string) {
  const env = ledger.snapshot().environments[id]!;
  const host = await DockerHost.connect({ configDirectory: join(directory, `docker-reconcile-${randomUUID()}`), owner: { environmentId: id, epoch: env.lease_epoch } });
  const owned = await host.listOwned(); const observations: unknown[] = [owned];
  for (const resource of env.resources) {
    if (resource.kind === "volume") {
      const volume = await host.inspectVolume(resource.provider_id);
      demand(volume === null || (volume.labels["io.egap.environment"] === id && volume.labels["io.egap.epoch"] === String(env.lease_epoch)), "OWNERSHIP_MISMATCH");
      observations.push({ resource_id: resource.resource_id, volume });
    } else if (resource.kind === "process") {
      const match = /^pid:([0-9]+):signature:([a-f0-9]{64})$/.exec(resource.provider_id); demand(match, "INVALID_PROCESS_RECEIPT");
      let signature = "";
      try { signature = execFileSync("ps", ["-p", match[1]!, "-o", "lstart=", "-o", "command="], { encoding: "utf8", timeout: 3000, stdio: ["ignore", "pipe", "ignore"] }).trim(); } catch { /* 已退出，不能借旧PID操作另一个进程。 */ }
      const marker = " -m genai_support.runtime ";
      const python = join(root, "services/genai-support-sut/.venv/bin/python"); const resolved = realpathSync(python);
      const pythonAliases = [python, resolved, resolved.replace(/\/bin\/python[^/]*$/, "/Resources/Python.app/Contents/MacOS/Python")];
      if (signature.includes(marker) && pythonAliases.includes(signature.split(marker)[0]!.slice(24).trim())) signature = signature.slice(0, 24) + marker + signature.split(marker)[1]!;
      observations.push({ resource_id: resource.resource_id, original_process_present: createHash("sha256").update(signature).digest("hex") === match[2] });
    }
  }
  const receipt = { original_receipt_digest: receiptDigest, observations, barrier_status: "NO_EXPERIMENT_BARRIERS_IMPLEMENTED" };
  save(join(directory, `reconciliation-${randomUUID()}.json`), receipt);
  return { receipt_digest: labDigest(receipt), observed_resource_ids: env.resources.map(r => r.resource_id), barrier_checked: true as const };
}

async function main(): Promise<void> {
  const command = process.argv[2]; demand(["up", "inspect", "register", "resource", "action", "complete", "down", "source-check"].includes(command ?? ""), "INVALID_COMMAND");
  const input = JSON.parse(readFileSync(0, "utf8")) as Record<string, unknown>;
  mkdirSync(ledgerRoot, { recursive: true, mode: 0o700 }); const ledger = LabLedger.open(ledgerRoot);
  try {
    if (command === "up") {
      demand(!Object.values(ledger.snapshot().environments).some(e => e.status === "PLANNED"), "UNRESOLVED_ENVIRONMENT_REQUIRES_INSPECTION");
      demand(input.mode === "SCRIPTED_CALIBRATION" || input.mode === "LIVE_MODEL", "INVALID_MODE");
      const files = sourceManifest(); const digest = labDigest(files);
      const sourceCommit = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
      const identity = ledger.createExperiment({ actor: "local-support-provisioner", authorization_scope: "User-authorized build and run of synthetic customer support SUT with local model, durable PostgreSQL/Redis, recovery and precise cleanup; calibration-only, no customer messages or refunds." });
      const build = `support-local-${digest.slice(0, 20)}`;
      ledger.bindSource(identity.experiment_id, { source_commit: sourceCommit, build_revision: build,
        business_revision: "synthetic-v1-local-runtime", upstream_lock_sha256: digest });
      const config = { mode: input.mode, model: input.model ?? null, images, source_manifest_sha256: digest, scope: "local-calibration" };
      const env = ledger.reserveEnvironment(identity.experiment_id, { environment_config_digest: labDigest(config), source_commit: sourceCommit,
        build_revision: build, expires_at: new Date(Date.now() + 24 * 3600_000).toISOString() });
      const action = ledger.beginAction(env.environment_instance_id, env.lease_epoch, "PROVISION");
      const directory = join(ledgerRoot, env.environment_instance_id); mkdirSync(directory, { mode: 0o700 });
      save(join(directory, "source-manifest.json"), { base_commit: sourceCommit, qualification: "WORKING_TREE_CONTENT_SNAPSHOT", files, sha256: digest });
      const metadata: Record<string, unknown> = { identity, environment: env, action_id: action.action_id, directory: relative(root, directory), config, resources: {} };
      save(join(directory, "environment.json"), metadata);
      // 先给出可恢复身份；后续错误不能通过再次up隐去半成品。
      process.stderr.write(JSON.stringify({ environment_instance_id: env.environment_instance_id, directory: relative(root, directory) }) + "\n");
      const audit: DockerAudit[] = [];
      const host = await DockerHost.connect({ configDirectory: join(directory, "docker-config"), owner: { environmentId: env.environment_instance_id, epoch: env.lease_epoch }, audit: e => { audit.push(e); save(join(directory, "docker-audit.json"), audit); } });
      const suffix = env.environment_instance_id.slice("environment_".length);
      // Docker Desktop internal网络不提供宿主publish端口；本轮为本机进程校准拓扑，
      // 使用专属bridge+127.0.0.1发布+数据库口令。不能声称已有候选网络隔离资格。
      const network = await host.createNetwork({ name: `egap-support-${suffix}`, internal: false });
      ledger.recordResource(env.environment_instance_id, env.lease_epoch, { kind: "network", provider_id: network.id });
      const resources = metadata.resources as Record<string, unknown>; resources.network = network.id; save(join(directory, "environment.json"), metadata);
      const pgVolume = await host.createVolume(`egap-pg-${suffix}`);
      ledger.recordResource(env.environment_instance_id, env.lease_epoch, { kind: "volume", provider_id: pgVolume.name });
      const redisVolume = await host.createVolume(`egap-redis-${suffix}`);
      ledger.recordResource(env.environment_instance_id, env.lease_epoch, { kind: "volume", provider_id: redisVolume.name });
      resources.volumes = [pgVolume.name, redisVolume.name]; save(join(directory, "environment.json"), metadata);
      const secrets = { postgres_password: randomUUID().replaceAll("-", ""), redis_password: randomUUID().replaceAll("-", ""),
        sut_password: randomUUID().replaceAll("-", ""), gateway_password: randomUUID().replaceAll("-", ""), reader_password: randomUUID().replaceAll("-", ""),
        api_token_a: randomUUID().replaceAll("-", ""), api_token_b: randomUUID().replaceAll("-", ""), gateway_token: randomUUID().replaceAll("-", "") };
      save(join(directory, "secrets.json"), secrets);
      const postgres = await host.createContainer({ name: `egap-support-pg-${suffix}`, image: images.postgres, networkId: network.id, user: "70:70",
        readOnly: true, ports: [5432], env: { POSTGRES_USER: "postgres", POSTGRES_PASSWORD: secrets.postgres_password, POSTGRES_DB: "postgres" },
        volumes: [{ name: pgVolume.name, destination: "/var/lib/postgresql/data" }], tmpfs: ["/tmp:rw,nosuid,nodev,mode=1777", "/run:rw,nosuid,nodev,mode=1777"], memory: "768m" });
      ledger.recordResource(env.environment_instance_id, env.lease_epoch, { kind: "container", provider_id: postgres.id }); resources.postgres = postgres.id; save(join(directory, "environment.json"), metadata);
      await host.startContainer(postgres.id);
      const redis = await host.createContainer({ name: `egap-support-redis-${suffix}`, image: images.redis, networkId: network.id, user: "999:999",
        readOnly: true, ports: [6379], command: ["redis-server", "--appendonly", "yes", "--appendfsync", "always", "--requirepass", secrets.redis_password],
        volumes: [{ name: redisVolume.name, destination: "/data" }], tmpfs: ["/tmp:rw,nosuid,nodev,mode=1777"], memory: "256m" });
      ledger.recordResource(env.environment_instance_id, env.lease_epoch, { kind: "container", provider_id: redis.id }); resources.redis = redis.id; save(join(directory, "environment.json"), metadata);
      await host.startContainer(redis.id);
      const pg = await host.inspectContainer(postgres.id); const rd = await host.inspectContainer(redis.id); demand(pg && rd, "RESOURCE_UNCONFIRMED");
      metadata.ports = { postgres: observedPort(pg, 5432), redis: observedPort(rd, 6379) };
      metadata.image_ids = { postgres: pg.imageId.replace("sha256:", ""), redis: rd.imageId.replace("sha256:", "") };
      save(join(directory, "environment.json"), metadata); process.stdout.write(JSON.stringify(metadata) + "\n"); return;
    }
    const id = input.environment_instance_id; demand(typeof id === "string" && /^environment_[0-9a-f-]{36}$/.test(id), "INVALID_ENVIRONMENT");
    const env = ledger.snapshot().environments[id]; demand(env && (env.status !== "RETIRED" || command === "down"), "ENVIRONMENT_UNAVAILABLE");
    const directory = join(ledgerRoot, id);
    if (command === "source-check") {
      const source = JSON.parse(readFileSync(join(directory, "source-manifest.json"), "utf8")) as {sha256: string};
      process.stdout.write(JSON.stringify({ unchanged: source.sha256 === labDigest(sourceManifest()) }) + "\n"); return;
    }
    if (command === "resource") {
      demand(input.kind === "process" && typeof input.provider_id === "string", "INVALID_RESOURCE");
      ledger.recordResource(id, env.lease_epoch, { kind: "process", provider_id: input.provider_id }); process.stdout.write('{"status":"RECORDED"}\n'); return;
    }
    if (command === "register") {
      const metadata = JSON.parse(readFileSync(join(directory, "environment.json"), "utf8")) as Record<string, unknown>;
      const manifest: EnvironmentInstance = { environment_instance_id: id, authority_mode: "file-lab", evidence_scope: "calibration-only",
        environment_config_digest: env.environment_config_digest, environment_profile_digest: null, source_commit: env.source_commit,
        build_revision: env.build_revision, fixture_reset_id: env.fixture_reset_id, lease_epoch: env.lease_epoch,
        endpoints: input.endpoints as Record<string, string>, image_digests: metadata.image_ids as Record<string, string>,
        observed_topology_ref: `${relative(root, directory)}/runtime.json`, resource_manifest_ref: `${relative(root, directory)}/environment.json`,
        schema_mapping_ref: null, expires_at: env.expires_at };
      if (env.manifest === null) ledger.registerEnvironment(manifest);
      else demand(labDigest(env.manifest) === labDigest(manifest), "RESTART_MANIFEST_MISMATCH");
      const action = ledger.snapshot().actions[String(metadata.action_id)]!;
      if (action.status === "UNKNOWN") ledger.reconcileAction(action.action_id, env.lease_epoch, "SUCCEEDED", await reconciliation(ledger, id, directory, labDigest(manifest)));
      else if (action.status === "PENDING") ledger.completeAction(action.action_id, env.lease_epoch, "SUCCEEDED", labDigest(manifest));
      else demand(action.status === "SUCCEEDED", "PROVISION_FAILED_REQUIRES_NEW_ENVIRONMENT");
      save(join(directory, "manifest.json"), manifest); process.stdout.write(JSON.stringify(manifest) + "\n"); return;
    }
    if (command === "action") {
      demand(input.kind === "SMOKE" || input.kind === "INSPECT", "INVALID_ACTION");
      process.stdout.write(JSON.stringify(ledger.beginAction(id, env.lease_epoch, input.kind)) + "\n"); return;
    }
    if (command === "complete") {
      demand(typeof input.action_id === "string" && (input.outcome === "SUCCEEDED" || input.outcome === "FAILED") && typeof input.receipt_digest === "string", "INVALID_RECEIPT");
      const action = ledger.snapshot().actions[input.action_id]; demand(action?.environment_instance_id === id, "ACTION_IDENTITY_MISMATCH");
      if (action.status === "UNKNOWN") ledger.reconcileAction(input.action_id, env.lease_epoch, input.outcome, await reconciliation(ledger, id, directory, input.receipt_digest));
      else ledger.completeAction(input.action_id, env.lease_epoch, input.outcome, input.receipt_digest);
      process.stdout.write('{"status":"RECORDED"}\n'); return;
    }
    const host = await DockerHost.connect({ configDirectory: join(directory, `docker-inspect-${randomUUID()}`), owner: { environmentId: id, epoch: env.lease_epoch } });
    const owned = await host.listOwned();
    if (command === "inspect") { process.stdout.write(JSON.stringify({ environment: env, ...owned }) + "\n"); return; }
    // 本机进程先由Python宿主按PID+启动身份停止。没有回执则不移除数据库。
    demand(input.processes_stopped === true, "PROCESS_RECONCILIATION_REQUIRED");
    if (env.status === "RETIRED") {
      demand(owned.containers.length === 0 && owned.networks.length === 0, "RETIRED_ENVIRONMENT_RESIDUAL_RESOURCES");
      for (const prefix of ["pg", "redis"]) demand(await host.inspectVolume(`egap-${prefix}-${id.slice("environment_".length)}`) === null, "RETIRED_ENVIRONMENT_RESIDUAL_RESOURCES");
      process.stdout.write('{"status":"DESTROYED","already_retired":true}\n'); return;
    }
    for (const [kind, providers] of [["container", owned.containers.map(c => c.id)], ["network", owned.networks.map(n => n.id)]] as const) {
      for (const providerId of providers) if (!ledger.snapshot().environments[id]!.resources.some(r => r.kind === kind && r.provider_id === providerId)) {
        ledger.recordResource(id, env.lease_epoch, { kind, provider_id: providerId });
      }
    }
    // volume create回执可能丢失：仅探测本环境预先确定的两个卷名并核对owner。
    for (const prefix of ["pg", "redis"]) {
      const name = `egap-${prefix}-${id.slice("environment_".length)}`; const volume = await host.inspectVolume(name);
      if (volume && !ledger.snapshot().environments[id]!.resources.some(r => r.kind === "volume" && r.provider_id === name)) {
        demand(volume.labels["io.egap.environment"] === id && volume.labels["io.egap.epoch"] === String(env.lease_epoch), "OWNERSHIP_MISMATCH");
        ledger.recordResource(id, env.lease_epoch, { kind: "volume", provider_id: name });
      }
    }
    const pending = Object.values(ledger.snapshot().actions).filter(a => a.environment_instance_id === id && ["PENDING", "UNKNOWN"].includes(a.status));
    for (const action of pending) ledger.reconcileAction(action.action_id, env.lease_epoch, "FAILED", await reconciliation(ledger, id, directory, labDigest(owned)));
    const action = ledger.beginAction(id, env.lease_epoch, "DESTROY");
    for (const container of owned.containers) { if (container.state === "running") await host.stopContainer(container.id); await host.removeContainer(container.id); }
    // 仅删除账本中确切记录的持久卷，不做名称通配、全局prune或清理其他实验。
    for (const resource of ledger.snapshot().environments[id]!.resources.filter(r => r.kind === "volume")) if (await host.inspectVolume(resource.provider_id)) await host.removeVolume(resource.provider_id);
    for (const network of owned.networks) await host.removeNetwork(network.id);
    ledger.completeAction(action.action_id, env.lease_epoch, "SUCCEEDED", labDigest({ containers: owned.containers.map(c => c.id), processes_stopped: true }));
    process.stdout.write('{"status":"DESTROYED"}\n');
  } finally { ledger.close(); }
}
main().catch((error: unknown) => { const message = error instanceof Error ? error.message : ""; const code = /^[A-Z][A-Z0-9_]{2,100}$/.test(message) ? message : "SUPPORT_ENVIRONMENT_FAILED";
  process.stderr.write(JSON.stringify({ status: "FAILED", code }) + "\n"); process.exitCode = 1; });
