import { createHash } from "node:crypto";
import { crc32, deflateSync } from "node:zlib";

export type SyntheticFixture = "valid-png" | "invalid-png" | "unsupported-mime";

function chunk(type: string, payload: Buffer): Buffer {
  const name = Buffer.from(type, "ascii");
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([name, payload])));
  return Buffer.concat([header, name, payload, checksum]);
}

/**
 * submitFixture → 本函数 → 可复现的纯合成图片；不接受文件路径或用户图片。
 * 16×16 RGB PNG足以经过上游 resize/crop，颜色没有类别真值，因此不能测模型准确率。
 * PNG 的 CRC 与压缩数据均由标准库生成，测试再独立解压核对真实像素布局。
 */
export function syntheticFixture(kind: SyntheticFixture): {
  bytes: Buffer; contentType: string; filename: string; sha256: string;
} {
  let bytes: Buffer;
  if (kind === "valid-png") {
    const header = Buffer.alloc(13);
    header.writeUInt32BE(16, 0); header.writeUInt32BE(16, 4);
    header[8] = 8; header[9] = 2; // 8-bit RGB，无交错、标准压缩/过滤。
    const rows = Buffer.alloc(16 * (1 + 16 * 3));
    for (let y = 0; y < 16; y++) {
      for (let x = 0; x < 16; x++) rows[y * 49 + 1 + x * 3] = 128;
    }
    bytes = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
      chunk("IHDR", header), chunk("IDAT", deflateSync(rows)), chunk("IEND", Buffer.alloc(0))]);
  } else if (kind === "invalid-png" || kind === "unsupported-mime") {
    bytes = Buffer.from("egap synthetic fixture: these bytes are not an image", "utf8");
  } else throw new TypeError("Unknown synthetic fixture.");
  return { bytes, contentType: kind === "unsupported-mime" ? "text/plain" : "image/png",
    filename: kind === "unsupported-mime" ? "egap-synthetic.txt" : "egap-synthetic.png",
    sha256: createHash("sha256").update(bytes).digest("hex") };
}
