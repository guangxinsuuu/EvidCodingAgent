import { openSync, closeSync, fstatSync, readFileSync, constants } from "node:fs";
import { request } from "node:http";
import { canonical, sha, requireThat, RuntimeError, type ReleaseBundle, type Json } from "./types.js";

export interface SourceBinding {
  ref: string; instance_id: string; tenant_id: string; namespace: "jira" | "rules";
  object_id: string; version: string; publication_id: string; content_sha256: string;
  publisher_id: string; metadata: { authority_scope: string; approval_state: string;
    profile: string; content_type: string; source: { repository_url: string; commit: string; path: string; blob: string } };
}
interface ReaderConfig {
  base_url: string; token: string; principal_id: string; tenant_id: string;
  instance_id: string; source_kind: string;
}
const registry = JSON.parse(readFileSync(new URL("../../../config/source-publications.v1.json", import.meta.url), "utf8")) as { objects: Json[] };

/** 安装侧固定登记决定可读对象；任务、来源正文或模型均不能提供地址和凭据。 */
export function validateSourceBindings(bundle: ReleaseBundle): void {
  const sources = bundle.external_sources;
  const external = bundle.materials.filter(m => m.external_source_ref !== undefined);
  if (sources === undefined) {
    requireThat(!external.length, "SOURCE_BINDING_REQUIRED", 400); return;
  }
  requireThat(bundle.schema_version === 2 && Array.isArray(sources) && sources.length === 2 &&
    new Set(sources.map(s => s.ref)).size === 2, "SOURCE_BINDING_INVALID", 400);
  for (const s of sources) {
    const r = registry.objects.find(r => r.namespace === s.namespace && r.object_id === s.object_id && r.version === s.version);
    requireThat(r && ["jira:EGAPLAB-100", "rules:refund-rules"].includes(s.ref) && s.ref === `${s.namespace}:${s.object_id}` &&
      /^evid-sources-[a-f0-9]{12}$/.test(s.instance_id) && s.tenant_id === "company-a" &&
      /^[a-f0-9]{32}$/.test(s.publication_id) && /^[a-f0-9]{64}$/.test(s.content_sha256) &&
      s.publisher_id === "lab:" + r.publisher && s.metadata?.authority_scope === "SYNTHETIC_LOCAL_CALIBRATION" &&
      s.metadata.approval_state === (s.namespace === "rules" ? "PENDING_USER_REVIEW" : "DRAFT") &&
      s.metadata.profile === r.profile && s.metadata.content_type === r.content_type &&
      s.metadata.source?.repository_url === `https://github.com/guangxinsuuu/${r.repository}.git` &&
      s.metadata.source.path === r.path && /^[a-f0-9]{40}$/.test(s.metadata.source.commit) &&
      /^[a-f0-9]{40}$/.test(s.metadata.source.blob), "SOURCE_REGISTRATION_MISMATCH", 400);
    requireThat(Object.keys(s).sort().join() === ["ref", "instance_id", "tenant_id", "namespace", "object_id", "version",
      "publication_id", "content_sha256", "publisher_id", "metadata"].sort().join(), "SOURCE_BINDING_FIELDS_INVALID", 400);
  }
  requireThat(sources[0]!.instance_id === sources[1]!.instance_id, "SOURCE_INSTANCE_MISMATCH", 400);
  requireThat(bundle.materials.some(m => m.material_ref === "jira:EGAPLAB-100" && m.external_source_ref === "jira:EGAPLAB-100"),
    "JIRA_MATERIAL_REQUIRED", 400);
  for (const m of bundle.materials) {
    if (m.kind === "REGISTERED_RULE_EXCERPT") requireThat(m.external_source_ref === "rules:refund-rules", "LOCAL_RULE_FALLBACK_FORBIDDEN", 400);
    if (m.external_source_ref === undefined) continue;
    const source = sources.find(s => s.ref === m.external_source_ref);
    requireThat(source && m.git_commit === source.metadata.source.commit && m.path === source.metadata.source.path &&
      Number.isSafeInteger(m.source_byte_start) && Number.isSafeInteger(m.source_byte_end) && m.source_byte_start! >= 0 &&
      m.source_byte_end! > m.source_byte_start! && m.source_byte_end! <= 524288, "SOURCE_MATERIAL_BINDING_INVALID", 400);
  }
}

/** 来源读取不缓存授权；受信启动配置 → 每次 HTTP → 身份/摘要/投影核验。
 * 返回的是使用时的授权回执，不是业务正确性证明。网络失败一律停止使用旧快照。
 */
export class SimulatedSourceReader {
  readonly identity: Json;
  private readonly endpoint: URL;
  constructor(private readonly config: ReaderConfig) {
    this.endpoint = new URL(config.base_url);
    requireThat(this.endpoint.protocol === "http:" && this.endpoint.hostname === "127.0.0.1" &&
      Boolean(this.endpoint.port) && this.endpoint.pathname === "/" && !this.endpoint.search &&
      !this.endpoint.hash && !this.endpoint.username && !this.endpoint.password &&
      config.principal_id === "lab:planner-reader" && config.tenant_id === "company-a" &&
      config.source_kind === "SIMULATED_ENTERPRISE_SOURCE" && /^[A-Za-z0-9._~-]{32,256}$/.test(config.token),
      "SOURCE_READER_CONFIG_INVALID", 400);
    // 口令不进入任务或模型。地址/身份进入配置摘要，重启不能换服务后沿用旧 Run。
    this.identity = { endpoint: config.base_url, principal_id: config.principal_id,
      instance_id: config.instance_id, tenant_id: config.tenant_id, source_kind: config.source_kind };
  }
  static fromFile(path: string): SimulatedSourceReader {
    const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const stat = fstatSync(fd);
      requireThat(stat.isFile() && !(stat.mode & 0o077) && stat.size < 16384, "PRIVATE_SOURCE_READER_CONFIG_REQUIRED", 400);
      return new SimulatedSourceReader(JSON.parse(readFileSync(fd, "utf8")) as ReaderConfig);
    } finally { closeSync(fd); }
  }
  private async read(s: SourceBinding): Promise<Json> {
    requireThat(s.instance_id === this.config.instance_id && s.tenant_id === this.config.tenant_id,
      "SOURCE_READER_SCOPE_MISMATCH", 403);
    return new Promise((resolve, reject) => {
      const fail = () => reject(new RuntimeError("SOURCE_ACCESS_UNAVAILABLE", 503));
      const req = request({ hostname: "127.0.0.1", port: this.endpoint.port, method: "GET",
        path: `/v1/objects/${s.namespace}/${s.object_id}/versions/${s.version}`,
        headers: { Authorization: "Bearer " + this.config.token, Accept: "application/json" } }, res => {
        const parts: Buffer[] = []; let length = 0;
        res.on("data", (part: Buffer) => {
          length += part.length;
          if (length > 1048576) { res.destroy(); fail(); } else parts.push(part);
        });
        res.on("error", fail);
        res.on("end", () => {
          if (res.statusCode !== 200 || res.headers["content-type"] !== "application/json") { fail(); return; }
          try { resolve(JSON.parse(Buffer.concat(parts).toString("utf8")) as Json); } catch { fail(); }
        });
      });
      const timer = setTimeout(() => { req.destroy(); fail(); }, 5000);
      req.on("error", fail); req.on("close", () => clearTimeout(timer)); req.end();
    });
  }
  async authorize(bundle: ReleaseBundle): Promise<Json[]> {
    validateSourceBindings(bundle);
    const receipts: Json[] = [];
    for (const s of bundle.external_sources ?? []) {
      const value = await this.read(s);
      const acl = value.acl_decision as Json | undefined;
      requireThat(value.source_kind === "SIMULATED_ENTERPRISE_SOURCE" && value.schema_version === 1 &&
        ["instance_id", "tenant_id", "namespace", "object_id", "version", "publication_id", "content_sha256", "publisher_id", "metadata"]
          .every(k => canonical(value[k]) === canonical(s[k as keyof SourceBinding])) &&
        acl?.principal_id === this.config.principal_id && acl.permission === "READ" &&
        acl.scope === "EXACT_OBJECT_AND_TENANT" && acl.cache_authorization_seconds === 0,
        "SOURCE_RESPONSE_IDENTITY_MISMATCH", 403);
      requireThat(typeof value.content_base64 === "string", "SOURCE_CONTENT_INVALID", 403);
      const raw = Buffer.from(value.content_base64, "base64");
      const content = raw.toString("utf8");
      requireThat(raw.toString("base64") === value.content_base64 && sha(content) === s.content_sha256,
        "SOURCE_CONTENT_DIGEST_MISMATCH", 403);
      for (const m of bundle.materials.filter(m => m.external_source_ref === s.ref))
        requireThat(raw.subarray(m.source_byte_start, m.source_byte_end).toString("utf8") === m.content && sha(m.content) === m.sha256,
          "SOURCE_PROJECTION_MISMATCH", 403);
      if (s.namespace === "jira") this.verifyRequest(bundle, JSON.parse(content) as Json);
      receipts.push({ source_ref: s.ref, instance_id: s.instance_id, publication_id: s.publication_id,
        content_sha256: s.content_sha256, observed_at: value.observed_at, acl_decision: acl });
    }
    return receipts;
  }
  private verifyRequest(bundle: ReleaseBundle, issue: Json): void {
    const r = bundle.review_request!;
    requireThat(issue.source_kind === "SIMULATED_JIRA_ISSUE" && issue.issue_key === "EGAPLAB-100" && issue.revision === 1 &&
      r.request_id === "jira:EGAPLAB-100" && r.revision === 1 && r.goal === issue.summary &&
      r.owner_ref === issue.owner_ref && r.requester_ref === issue.requester_ref &&
      canonical(r.non_goals) === canonical(issue.non_goals) && Array.isArray(issue.requirements), "JIRA_REQUEST_BINDING_MISMATCH", 403);
    const rows = (issue.requirements as Json[]).map(row => ({ id: row.id, kind: row.kind, statement: row.statement, owner_ref: row.owner_ref,
      material_ref: /^R[1-6]$/.test(String(row.id)) ? "rule:" + row.id : "jira:EGAPLAB-100", authority: "LOCAL_CALIBRATION", status: "PENDING" }));
    requireThat(canonical(rows) === canonical(r.requirements), "JIRA_REQUIREMENTS_CHANGED", 403);
  }
}
