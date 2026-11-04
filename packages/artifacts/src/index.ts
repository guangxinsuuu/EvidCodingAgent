import { createHash } from "node:crypto";

declare const artifactRefBrand: unique symbol;
export type ArtifactRef = `sha256:${string}` & {
  readonly [artifactRefBrand]: true;
};

/** 后续持久化适配器遵循的最小字节存储接口；引用不包含文件路径。 */
export interface ArtifactStore {
  put(bytes: Uint8Array): Promise<ArtifactRef>;
  get(ref: ArtifactRef): Promise<Uint8Array | null>;
}

/**
 * 对原始字节计算小写 SHA-256；不执行文本解码、路径规范化或业务裁决。
 * put → 本函数 → 内容引用；相同文件名不意味着相同内容身份。
 */
export function sha256Bytes(bytes: Uint8Array): string {
  if (!(bytes instanceof Uint8Array)) {
    throw new TypeError("Artifact content must be bytes.");
  }
  return createHash("sha256").update(bytes).digest("hex");
}

/**
 * 仅供当前骨架的测试/本地开发使用：进程退出即丢失，不具备鉴权或持久性。
 * 生产 ArtifactStore、租户授权和证据保留由后续章节实现，不能由此类替代。
 */
export class MemoryArtifactStore implements ArtifactStore {
  readonly #objects = new Map<ArtifactRef, Uint8Array>();

  /**
   * 保存独立快照后计算引用；调用者改变原 Buffer 不应改变已登记的工件。
   * 相同内容复用引用，本实现不产生磁盘、网络或其他外部副作用。
   */
  async put(bytes: Uint8Array): Promise<ArtifactRef> {
    if (!(bytes instanceof Uint8Array)) {
      throw new TypeError("Artifact content must be bytes.");
    }
    const snapshot = new Uint8Array(bytes);
    const ref = `sha256:${sha256Bytes(snapshot)}` as ArtifactRef;
    if (!this.#objects.has(ref)) {
      this.#objects.set(ref, snapshot);
    }
    return ref;
  }

  /**
   * 缺失返回 null；引用非法或内容与摘要不符时拒绝，不返回疑似损坏字节。
   * 返回值再次复制，避免一次读取让调用者取得可改写内部对象的别名。
   */
  async get(ref: ArtifactRef): Promise<Uint8Array | null> {
    if (typeof ref !== "string" || !/^sha256:[0-9a-f]{64}$/.test(ref)) {
      throw new TypeError("Invalid artifact reference.");
    }
    const bytes = this.#objects.get(ref);
    if (!bytes) {
      return null;
    }
    if (`sha256:${sha256Bytes(bytes)}` !== ref) {
      throw new Error("Artifact integrity check failed.");
    }
    return new Uint8Array(bytes);
  }
}
