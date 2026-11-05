import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { chmod, lstat, mkdir, readFile, readdir, unlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";

export const DOCKER_OWNER_LABEL = "io.egap.environment";
export const DOCKER_EPOCH_LABEL = "io.egap.epoch";
const RESOURCE_ID = /^[0-9a-f]{64}$/;
const IMAGE_REF = /^[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}$/;
const NAME = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/;
const CONTAINER_FORMAT = '{"id":{{json .Id}},"name":{{json .Name}},"imageId":{{json .Image}},"state":{{json .State.Status}},"networkMode":{{json .HostConfig.NetworkMode}},"networks":{{json .NetworkSettings.Networks}},"ports":{{json .NetworkSettings.Ports}},"configuredPorts":{{json .HostConfig.PortBindings}},"mounts":{{json .Mounts}},"labels":{{json .Config.Labels}}}';
const NETWORK_FORMAT = '{"id":{{json .Id}},"name":{{json .Name}},"internal":{{json .Internal}},"labels":{{json .Labels}},"containers":{{json .Containers}}}';
const IMAGE_FORMAT = '{"id":{{json .Id}},"os":{{json .Os}},"architecture":{{json .Architecture}},"repoDigests":{{json .RepoDigests}}}';

export interface DockerOwner { readonly environmentId: string; readonly epoch: number }
export interface DockerContainer {
  readonly id: string; readonly name: string; readonly imageId: string; readonly state: string;
  readonly networkMode: string;
  readonly networks: Readonly<Record<string, { readonly id: string; readonly ipAddress: string }>>;
  readonly ports: Readonly<Record<string, readonly { readonly hostIp: string; readonly hostPort: number }[]>>;
  readonly mounts: readonly { readonly type: string; readonly destination: string; readonly name?: string }[];
  readonly labels: Readonly<Record<string, string>>;
}
export interface DockerNetwork {
  readonly id: string; readonly name: string; readonly internal: boolean;
  readonly labels: Readonly<Record<string, string>>; readonly containerIds: readonly string[];
}
export interface DockerImage { readonly id: string; readonly os: string; readonly architecture: string; readonly repoDigests: readonly string[] }
export interface DockerVolume { readonly name: string; readonly labels: Readonly<Record<string, string>> }
export interface DockerContainerSpec {
  readonly name: string; readonly image: string; readonly networkId: string; readonly networkAlias?: string;
  readonly env?: Readonly<Record<string, string>>; readonly entrypoint?: string; readonly command?: readonly string[];
  readonly ports?: readonly number[]; readonly user?: string; readonly readOnly?: boolean; readonly capDrop?: readonly string[];
  readonly tmpfs?: readonly string[]; readonly memory?: string; readonly cpus?: number; readonly pidsLimit?: number;
  /** 只接受本environment创建的local named volume；不能传宿主路径、匿名卷或driver选项。 */
  readonly volumes?: readonly { readonly name: string; readonly destination: "/var/lib/postgresql/data" | "/data" }[];
}
/** 注入点只供可信 host 编排和离线测试使用，不能由候选代码提供。 */
export type DockerTransport = (argv: readonly string[], options: { timeoutMs: number; maxOutputBytes: number }) => Promise<DockerCommandResult>;
export interface DockerCommandResult { readonly stdout: string; readonly stderr: string; readonly exitCode: number | null; readonly interrupted?: boolean }
export interface DockerAudit {
  readonly argv: readonly string[]; readonly elapsedMs: number; readonly exitCode: number | null;
  readonly outcome: "SUCCEEDED" | "NOT_DISPATCHED" | "UNKNOWN";
  readonly stdout: { readonly bytes: number; readonly sha256: string };
  readonly stderr: { readonly bytes: number; readonly sha256: string };
}
export interface DockerHostOptions {
  readonly configDirectory: string; readonly owner: DockerOwner;
  readonly audit?: (event: DockerAudit) => void; readonly transport?: DockerTransport;
  /** 测试可注入环境；生产默认读取真实进程环境，只检查 Docker 路由覆盖项。 */
  readonly environment?: Readonly<Record<string, string | undefined>>;
}

export class DockerHostError extends Error {
  constructor(readonly code: string, readonly outcome: "NOT_DISPATCHED" | "UNKNOWN" = "NOT_DISPATCHED") {
    // 不拼接 argv、stderr 或 cause：这些值可能包含数据库口令或 daemon 回显。
    super(`Docker host operation failed: ${code}; outcome=${outcome}`); this.name = "DockerHostError";
  }
}

/**
 * provisioner → DockerHost → execFile('docker', argv) → 固定本机 socket。
 * 本类只持有单个 environment/epoch 权限，不接受 shell、host mount、任意 context 或 prune。
 * 文件账本保证本机进程协调；标签校验不是 Docker daemon 提供的原子 fencing。
 */
export class DockerHost {
  readonly owner: DockerOwner;
  readonly socketPath = join(homedir(), ".docker", "run", "docker.sock");
  private readonly transport: DockerTransport;
  private readonly prefix: readonly string[];
  private constructor(private readonly options: DockerHostOptions) {
    this.owner = Object.freeze({ ...options.owner });
    this.prefix = ["--config", options.configDirectory, "--host", `unix://${this.socketPath}`];
    this.transport = options.transport ?? nativeTransport;
  }

  /** 首次连接建立专属空配置；不读取、复制或继承用户 Docker credentials/context。 */
  static async connect(options: DockerHostOptions): Promise<DockerHost> {
    assert(/^environment_[0-9a-f-]{36}$/.test(options.owner.environmentId) && Number.isSafeInteger(options.owner.epoch) && options.owner.epoch > 0, "INVALID_OWNER");
    assert(isAbsolute(options.configDirectory), "CONFIG_MUST_BE_ABSOLUTE");
    const host = new DockerHost(options); const environment = options.environment ?? process.env;
    assert(!environment.DOCKER_HOST || environment.DOCKER_HOST === `unix://${host.socketPath}`, "UNTRUSTED_DOCKER_HOST");
    assert(!environment.DOCKER_CONTEXT || environment.DOCKER_CONTEXT === "desktop-linux", "UNTRUSTED_DOCKER_CONTEXT");
    assert(!environment.DOCKER_TLS_VERIFY && !environment.DOCKER_CERT_PATH, "UNTRUSTED_DOCKER_TLS");
    await mkdir(options.configDirectory, { recursive: true, mode: 0o700 });
    const configStat = await lstat(options.configDirectory);
    assert(!configStat.isSymbolicLink(), "CONFIG_SYMLINK");
    assert((configStat.mode & 0o077) === 0, "PRIVATE_CONFIG_REQUIRED");
    assert((await readdir(options.configDirectory)).length === 0, "CONFIG_NOT_EMPTY");
    await writeFile(join(options.configDirectory, "config.json"), "{}\n", { flag: "wx", mode: 0o600 });
    if (!options.transport) assert((await lstat(host.socketPath)).isSocket(), "LOCAL_SOCKET_REQUIRED");
    const info = parse(await host.run(["info", "--format", '{"os":{{json .OSType}},"architecture":{{json .Architecture}}}']));
    assert(info.os === "linux" && typeof info.architecture === "string", "LINUX_DAEMON_REQUIRED");
    return host;
  }

  async inspectImage(reference: string): Promise<DockerImage | null> {
    assert(IMAGE_REF.test(reference) || /^sha256:[0-9a-f]{64}$/.test(reference), "IMMUTABLE_IMAGE_REQUIRED");
    const result = await this.run(["image", "inspect", "--format", IMAGE_FORMAT, reference], { missingAllowed: true });
    if (result === null) return null;
    const item = parse(result);
    assert(/^sha256:[0-9a-f]{64}$/.test(str(item.id)), "MALFORMED_IMAGE");
    return { id: str(item.id), os: str(item.os), architecture: str(item.architecture), repoDigests: stringArray(item.repoDigests ?? []) };
  }

  async pullImage(reference: string): Promise<DockerImage> {
    assert(IMAGE_REF.test(reference), "IMMUTABLE_IMAGE_REQUIRED");
    await this.run(["image", "pull", "--quiet", reference], { mutation: true, timeoutMs: 900_000 });
    return confirmReceipt(async () => {
      const image = await this.inspectImage(reference);
      assert(image !== null && image.os === "linux", "PULLED_IMAGE_UNCONFIRMED", "UNKNOWN");
      assert(image.repoDigests.some(d => d.endsWith(reference.slice(reference.indexOf("@")))), "PULLED_DIGEST_UNCONFIRMED", "UNKNOWN");
      return image;
    });
  }

  async createNetwork(spec: { readonly name: string; readonly internal?: boolean }): Promise<DockerNetwork> {
    assert(NAME.test(spec.name), "INVALID_NAME");
    const args = ["network", "create", "--driver", "bridge", ...this.labelArgs()];
    if (spec.internal !== false) args.push("--internal");
    const id = (await this.run([...args, spec.name], { mutation: true }))!.trim();
    assert(RESOURCE_ID.test(id), "NETWORK_RECEIPT_UNCONFIRMED", "UNKNOWN");
    return confirmReceipt(async () => { const network = await this.inspectNetwork(id); assert(network !== null, "NETWORK_RECEIPT_UNCONFIRMED", "UNKNOWN"); this.owned(network.labels); return network; });
  }

  async inspectNetwork(id: string): Promise<DockerNetwork | null> {
    resourceId(id); const output = await this.run(["network", "inspect", "--format", NETWORK_FORMAT, id], { missingAllowed: true });
    if (output === null) return null; const raw = parse(output);
    assert(raw.id === id && typeof raw.internal === "boolean", "MALFORMED_NETWORK");
    const containerIds = Object.keys(record(raw.containers ?? {})); containerIds.forEach(resourceId);
    return { id, name: str(raw.name), internal: raw.internal, labels: labels(raw.labels), containerIds };
  }

  /** provisioner → 创建显式命名持久卷 → 读回owner；进程/容器重启不丢数据库内容。 */
  async createVolume(name: string): Promise<DockerVolume> {
    assert(/^egap-[a-z0-9-]{1,100}$/.test(name), "INVALID_VOLUME_NAME");
    assert(await this.inspectVolume(name) === null, "VOLUME_ALREADY_EXISTS");
    await this.run(["volume", "create", "--driver", "local", ...this.labelArgs(), name], { mutation: true });
    return confirmReceipt(async () => { const volume = await this.inspectVolume(name); assert(volume, "VOLUME_RECEIPT_UNCONFIRMED", "UNKNOWN"); this.owned(volume.labels); return volume; });
  }

  async inspectVolume(name: string): Promise<DockerVolume | null> {
    assert(/^egap-[a-z0-9-]{1,100}$/.test(name), "INVALID_VOLUME_NAME");
    const raw = await this.run(["volume", "inspect", "--format", '{"name":{{json .Name}},"driver":{{json .Driver}},"options":{{json .Options}},"labels":{{json .Labels}}}', name], { missingAllowed: true });
    if (raw === null) return null;
    const v = parse(raw); assert(v.name === name && v.driver === "local" && Object.keys(record(v.options ?? {})).length === 0, "UNTRUSTED_VOLUME");
    return { name, labels: labels(v.labels) };
  }

  /** 默认清理保留卷；只有显式销毁环境才调用，daemon拒绝删除仍被容器引用的卷。 */
  async removeVolume(name: string): Promise<void> {
    const volume = await this.inspectVolume(name); assert(volume, "VOLUME_ABSENT"); this.owned(volume.labels);
    await this.run(["volume", "rm", name], { mutation: true });
  }

  /** 固定网络和 localhost 动态端口；不提供 mounts/privileged/docker.sock 等逃逸参数入口。 */
  async createContainer(spec: DockerContainerSpec): Promise<DockerContainer> {
    assert(NAME.test(spec.name) && IMAGE_REF.test(spec.image), "INVALID_CONTAINER_SPEC");
    const network = await this.inspectNetwork(spec.networkId); assert(network !== null, "NETWORK_ABSENT"); this.owned(network.labels);
    const args = ["container", "create", "--name", spec.name, "--network", spec.networkId, ...this.labelArgs(), "--security-opt", "no-new-privileges:true"];
    if (spec.networkAlias !== undefined) { assert(NAME.test(spec.networkAlias), "INVALID_NETWORK_ALIAS"); args.push("--network-alias", spec.networkAlias); }
    if (spec.user !== undefined) { assert(/^[0-9]+(?::[0-9]+)?$/.test(spec.user), "NUMERIC_USER_REQUIRED"); args.push("--user", spec.user); }
    if (spec.readOnly ?? true) args.push("--read-only");
    const drops = spec.capDrop ?? ["ALL"]; assert(drops.length === 1 && drops[0] === "ALL", "CAP_DROP_ALL_REQUIRED"); args.push("--cap-drop", "ALL");
    const memory = spec.memory ?? "768m"; assert(/^[1-9][0-9]*(?:[mg])$/.test(memory), "INVALID_MEMORY_LIMIT"); args.push("--memory", memory);
    const cpus = spec.cpus ?? 1; assert(Number.isFinite(cpus) && cpus > 0 && cpus <= 12, "INVALID_CPU_LIMIT"); args.push("--cpus", String(cpus));
    const pids = spec.pidsLimit ?? 256; assert(Number.isSafeInteger(pids) && pids >= 1 && pids <= 4096, "INVALID_PIDS_LIMIT"); args.push("--pids-limit", String(pids));
    for (const value of spec.tmpfs ?? []) {
      // 只覆盖当前可信镜像确实需要的临时目录；不能让调用方覆盖 /proc、/sys 等路径。
      assert(/^\/(?:tmp|run|var\/lib\/postgresql|etc\/kafka\/secrets|mnt\/shared\/config)(?::[a-zA-Z0-9=,]+)?$/.test(value), "INVALID_TMPFS"); args.push("--tmpfs", value);
    }
    const destinations = new Set<string>();
    for (const volume of spec.volumes ?? []) {
      assert(["/var/lib/postgresql/data", "/data"].includes(volume.destination) && !destinations.has(volume.destination), "INVALID_VOLUME_DESTINATION");
      destinations.add(volume.destination); const actual = await this.inspectVolume(volume.name);
      assert(actual !== null, "VOLUME_ABSENT"); this.owned(actual.labels);
      args.push("--mount", `type=volume,source=${volume.name},target=${volume.destination}`);
    }
    for (const port of spec.ports ?? []) { assert(Number.isInteger(port) && port >= 1 && port <= 65535, "INVALID_PORT"); args.push("--publish", `127.0.0.1::${port}/tcp`); }
    for (const [key, value] of Object.entries(spec.env ?? {})) { assert(/^[A-Z][A-Z0-9_]*$/.test(key) && !value.includes("\0"), "INVALID_ENV"); args.push("--env", `${key}=${value}`); }
    if (spec.entrypoint !== undefined) { safeArg(spec.entrypoint); args.push("--entrypoint", spec.entrypoint); }
    (spec.command ?? []).forEach(safeArg);
    const id = (await this.run([...args, spec.image, ...spec.command ?? []], { mutation: true }))!.trim();
    assert(RESOURCE_ID.test(id), "CONTAINER_RECEIPT_UNCONFIRMED", "UNKNOWN");
    return confirmReceipt(async () => { const container = await this.inspectContainer(id); assert(container !== null, "CONTAINER_RECEIPT_UNCONFIRMED", "UNKNOWN"); await this.ownedContainer(container); return container; });
  }

  async inspectContainer(id: string): Promise<DockerContainer | null> {
    resourceId(id); const output = await this.run(["container", "inspect", "--format", CONTAINER_FORMAT, id], { missingAllowed: true });
    if (output === null) return null; const raw = parse(output);
    assert(raw.id === id && /^sha256:[0-9a-f]{64}$/.test(str(raw.imageId)), "MALFORMED_CONTAINER");
    const networkMode = str(raw.networkMode); resourceId(networkMode);
    const networks: Record<string, { id: string; ipAddress: string }> = {};
    for (const [name, value] of Object.entries(record(raw.networks ?? {}))) {
      const network = record(value); const networkId = str(network.NetworkID);
      // 尚未启动的容器可能只有 HostConfig.NetworkMode，不能假装已有实测网络 attachment。
      assert(networkId === "" ? raw.state !== "running" : RESOURCE_ID.test(networkId), "MALFORMED_NETWORK_ATTACHMENT");
      networks[name] = { id: networkId, ipAddress: str(network.IPAddress ?? "") };
    }
    for (const value of Object.values(record(raw.configuredPorts ?? {}))) {
      assert(Array.isArray(value), "MALFORMED_PORTS");
      for (const binding of value) assert(record(binding).HostIp === "127.0.0.1", "NON_LOOPBACK_PORT");
    }
    assert(Array.isArray(raw.mounts), "MALFORMED_MOUNTS");
    const mounts = raw.mounts.map((value: unknown) => { const mount = record(value); return { type: str(mount.Type), destination: str(mount.Destination), ...(typeof mount.Name === "string" ? { name: mount.Name } : {}) }; });
    const ports: Record<string, { hostIp: string; hostPort: number }[]> = {};
    for (const [name, value] of Object.entries(record(raw.ports ?? {}))) {
      assert(/^\d+\/(?:tcp|udp)$/.test(name) && (value === null || Array.isArray(value)), "MALFORMED_PORTS");
      ports[name] = (value ?? []).map((binding: unknown) => { const b = record(binding); const hostPort = Number(b.HostPort); assert(Number.isInteger(hostPort) && hostPort > 0 && hostPort <= 65535, "MALFORMED_PORTS"); return { hostIp: str(b.HostIp), hostPort }; });
    }
    return { id, name: str(raw.name).replace(/^\//, ""), imageId: str(raw.imageId), state: str(raw.state), networkMode, networks, ports, mounts, labels: labels(raw.labels) };
  }

  async startContainer(id: string): Promise<void> { await this.requireContainer(id); await this.run(["container", "start", id], { mutation: true }); }
  async stopContainer(id: string): Promise<void> { await this.requireContainer(id); await this.run(["container", "stop", "--time", "20", id], { mutation: true, timeoutMs: 40_000 }); }
  /** 删除前必须已停止；不使用 rm --force，也不按名字或模糊标签批量删除。 */
  async removeContainer(id: string): Promise<void> { const c = await this.requireContainer(id); assert(c.state !== "running" && c.state !== "restarting" && c.state !== "paused", "CONTAINER_MUST_BE_STOPPED"); await this.run(["container", "rm", id], { mutation: true }); }
  async removeNetwork(id: string): Promise<void> { const n = await this.inspectNetwork(id); assert(n !== null, "NETWORK_ABSENT"); this.owned(n.labels); assert(n.containerIds.length === 0, "NETWORK_NOT_EMPTY"); await this.run(["network", "rm", id], { mutation: true }); }

  /** 文件在宿主已固定 SHA；复制仅限停止容器的固定 JAR 位置，不提供宿主 bind mount。 */
  async copyInto(id: string, input: { readonly source: string; readonly sha256: string; readonly destination: "/opt/service.jar" }): Promise<void> {
    const c = await this.requireContainer(id); assert(c.state === "created" || c.state === "exited", "COPY_REQUIRES_STOPPED_CONTAINER");
    assert(input.destination === "/opt/service.jar" && isAbsolute(input.source) && /^[0-9a-f]{64}$/.test(input.sha256), "INVALID_COPY_SPEC");
    const stat = await lstat(input.source); assert(stat.isFile() && !stat.isSymbolicLink(), "COPY_REQUIRES_REGULAR_FILE");
    const bytes = await readFile(input.source); assert(hash(bytes) === input.sha256, "COPY_SOURCE_DIGEST_MISMATCH");
    // docker cp 读取的是已校验字节的私有快照，避免 hash 检查后原 artifact 被替换的窗口。
    const staged = join(this.options.configDirectory, `artifact-${randomUUID()}.jar`);
    // config 目录是 0700；文件用 0444 让 docker cp 后的业务 UID 10001 可以读取，不能依赖 cp 的 owner 保留语义。
    await writeFile(staged, bytes, { mode: 0o444, flag: "wx" });
    await chmod(staged, 0o444); // 显式设置，避免调用进程 umask=077 把 group/other read 位屏蔽。
    try { await this.run(["container", "cp", staged, `${id}:${input.destination}`], { mutation: true }); }
    finally { await unlink(staged); }
  }

  /** 只给可信 provisioner 的数据库健康检查/初始化使用；候选代码不能传入 argv。 */
  async exec(id: string, argv: readonly string[], options: { readonly timeoutMs?: number } = {}): Promise<{ stdout: string; stderr: string }> {
    const c = await this.requireContainer(id); assert(c.state === "running", "EXEC_REQUIRES_RUNNING_CONTAINER");
    assert(["pg_isready", "psql", "/usr/bin/pg_isready", "/usr/bin/psql"].includes(argv[0] ?? ""), "EXEC_PROGRAM_NOT_ALLOWED"); argv.forEach(safeArg);
    const result = await this.command(["container", "exec", id, ...argv], { mutation: true, timeoutMs: options.timeoutMs ?? 30_000 });
    return { stdout: result.stdout, stderr: result.stderr };
  }

  /** 日志原文仅返回内存，由上层按本次 secret 集合脱敏后持久化；审计只保存长度/摘要。 */
  async logs(id: string, options: { readonly tail?: number } = {}): Promise<string> {
    await this.requireContainer(id); const tail = options.tail ?? 200;
    assert(Number.isInteger(tail) && tail >= 1 && tail <= 2000, "INVALID_LOG_LIMIT");
    const result = await this.command(["container", "logs", "--tail", String(tail), id]); return result.stdout + result.stderr;
  }

  /** create 回执丢失后的只读查找；只扫描当前 owner+epoch，不能用于跨任务删除。 */
  async listOwned(): Promise<{ containers: DockerContainer[]; networks: DockerNetwork[] }> {
    const filters = ["--filter", `label=${DOCKER_OWNER_LABEL}=${this.owner.environmentId}`, "--filter", `label=${DOCKER_EPOCH_LABEL}=${this.owner.epoch}`];
    const containerIds = ids(await this.run(["container", "ls", "--all", "--no-trunc", ...filters, "--format", "{{.ID}}"]));
    const networkIds = ids(await this.run(["network", "ls", "--no-trunc", ...filters, "--format", "{{.ID}}"]));
    const containers: DockerContainer[] = []; const networks: DockerNetwork[] = [];
    for (const id of containerIds) { const c = await this.inspectContainer(id); if (c) { this.owned(c.labels); containers.push(c); } }
    for (const id of networkIds) { const n = await this.inspectNetwork(id); if (n) { this.owned(n.labels); networks.push(n); } }
    return { containers, networks };
  }

  async inspectByName(name: string): Promise<DockerContainer | null> { assert(NAME.test(name), "INVALID_NAME"); const { containers } = await this.listOwned(); const matches = containers.filter(c => c.name === name); assert(matches.length <= 1, "AMBIGUOUS_CONTAINER_NAME"); return matches[0] ?? null; }
  private labelArgs(): string[] { return ["--label", `${DOCKER_OWNER_LABEL}=${this.owner.environmentId}`, "--label", `${DOCKER_EPOCH_LABEL}=${this.owner.epoch}`]; }
  private owned(labels: Readonly<Record<string, string>>): void { assert(labels[DOCKER_OWNER_LABEL] === this.owner.environmentId && labels[DOCKER_EPOCH_LABEL] === String(this.owner.epoch), "OWNERSHIP_MISMATCH"); }
  private async requireContainer(id: string): Promise<DockerContainer> { const container = await this.inspectContainer(id); assert(container !== null, "CONTAINER_ABSENT"); await this.ownedContainer(container); return container; }
  private async ownedContainer(container: DockerContainer): Promise<void> {
    this.owned(container.labels); const networks = Object.values(container.networks);
    assert(networks.length === 1, "EXCLUSIVE_NETWORK_REQUIRED");
    assert(networks[0]!.id === container.networkMode || (networks[0]!.id === "" && container.state !== "running"), "NETWORK_MODE_MISMATCH");
    const network = await this.inspectNetwork(container.networkMode); assert(network !== null, "NETWORK_ABSENT"); this.owned(network.labels);
    for (const mount of container.mounts) {
      if (mount.type === "tmpfs") continue;
      assert(mount.type === "volume" && mount.name && /^egap-[a-z0-9-]{1,100}$/.test(mount.name) &&
        ["/var/lib/postgresql/data", "/data"].includes(mount.destination), "HOST_OR_ANONYMOUS_VOLUME_FORBIDDEN");
      const volume = await this.inspectVolume(mount.name); assert(volume, "VOLUME_ABSENT"); this.owned(volume.labels);
    }
    for (const bindings of Object.values(container.ports)) for (const port of bindings) assert(port.hostIp === "127.0.0.1", "NON_LOOPBACK_PORT");
  }

  private async run(args: readonly string[], options: CommandOptions = {}): Promise<string | null> {
    const result = await this.command(args, options);
    return result.exitCode === 0 ? result.stdout : null;
  }
  private async command(args: readonly string[], options: CommandOptions = {}): Promise<DockerCommandResult> {
    const timeoutMs = options.timeoutMs ?? 30_000; assert(Number.isInteger(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 900_000, "INVALID_TIMEOUT");
    const argv = [...this.prefix, ...args]; const started = Date.now(); let result: DockerCommandResult;
    try { result = await this.transport(argv, { timeoutMs, maxOutputBytes: 4 * 1024 * 1024 }); }
    catch { result = { stdout: "", stderr: "", exitCode: null, interrupted: true }; }
    const missing = options.missingAllowed && !result.interrupted && result.exitCode !== 0 && isMissingReceipt(result.stderr, args.at(-1)!);
    const outcome = result.exitCode === 0 && !result.interrupted ? "SUCCEEDED" : options.mutation ? "UNKNOWN" : "NOT_DISPATCHED";
    try { this.options.audit?.({ argv: auditArgs(argv), elapsedMs: Date.now() - started, exitCode: result.exitCode, outcome, stdout: summary(result.stdout), stderr: summary(result.stderr) }); }
    catch { throw new DockerHostError("AUDIT_RECEIPT_FAILED", options.mutation ? "UNKNOWN" : "NOT_DISPATCHED"); }
    if (outcome !== "SUCCEEDED" && !missing) throw new DockerHostError(result.interrupted ? "COMMAND_INTERRUPTED" : "COMMAND_FAILED", options.mutation ? "UNKNOWN" : "NOT_DISPATCHED");
    return result;
  }
}

interface CommandOptions { readonly mutation?: boolean; readonly missingAllowed?: boolean; readonly timeoutMs?: number }
function assert(condition: unknown, code: string, outcome: "NOT_DISPATCHED" | "UNKNOWN" = "NOT_DISPATCHED"): asserts condition { if (!condition) throw new DockerHostError(code, outcome); }
function resourceId(id: string): void { assert(RESOURCE_ID.test(id), "EXACT_RESOURCE_ID_REQUIRED"); }
function safeArg(value: string): void { assert(typeof value === "string" && value.length <= 65_536 && !value.includes("\0"), "INVALID_ARGUMENT"); }
function record(value: unknown): Record<string, unknown> { assert(typeof value === "object" && value !== null && !Array.isArray(value), "MALFORMED_INSPECT"); return value as Record<string, unknown>; }
function str(value: unknown): string { assert(typeof value === "string", "MALFORMED_INSPECT"); return value; }
function parse(output: string | null): Record<string, unknown> { try { return record(JSON.parse(output ?? "")); } catch { throw new DockerHostError("MALFORMED_INSPECT"); } }
function stringArray(value: unknown): string[] { assert(Array.isArray(value), "MALFORMED_INSPECT"); return value.map(str); }
function labels(value: unknown): Record<string, string> { return Object.fromEntries(Object.entries(record(value ?? {})).map(([k, v]) => [k, str(v)])); }
function ids(output: string | null): string[] { const values = (output ?? "").trim().split(/\r?\n/).filter(Boolean); values.forEach(resourceId); return values; }
function hash(value: string | Buffer): string { return createHash("sha256").update(value).digest("hex"); }
function summary(value: string): { bytes: number; sha256: string } { return { bytes: Buffer.byteLength(value), sha256: hash(value) }; }
/** 请求已交给 daemon 后，确认读取失败也必须标记 UNKNOWN，不能重新 create。 */
async function confirmReceipt<T>(operation: () => Promise<T>): Promise<T> { try { return await operation(); } catch (error) { throw new DockerHostError(error instanceof DockerHostError ? error.code : "RECEIPT_UNCONFIRMED", "UNKNOWN"); } }
function isMissingReceipt(stderr: string, requested: string): boolean {
  const message = stderr.trim().replace(/^Error(?: response from daemon)?:\s*/i, "");
  // 严格匹配精确对象；例如 credential helper not found 绝不能被误判为资源不存在。
  return ["object", "container", "network", "image", "volume"].some(kind => message === `No such ${kind}: ${requested}`) || message === `network ${requested} not found` || message === `get ${requested}: no such volume`;
}
function auditArgs(argv: readonly string[]): string[] {
  const copy = [...argv];
  for (let i = 0; i < copy.length; i++) if (copy[i] === "--env" && copy[i + 1] !== undefined) { copy[i + 1] = `${copy[i + 1]!.split("=", 1)[0]}=<redacted>`; i++; }
  const execIndex = copy.indexOf("exec"); if (execIndex >= 0) for (let i = execIndex + 3; i < copy.length; i++) copy[i] = "<redacted-argument>";
  // image 后的 command 可以含应用口令，不把普通位置参数当成无秘密配置。
  const imageIndex = copy.findIndex(v => IMAGE_REF.test(v));
  if (copy.includes("create") && imageIndex >= 0) for (let i = imageIndex + 1; i < copy.length; i++) copy[i] = "<redacted-argument>";
  return copy;
}
/** child env 只传系统路径；--config/--host 明确指定，不继承用户 Docker 配置和 TLS 路由。 */
const nativeTransport: DockerTransport = (argv, options) => new Promise(resolve => {
  execFile("docker", [...argv], { encoding: "utf8", timeout: options.timeoutMs, maxBuffer: options.maxOutputBytes, env: { PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin", LANG: "C", LC_ALL: "C" } }, (error, stdout, stderr) => {
    const interrupted = error !== null && (error.killed || typeof error.code !== "number");
    resolve({ stdout, stderr, exitCode: error === null ? 0 : typeof error.code === "number" ? error.code : null, interrupted });
  });
});
