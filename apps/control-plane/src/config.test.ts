import assert from "node:assert/strict";
import test from "node:test";
import { ConfigurationError, parseConfig } from "./config.js";

const valid = { EGAP_MODE: "local-dev", EGAP_PORT: "4317", PATH: "/ignored-system-path" };

test("配置显式准入：回环默认值、逻辑引用与不可变返回值", () => {
  const config = parseConfig({ ...valid, EGAP_DATABASE_REF: "local-postgres" });
  assert.deepEqual(config, { mode: "local-dev", host: "127.0.0.1", port: 4317, logLevel: "info", databaseRef: "local-postgres" });
  assert.equal(Object.isFrozen(config), true);
  assert.equal(parseConfig({ ...valid, EGAP_HOST: "::1" }).host, "::1");
});

test("拒绝缺字段、未知配置、非开发模式和非回环监听", () => {
  for (const env of [
    {}, { EGAP_MODE: "local-dev" }, { ...valid, EGAP_MODE: "production" },
    { ...valid, EGAP_HOST: "0.0.0.0" }, { ...valid, EGAP_HOST: "localhost" },
    { ...valid, EGAP_HOST: "192.168.1.2" }, { ...valid, EGAP_PRTO: "4317" },
    { ...valid, EGAP_LOG_LEVEL: "everything" },
  ]) assert.throws(() => parseConfig(env), ConfigurationError);
});

test("端口不接受隐式转换或越界值", () => {
  for (const port of ["0", "-1", "65536", "3.5", "1e3", " 4317", "4317x", "04317", "NaN"]) {
    assert.throws(() => parseConfig({ ...valid, EGAP_PORT: port }), ConfigurationError);
  }
  assert.equal(parseConfig({ ...valid, EGAP_PORT: "65535" }).port, 65535);
});

test("配置拒绝日志只含字段与固定原因，不泄露URL中的凭据", () => {
  const secret = "postgres://user:secret-value@host/db";
  assert.throws(() => parseConfig({ ...valid, EGAP_DATABASE_REF: secret }), (error: unknown) => {
    assert.ok(error instanceof ConfigurationError);
    assert.equal(error.field, "EGAP_DATABASE_REF");
    assert.equal(error.message.includes("secret-value"), false);
    return true;
  });
});
