import assert from "node:assert/strict";
import { test } from "node:test";
import { parseEnvironmentInstance, parseLabIdentity } from "./lab.js";

const UUID = "b700a836-eb63-4b59-a9f8-bc37f3011275";
const identity = {
  experiment_id: `experiment_${UUID}`,
  run_id: `run_${UUID}`,
  check_run_id: `check_${UUID}`,
  execution_attempt_id: `attempt_${UUID}`,
  authority_mode: "file-lab",
  evidence_scope: "calibration-only",
};
const environment = {
  environment_instance_id: `environment_${UUID}`,
  authority_mode: "file-lab",
  evidence_scope: "calibration-only",
  environment_config_digest: "a".repeat(64),
  environment_profile_digest: null,
  source_commit: "8".repeat(40),
  build_revision: "preflight-v2-local-repositories-and-verified-distribution",
  fixture_reset_id: `fixture_${UUID}`,
  lease_epoch: 1,
  endpoints: { gateway: "http://127.0.0.1:4318", order: "https://localhost:4319/api" },
  image_digests: { order: "b".repeat(64) },
  observed_topology_ref: "artifacts/evidence/baseline/observed-topology.json",
  schema_mapping_ref: null,
  resource_manifest_ref: "artifacts/evidence/baseline/resources.json",
  expires_at: "2026-11-04T05:00:00.000Z",
};

test("实验身份只接受file-lab校准投影与对应UUID前缀", () => {
  const input = { ...identity };
  const parsed = parseLabIdentity(input);
  input.run_id = "mutated";
  assert.equal(parsed.run_id, identity.run_id);
  assert.equal(Object.isFrozen(parsed), true);
  const { run_id: _omitted, ...missingRun } = identity;
  for (const value of [missingRun, { ...identity, run_id: identity.check_run_id },
    { ...identity, execution_attempt_id: `attempt_${UUID.toUpperCase()}` },
    { ...identity, run_id: `run_${UUID}\n` },
    { ...identity, authority_mode: "database" }, { ...identity, evidence_scope: "final" },
    { ...identity, approved: true }, null, []]) {
    assert.throws(() => parseLabIdentity(value), TypeError);
  }
});

test("环境投影深复制并冻结端口和镜像，解析不把已经过期解释为形状错误", () => {
  const input = { ...environment, endpoints: { ...environment.endpoints }, image_digests: { ...environment.image_digests } };
  const parsed = parseEnvironmentInstance(input);
  input.endpoints.gateway = "http://127.0.0.1:9999";
  input.image_digests.order = "c".repeat(64);
  assert.equal(parsed.endpoints.gateway, environment.endpoints.gateway);
  assert.equal(parsed.image_digests.order, environment.image_digests.order);
  assert.equal(Object.isFrozen(parsed), true);
  assert.equal(Object.isFrozen(parsed.endpoints), true);
  assert.equal(Object.isFrozen(parsed.image_digests), true);
  assert.equal(parseEnvironmentInstance({ ...environment, expires_at: "2000-01-01T00:00:00.000Z" }).expires_at,
    "2000-01-01T00:00:00.000Z");
});

test("拒绝无效epoch、摘要、未实施Profile、身份串用和非规范时间", () => {
  const invalid = [
    ...[0, -1, "1", 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1].map((lease_epoch) => ({ ...environment, lease_epoch })),
    { ...environment, environment_config_digest: `sha256:${"a".repeat(64)}` },
    { ...environment, environment_config_digest: `${"a".repeat(64)}\n` },
    { ...environment, image_digests: { order: "SHA256:bad" } },
    { ...environment, environment_profile_digest: "a".repeat(64) },
    { ...environment, fixture_reset_id: `environment_${UUID}` },
    { ...environment, source_commit: "8".repeat(39) },
    { ...environment, unexpected_secret: "not-for-logs" },
    ...["2026-02-30T00:00:00.000Z", "2026-11-04", "2026-11-04T05:00:00Z", "secret-time"].map((expires_at) => ({ ...environment, expires_at })),
  ];
  for (const value of invalid) {
    assert.throws(() => parseEnvironmentInstance(value), { name: "TypeError", message: "Invalid laboratory contract." });
  }
});

test("端口记录拒绝凭据、query秘密、非HTTP协议；证据引用拒绝路径越界", () => {
  for (const gateway of ["file:///etc/passwd", "ftp://localhost/data", "http://user:secret@localhost:1234",
    "http://localhost:1234/?token=secret", "http://localhost:1234/#secret", " http://localhost:1234", "not-a-url"]) {
    assert.throws(() => parseEnvironmentInstance({ ...environment, endpoints: { gateway } }), TypeError);
  }
  for (const resource_manifest_ref of ["../outside.json", "/tmp/resources.json", "artifacts/../outside.json",
    "artifacts//resources.json", "C:\\tmp\\resources.json", "https://host/?secret=x"]) {
    assert.throws(() => parseEnvironmentInstance({ ...environment, resource_manifest_ref }), TypeError);
  }
});

test("不执行访问器，不接受隐藏字段、symbol或原型对象", () => {
  const getter = Object.defineProperty({ ...environment }, "lease_epoch", {
    enumerable: true, get() { throw new Error("getter should not run"); },
  });
  assert.throws(() => parseEnvironmentInstance(getter), { message: "Invalid laboratory contract." });
  assert.throws(() => parseEnvironmentInstance(Object.assign(Object.create({ inherited: true }), environment)), TypeError);
  assert.throws(() => parseEnvironmentInstance({ ...environment, [Symbol("hidden")]: true }), TypeError);
  assert.throws(() => parseEnvironmentInstance(Object.defineProperty({ ...environment }, "hidden", { value: true })), TypeError);
});
