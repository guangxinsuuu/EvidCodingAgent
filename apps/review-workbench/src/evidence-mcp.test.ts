import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { RegisteredArchive, createArchiveMcpServer, McpArchiveReader } from "./evidence-mcp.js";
import { loadEvidenceArchive, parseEvidenceArchive } from "./evidence-archive.js";
import type { EvidenceMaterial } from "@egap/contracts";
import type { WorkflowArchive } from "./workflow-types.js";

const token = "mcp-test-token-".repeat(4);
const content = "第一行\n第二行\n第三行";
const material: EvidenceMaterial = { material_ref: "m:test", kind: "SAVED_PRODUCT_DRAFT", title: "固定材料", content,
  sha256: createHash("sha256").update(content).digest("hex"), provenance: { tenant_id: "t", historical_run_id: "h",
    job_id: "j", ticket_id: "ticket", ticket_revision: 1, phase: "before", candidate_sha: "a".repeat(40), source_ref: "archive:unit", source_sha256: "b".repeat(64) } };
const input = { run_id: "run-a", operation_id: "read-a", material_ref: material.material_ref, sha256: material.sha256, start_line: 1, end_line: 2 };

test("MCP exposes scoped read tools, requires private auth and denies browser cross-origin", async () => {
  const registry = new RegisteredArchive(); registry.register("run-a", [material]);
  const app = createArchiveMcpServer(registry, token);
  const headers = { host: "127.0.0.1:4700", authorization: `Bearer ${token}`, "mcp-protocol-version": "2025-06-18" };
  try {
    const call = (method: string, params: object, extra = {}) => app.inject({ method: "POST", url: "/mcp", headers: { ...headers, ...extra }, payload: { jsonrpc: "2.0", id: 1, method, params } });
    assert.equal((await call("initialize", {})).json().result.protocolVersion, "2025-06-18");
    assert.deepEqual((await call("tools/list", {})).json().result.tools.map((t: {name:string}) => t.name), ["evidence.list_registered", "evidence.read_registered"]);
    assert.equal((await call("tools/list", {}, { origin: "https://attacker.example" })).statusCode, 403);
    assert.equal((await call("tools/list", {}, { authorization: "Bearer browser-session" })).statusCode, 403);
    assert.equal((await call("tools/call", { name: "review.accept", arguments: {} })).json().error.code, -32602);
    const read = (await call("tools/call", { name: "evidence.read_registered", arguments: input })).json().result;
    assert.equal(read.isError, false); assert.equal(read.structuredContent.content, "第一行\n第二行");
    assert.equal((await call("tools/call", { name: "evidence.read_registered", arguments: { ...input, run_id: "other" } })).json().result.isError, true);
    assert.equal((await call("tools/call", { name: "evidence.read_registered", arguments: { ...input, path: "/etc/passwd" } })).json().result.isError, true);
    assert.equal((await app.inject({ method: "GET", url: "/mcp", headers })).statusCode, 405);
  } finally { await app.close(); }
});

test("frozen registry does not replace material under reused run identity or accept wrong hash/range", () => {
  const registry = new RegisteredArchive(); registry.register("run-a", [material]);
  assert.deepEqual(registry.read(input), registry.read({ ...input, operation_id: "read-again" }));
  assert.throws(() => registry.register("run-a", [{ ...material, title: "replacement" }]), /SCOPE_CONFLICT/);
  assert.throws(() => registry.read({ ...input, sha256: "a".repeat(64) }), /SCOPE_DENIED/);
  assert.throws(() => registry.read({ ...input, end_line: 4 }), /RANGE_REJECTED/);
  assert.throws(() => registry.read({ ...input, start_line: 0 }), /RANGE_REJECTED/);
});

test("real loopback MCP HTTP adapter initializes and consumes immutable evidence bytes", async () => {
  const registry = new RegisteredArchive(); registry.register("run-a", [material]);
  const app = createArchiveMcpServer(registry, token);
  try {
    await app.listen({ host: "127.0.0.1", port: 0 });
    const address = app.server.address(); assert.ok(address && typeof address !== "string");
    const reader = new McpArchiveReader(`http://127.0.0.1:${address.port}/mcp`, token);
    assert.equal((await reader.read(input)).content, "第一行\n第二行");
    await assert.rejects(reader.read({ ...input, run_id: "other" }), /MCP_READ_REJECTED/);
  } finally { await app.close(); }
});

test("historical importer rejects altered archive instead of silently trusting JSON", () => {
  assert.throws(() => parseEvidenceArchive(Buffer.from('{"cases":[]}')), /ARCHIVE_DIGEST_MISMATCH/);
});

test("available pinned local archive provides four exact jobs and twelve originals, never provider raw output", async t => {
  let archive: WorkflowArchive;
  try { archive = await loadEvidenceArchive(process.cwd()); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") { t.skip("private archive unavailable in this checkout"); return; } throw error; }
  assert.equal(archive.targets.length, 4); assert.equal(archive.materials.length, 12);
  assert.equal(archive.materials.some(m => m.kind === "RAW_PROVIDER_RESPONSE"), false);
  for (const delivery of archive.materials.filter(m => m.kind === "DELIVERY_RECEIPT")) {
    const receipt = JSON.parse(delivery.content); const note: EvidenceMaterial = archive.materials.find(m => m.material_ref === receipt.delivered_note_ref)!;
    assert.equal(note.sha256, receipt.delivered_note_sha256);
    const body = JSON.parse(note.content); assert.equal(body.private, true);
    assert.equal(body.external_message_id, receipt.external_message_id);
    assert.equal(body.job_id, receipt.job_id); assert.equal(body.ticket_revision, receipt.ticket_revision);
  }
});
