import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { DOCKER_EPOCH_LABEL, DOCKER_OWNER_LABEL, DockerHost, DockerHostError } from "./docker-host.js";
import type { DockerAudit, DockerCommandResult, DockerContainerSpec, DockerTransport } from "./docker-host.js";

const owner = { environmentId: "environment_11111111-1111-4111-8111-111111111111", epoch: 3 };
const networkId = "a".repeat(64), containerId = "b".repeat(64), imageId = `sha256:${"c".repeat(64)}`;
const image = `docker.io/library/fixture@sha256:${"d".repeat(64)}`;
const ownLabels = () => ({ [DOCKER_OWNER_LABEL]: owner.environmentId, [DOCKER_EPOCH_LABEL]: String(owner.epoch) });
function success(stdout = ""): DockerCommandResult { return { stdout, stderr: "", exitCode: 0 }; }
function failure(stderr: string, interrupted = false): DockerCommandResult { return { stdout: "", stderr, exitCode: interrupted ? null : 1, interrupted }; }
function isCode(code: string, outcome?: string) { return (error: unknown) => error instanceof DockerHostError && error.code === code && (outcome === undefined || error.outcome === outcome); }
function directory(t: { after(fn: () => void): void }): string { const p = mkdtempSync(join(tmpdir(), "egap-docker-host-test-")); t.after(() => rmSync(p, { recursive: true, force: true })); return p; }

/** 模拟 daemon，而非 shell；每项测试可在指定边界注入响应丢失/资源篡改。 */
class FakeDocker {
  readonly calls: string[][] = [];
  readonly limits: { timeoutMs: number; maxOutputBytes: number }[] = [];
  override?: (args: readonly string[]) => DockerCommandResult | undefined;
  network = { id: networkId, name: "test-network", internal: true, labels: ownLabels(), containers: {} };
  container = {
    id: containerId, name: "/test-service", imageId, state: "created", networkMode: networkId,
    networks: { "test-network": { NetworkID: "", IPAddress: "" } },
    ports: {} as Record<string, unknown>, configuredPorts: { "8080/tcp": [{ HostIp: "127.0.0.1", HostPort: "" }] },
    mounts: [] as Record<string, string>[], labels: ownLabels(),
  };
  transport: DockerTransport = async (argv, options) => {
    this.calls.push([...argv]); this.limits.push(options); const args = argv.slice(4);
    const overridden = this.override?.(args); if (overridden !== undefined) return overridden;
    if (args[0] === "info") return success(JSON.stringify({ os: "linux", architecture: "aarch64" }));
    if (args[0] === "network" && args[1] === "create") return success(networkId + "\n");
    if (args[0] === "network" && args[1] === "inspect") return success(JSON.stringify(this.network));
    if (args[0] === "container" && args[1] === "create") return success(containerId + "\n");
    if (args[0] === "container" && args[1] === "inspect") return success(JSON.stringify(this.container));
    if (args[0] === "image" && args[1] === "inspect") return success(JSON.stringify({ id: imageId, os: "linux", architecture: "arm64", repoDigests: [image] }));
    if (args[1] === "ls") return success((args[0] === "container" ? containerId : networkId) + "\n");
    return success();
  };
  commands(): string[][] { return this.calls.map(argv => argv.slice(4)); }
}

async function setup(t: { after(fn: () => void): void }) {
  const root = directory(t), configDirectory = join(root, "docker-config"), fake = new FakeDocker(), audits: DockerAudit[] = [];
  const host = await DockerHost.connect({ configDirectory, owner, transport: fake.transport, environment: {}, audit: event => audits.push(event) });
  return { root, configDirectory, fake, audits, host };
}

test("只连接固定本机 socket，使用任务空配置，命令设有限时和输出边界", async t => {
  const { host, fake, configDirectory } = await setup(t);
  assert.equal(readFileSync(join(configDirectory, "config.json"), "utf8"), "{}\n");
  assert.deepEqual(fake.calls[0]?.slice(0, 4), ["--config", configDirectory, "--host", `unix://${join(homedir(), ".docker", "run", "docker.sock")}`]);
  assert.equal(host.owner.epoch, 3); assert.ok(fake.limits.every(x => x.timeoutMs > 0 && x.timeoutMs <= 900_000 && x.maxOutputBytes === 4 * 1024 * 1024));
  await assert.rejects(DockerHost.connect({ configDirectory, owner, transport: fake.transport, environment: {} }), isCode("CONFIG_NOT_EMPTY"));
});

test("拒绝陌生 context、远端 daemon 和 TLS 覆盖；拒绝前不会执行命令", async t => {
  const root = directory(t), fake = new FakeDocker();
  for (const [environment, code] of [
    [{ DOCKER_HOST: "tcp://remote:2375" }, "UNTRUSTED_DOCKER_HOST"],
    [{ DOCKER_CONTEXT: "production" }, "UNTRUSTED_DOCKER_CONTEXT"],
    [{ DOCKER_TLS_VERIFY: "1" }, "UNTRUSTED_DOCKER_TLS"],
  ] as const) await assert.rejects(DockerHost.connect({ configDirectory: join(root, code), owner, transport: fake.transport, environment }), isCode(code));
  assert.equal(fake.calls.length, 0);
});

test("创建容器使用不可变镜像、专属网络、localhost动态端口，审计脱敏全部env和应用参数", async t => {
  const { host, fake, audits } = await setup(t);
  const network = await host.createNetwork({ name: "test-network" }); assert.equal(network.id, networkId);
  const created = await host.createContainer({ name: "test-service", image, networkId, networkAlias: "service", user: "999:999", readOnly: false,
    env: { POSTGRES_PASSWORD: "very-secret-password", NORMAL_SETTING: "also-private" }, command: ["--token=command-secret"],
    ports: [8080], tmpfs: ["/var/lib/postgresql:rw,nosuid,nodev,size=512m,mode=1777"], memory: "512m", cpus: 0.5, pidsLimit: 128 });
  assert.equal(created.id, containerId); assert.equal(created.networks["test-network"]?.id, ""); // created ≠ 已连接网络。
  const command = fake.commands().find(a => a[0] === "container" && a[1] === "create")!;
  assert.ok(command.includes("127.0.0.1::8080/tcp")); assert.ok(command.includes("ALL")); assert.ok(command.includes("no-new-privileges:true"));
  assert.ok(!command.includes("--read-only")); assert.ok(!command.some(a => ["--mount", "--volume", "--privileged"].includes(a)));
  assert.ok(fake.commands().find(a => a[0] === "network" && a[1] === "create")?.includes("--internal"));
  const auditText = JSON.stringify(audits); for (const secret of ["very-secret-password", "also-private", "command-secret"]) assert.ok(!auditText.includes(secret));
  assert.match(auditText, /POSTGRES_PASSWORD=<redacted>/);
});

test("创建回执丢失为UNKNOWN，不自动重发；原始stderr不泄漏", async t => {
  const { host, fake, audits } = await setup(t);
  fake.override = args => args[0] === "container" && args[1] === "create" ? failure("password=super-secret", true) : undefined;
  await assert.rejects(host.createContainer({ name: "test-service", image, networkId }), error => {
    assert.ok(isCode("COMMAND_INTERRUPTED", "UNKNOWN")(error)); assert.ok(!String(error).includes("super-secret")); return true;
  });
  assert.equal(fake.commands().filter(a => a[0] === "container" && a[1] === "create").length, 1);
  assert.ok(!JSON.stringify(audits).includes("super-secret"));
});

test("create已返回ID后inspect失败仍为UNKNOWN，不能被当作未派发", async t => {
  const { host, fake } = await setup(t);
  fake.override = args => args[0] === "container" && args[1] === "inspect" ? failure("daemon response lost", true) : undefined;
  await assert.rejects(host.createContainer({ name: "test-service", image, networkId }), isCode("COMMAND_INTERRUPTED", "UNKNOWN"));
  assert.equal(fake.commands().filter(a => a[1] === "create").length, 1);
});

test("外部owner、过期epoch、外部网络阻止mutation；只能删除精确ID", async t => {
  const { host, fake } = await setup(t);
  fake.container.labels[DOCKER_EPOCH_LABEL] = "2";
  await assert.rejects(host.stopContainer(containerId), isCode("OWNERSHIP_MISMATCH"));
  fake.container.labels = ownLabels(); fake.network.labels[DOCKER_OWNER_LABEL] = "environment_other";
  await assert.rejects(host.startContainer(containerId), isCode("OWNERSHIP_MISMATCH"));
  await assert.rejects(host.removeNetwork("test-network"), isCode("EXACT_RESOURCE_ID_REQUIRED"));
  assert.ok(!fake.commands().some(a => ["stop", "start", "rm"].includes(a[1] ?? "")));
});

test("检测到host/匿名卷或非loopback配置时拒绝启动", async t => {
  const { host, fake } = await setup(t);
  fake.container.mounts = [{ Type: "volume", Name: "anonymous", Destination: "/var/lib/data" }];
  await assert.rejects(host.startContainer(containerId), isCode("HOST_OR_ANONYMOUS_VOLUME_FORBIDDEN"));
  fake.container.mounts = []; fake.container.configuredPorts["8080/tcp"][0]!.HostIp = "0.0.0.0";
  await assert.rejects(host.startContainer(containerId), isCode("NON_LOOPBACK_PORT"));
  assert.ok(!fake.commands().some(a => a[1] === "start"));
});

test("inspect只返回安全投影；运行容器必须有真实attachment和loopback实际端口", async t => {
  const { host, fake } = await setup(t);
  fake.container.state = "running"; fake.container.networks["test-network"].NetworkID = networkId;
  fake.container.ports = { "8080/tcp": [{ HostIp: "127.0.0.1", HostPort: "32781" }] };
  const inspected = await host.inspectContainer(containerId);
  assert.equal(inspected?.ports["8080/tcp"]?.[0]?.hostPort, 32781); assert.equal(inspected?.networkMode, networkId);
  assert.ok(!JSON.stringify(inspected).includes("Env"));
  const format = fake.commands().find(a => a[1] === "inspect" && a[0] === "container")?.[3] ?? "";
  assert.ok(!format.includes(".Config.Env")); assert.ok(!format.includes("{{json .}}"));
  fake.container.networks["test-network"].NetworkID = "";
  await assert.rejects(host.stopContainer(containerId), isCode("MALFORMED_NETWORK_ATTACHMENT"));
});

test("删除只使用精确ID；拒绝删除仍运行容器及非空network", async t => {
  const { host, fake } = await setup(t);
  fake.container.state = "running"; fake.container.networks["test-network"].NetworkID = networkId;
  await assert.rejects(host.removeContainer(containerId), isCode("CONTAINER_MUST_BE_STOPPED"));
  fake.container.state = "exited"; await host.removeContainer(containerId);
  fake.network.containers = { [containerId]: {} };
  await assert.rejects(host.removeNetwork(networkId), isCode("NETWORK_NOT_EMPTY"));
  fake.network.containers = {}; await host.removeNetwork(networkId);
  const removals = fake.commands().filter(a => a[1] === "rm");
  assert.deepEqual(removals, [["container", "rm", containerId], ["network", "rm", networkId]]);
  assert.ok(!fake.commands().flat().includes("prune"));
});

test("仅精确daemon不存在回执返回null；helper故障不能当成已清理", async t => {
  const { host, fake } = await setup(t);
  fake.override = args => args[1] === "inspect" ? failure(`Error response from daemon: No such container: ${containerId}`) : undefined;
  assert.equal(await host.inspectContainer(containerId), null);
  fake.override = args => args[1] === "inspect" ? failure("credential helper not found") : undefined;
  await assert.rejects(host.inspectContainer(containerId), isCode("COMMAND_FAILED"));
  fake.override = args => args[1] === "inspect" ? failure(`Error response from daemon: network ${networkId} not found`) : undefined;
  assert.equal(await host.inspectNetwork(networkId), null);
});

test("丢失回执后仅按本环境和epoch发现资源，返回实际ID以供账本对账", async t => {
  const { host, fake } = await setup(t); const discovered = await host.listOwned();
  assert.equal(discovered.containers[0]?.id, containerId); assert.equal(discovered.networks[0]?.id, networkId);
  for (const args of fake.commands().filter(a => a[1] === "ls")) {
    assert.ok(args.includes(`label=${DOCKER_OWNER_LABEL}=${owner.environmentId}`)); assert.ok(args.includes(`label=${DOCKER_EPOCH_LABEL}=3`)); assert.ok(args.includes("--no-trunc"));
  }
  assert.equal((await host.inspectByName("test-service"))?.id, containerId);
});

test("镜像只按digest拉取，读回OS/架构/RepoDigests", async t => {
  const { host, fake } = await setup(t);
  await assert.rejects(host.pullImage("postgres:latest"), isCode("IMMUTABLE_IMAGE_REQUIRED"));
  const result = await host.pullImage(image); assert.equal(result.architecture, "arm64"); assert.equal(result.repoDigests[0], image);
  assert.deepEqual(fake.commands().find(a => a[1] === "pull"), ["image", "pull", "--quiet", image]);
});

test("JAR只复制已校验字节的私有快照到停止容器的固定路径", async t => {
  const { host, root, fake, configDirectory } = await setup(t); const source = join(root, "service.jar"), bytes = Buffer.from("fixture-jar"); writeFileSync(source, bytes);
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  fake.override = args => {
    if (args[1] !== "cp") return undefined;
    assert.notEqual(args[2], source); assert.deepEqual(readFileSync(args[2]!), bytes); assert.equal(args[3], `${containerId}:/opt/service.jar`);
    assert.equal(statSync(args[2]!).mode & 0o777, 0o444); assert.equal(statSync(configDirectory).mode & 0o077, 0);
    writeFileSync(source, "concurrent-source-replacement"); return success();
  };
  await host.copyInto(containerId, { source, sha256, destination: "/opt/service.jar" });
  assert.deepEqual(readdirSync(configDirectory), ["config.json"]);
  await assert.rejects(host.copyInto(containerId, { source, sha256, destination: "/opt/service.jar" }), isCode("COPY_SOURCE_DIGEST_MISMATCH"));
});

test("exec只允许可信数据库工具，命令参数和stdout原文不进入审计", async t => {
  const { host, fake, audits } = await setup(t); fake.container.state = "running"; fake.container.networks["test-network"].NetworkID = networkId;
  await assert.rejects(host.exec(containerId, ["sh", "-c", "anything"]), isCode("EXEC_PROGRAM_NOT_ALLOWED"));
  fake.override = args => args[1] === "exec" ? success("returned-secret-value") : undefined;
  const result = await host.exec(containerId, ["psql", "-c", "select 'query-secret'"]);
  assert.equal(result.stdout, "returned-secret-value"); assert.ok(!JSON.stringify(audits).includes("query-secret")); assert.ok(!JSON.stringify(audits).includes("returned-secret-value"));
});

/** 只扩展本测试的volume响应，避免通用FakeDocker假装所有volume默认存在。 */
function volumeReceipt(name = "egap-support-postgres", overrides: Record<string, unknown> = {}): DockerCommandResult {
  return success(JSON.stringify({ name, driver: "local", options: null, labels: ownLabels(), ...overrides }));
}

test("显式命名卷创建后读回local driver与owner，合法持久挂载只传登记名称", async t => {
  const { host, fake } = await setup(t); const name = "egap-support-postgres"; let exists = false;
  fake.override = args => {
    if (args[0] !== "volume") return undefined;
    if (args[1] === "inspect") return exists ? volumeReceipt(name) : failure(`Error response from daemon: get ${name}: no such volume`);
    if (args[1] === "create") { exists = true; return success(name + "\n"); }
    return undefined;
  };
  const volume = await host.createVolume(name);
  assert.equal(volume.name, name); assert.deepEqual(volume.labels, ownLabels());
  fake.container.mounts = [{ Type: "volume", Name: name, Destination: "/var/lib/postgresql/data" }];
  await host.createContainer({ name: "test-service", image, networkId, volumes: [{ name, destination: "/var/lib/postgresql/data" }] });
  await host.startContainer(containerId);
  const creation = fake.commands().find(args => args[0] === "volume" && args[1] === "create")!;
  assert.deepEqual(creation, ["volume", "create", "--driver", "local", "--label", `${DOCKER_OWNER_LABEL}=${owner.environmentId}`,
    "--label", `${DOCKER_EPOCH_LABEL}=${owner.epoch}`, name]);
  const container = fake.commands().find(args => args[0] === "container" && args[1] === "create")!;
  assert.equal(container[container.indexOf("--mount") + 1], `type=volume,source=${name},target=/var/lib/postgresql/data`);
  assert.ok(!container.includes("--volume")); assert.ok(!container.includes("--privileged"));
  assert.ok(fake.commands().some(args => args[0] === "container" && args[1] === "start"));
});

test("跨owner或旧epoch卷不能挂载或删除，已存在卷也不会被create接管", async t => {
  const { host, fake } = await setup(t); const name = "egap-support-postgres";
  for (const labels of [{ ...ownLabels(), [DOCKER_OWNER_LABEL]: "environment_other" },
    { ...ownLabels(), [DOCKER_EPOCH_LABEL]: "2" }]) {
    fake.override = args => args[0] === "volume" && args[1] === "inspect" ? volumeReceipt(name, { labels }) : undefined;
    await assert.rejects(host.createContainer({ name: "test-service", image, networkId,
      volumes: [{ name, destination: "/var/lib/postgresql/data" }] }), isCode("OWNERSHIP_MISMATCH"));
    await assert.rejects(host.removeVolume(name), isCode("OWNERSHIP_MISMATCH"));
    await assert.rejects(host.createVolume(name), isCode("VOLUME_ALREADY_EXISTS"));
  }
  assert.ok(!fake.commands().some(args => ["create", "rm"].includes(args[1] ?? "")));
});

test("拒绝非local卷及local driver的bind/NFS选项，不能借named volume访问宿主", async t => {
  const { host, fake } = await setup(t); const name = "egap-support-postgres";
  for (const override of [{ driver: "nfs" }, { options: { type: "none", o: "bind", device: "/" } },
    { options: { type: "nfs", device: ":/private" } }, { name: "egap-different-volume" }]) {
    fake.override = args => args[0] === "volume" && args[1] === "inspect" ? volumeReceipt(name, override) : undefined;
    await assert.rejects(host.inspectVolume(name), isCode("UNTRUSTED_VOLUME"));
    await assert.rejects(host.createContainer({ name: "test-service", image, networkId,
      volumes: [{ name, destination: "/var/lib/postgresql/data" }] }), isCode("UNTRUSTED_VOLUME"));
  }
  assert.ok(!fake.commands().some(args => args[1] === "create"));
});

test("挂载路径与卷名称有固定边界，重复目标不会下发容器create", async t => {
  const { host, fake } = await setup(t); const name = "egap-support-postgres";
  fake.override = args => args[0] === "volume" && args[1] === "inspect" ? volumeReceipt(name) : undefined;
  for (const volumes of [
    [{ name, destination: "/var/run/docker.sock" }], [{ name, destination: "/etc" }],
    [{ name, destination: "/data" }, { name, destination: "/data" }],
  ]) {
    await assert.rejects(host.createContainer({ name: "test-service", image, networkId, volumes } as unknown as DockerContainerSpec),
      isCode("INVALID_VOLUME_DESTINATION"));
  }
  for (const invalidName of ["/host/path", "anonymous", "egap-safe,volume-opt=device=/", "egap-bad\n"]) {
    await assert.rejects(host.inspectVolume(invalidName), isCode("INVALID_VOLUME_NAME"));
  }
  assert.ok(!fake.commands().some(args => args[0] === "container" && args[1] === "create"));
});

test("容器读回的卷目标或归属异常时拒绝启动，不能只信创建前校验", async t => {
  const { host, fake } = await setup(t); const name = "egap-support-postgres";
  fake.override = args => args[0] === "volume" && args[1] === "inspect" ? volumeReceipt(name) : undefined;
  fake.container.mounts = [{ Type: "volume", Name: name, Destination: "/etc" }];
  await assert.rejects(host.startContainer(containerId), isCode("HOST_OR_ANONYMOUS_VOLUME_FORBIDDEN"));
  fake.container.mounts = [{ Type: "volume", Name: name, Destination: "/data" }];
  fake.override = args => args[0] === "volume" && args[1] === "inspect"
    ? volumeReceipt(name, { labels: { ...ownLabels(), [DOCKER_OWNER_LABEL]: "environment_other" } }) : undefined;
  await assert.rejects(host.startContainer(containerId), isCode("OWNERSHIP_MISMATCH"));
  assert.ok(!fake.commands().some(args => args[1] === "start"));
});

test("卷create中断和create后inspect丢失都保持UNKNOWN，不自动重发", async t => {
  for (const missingAfterCreate of [false, true]) {
    const { host, fake, audits } = await setup(t); const name = "egap-support-postgres"; let created = false;
    fake.override = args => {
      if (args[0] !== "volume") return undefined;
      if (args[1] === "inspect") return created ? failure("private receipt lost", true)
        : failure(`Error response from daemon: No such volume: ${name}`);
      if (args[1] === "create") { created = true; return missingAfterCreate ? success(name) : failure("private receipt lost", true); }
      return undefined;
    };
    await assert.rejects(host.createVolume(name), isCode("COMMAND_INTERRUPTED", "UNKNOWN"));
    assert.equal(fake.commands().filter(args => args[0] === "volume" && args[1] === "create").length, 1);
    assert.ok(!JSON.stringify(audits).includes("private receipt lost"));
  }
});

test("在用卷删除被daemon拒绝或删除回执丢失均UNKNOWN；不强制删除或prune", async t => {
  const { host, fake, audits } = await setup(t); const name = "egap-support-postgres";
  for (const interrupted of [false, true]) {
    fake.override = args => {
      if (args[0] === "volume" && args[1] === "inspect") return volumeReceipt(name);
      if (args[0] === "volume" && args[1] === "rm") return failure("volume is in use by private-container", interrupted);
      return undefined;
    };
    await assert.rejects(host.removeVolume(name), isCode(interrupted ? "COMMAND_INTERRUPTED" : "COMMAND_FAILED", "UNKNOWN"));
  }
  const removals = fake.commands().filter(args => args[0] === "volume" && args[1] === "rm");
  assert.deepEqual(removals, [["volume", "rm", name], ["volume", "rm", name]]);
  assert.ok(!fake.commands().flat().some(arg => ["prune", "--force", "-f"].includes(arg)));
  assert.ok(!JSON.stringify(audits).includes("private-container"));
});

test("卷不存在只接受精确名称回执，不能把外部对象或helper失败当成已清理", async t => {
  const { host, fake } = await setup(t); const name = "egap-support-postgres";
  for (const message of [`Error response from daemon: get ${name}: no such volume`,
    `Error response from daemon: No such volume: ${name}`]) {
    fake.override = args => args[0] === "volume" ? failure(message) : undefined;
    assert.equal(await host.inspectVolume(name), null);
  }
  for (const message of ["credential helper not found", "Error response from daemon: get egap-other: no such volume"]) {
    fake.override = args => args[0] === "volume" ? failure(message) : undefined;
    await assert.rejects(host.inspectVolume(name), isCode("COMMAND_FAILED", "NOT_DISPATCHED"));
  }
});
