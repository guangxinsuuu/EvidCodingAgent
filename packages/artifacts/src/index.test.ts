import assert from "node:assert/strict";
import { test } from "node:test";
import { MemoryArtifactStore, sha256Bytes } from "./index.js";
import type { ArtifactRef } from "./index.js";

test("摘要符合已知 SHA-256 向量，字节变化改变内容身份", () => {
  assert.equal(
    sha256Bytes(Buffer.from("abc")),
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  );
  assert.notEqual(sha256Bytes(Buffer.from("abc")), sha256Bytes(Buffer.from("abd")));
});

test("重复内容复用引用，写入和读出都隔断可变字节别名", async () => {
  const store = new MemoryArtifactStore();
  const input = Uint8Array.of(1, 2, 3);
  const ref = await store.put(input);
  input[0] = 9;
  const firstRead = await store.get(ref);
  assert.deepEqual(firstRead, Uint8Array.of(1, 2, 3));
  assert.ok(firstRead);
  firstRead[1] = 8;
  assert.deepEqual(await store.get(ref), Uint8Array.of(1, 2, 3));
  assert.equal(await store.put(Uint8Array.of(1, 2, 3)), ref);
});

test("缺失对象明确返回 null，非法内容引用拒绝", async () => {
  const store = new MemoryArtifactStore();
  assert.equal(await store.get(`sha256:${"0".repeat(64)}` as ArtifactRef), null);
  await assert.rejects(store.get("../not-an-artifact" as ArtifactRef), {
    name: "TypeError",
    message: "Invalid artifact reference.",
  });
});

test("错误内容类型不能被 Uint8Array 隐式转换成空工件", async () => {
  const invalid = "not-bytes" as unknown as Uint8Array;
  const store = new MemoryArtifactStore();
  assert.throws(() => sha256Bytes(invalid), TypeError);
  await assert.rejects(store.put(invalid), TypeError);
});
