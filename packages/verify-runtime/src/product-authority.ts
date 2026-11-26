import { readFileSync } from "node:fs";
import { requireThat, type ReleaseBundle } from "./types.js";

export interface ProductBinding {
  schema_version: 1; product_id: string; repository_url: string; logical_prefix: string;
  baseline: { commit: string; tree: string }; candidate: { commit: string; tree: string };
  egap_commit: string; authority: "OPERATOR_REGISTERED_LOCAL_CHECKOUT"; remote_attestation: false;
}
const profiles = (JSON.parse(readFileSync(new URL("../../../config/external-products.v1.json", import.meta.url), "utf8")) as {
  products: { id: string; repository_url: string; logical_prefix: string }[];
}).products;

/**
 * 可信装配器 → Runtime 导入时核对产品登记、版本和真实路径映射。
 * origin 与提交存在性由装配器查 Git；此处不能把本机登记升级成 GitHub 签名或企业批准。
 * 历史 v1/v2 包保留原义，只有显式外部来源包必须携带这组身份。
 */
export function validateProductBinding(bundle: ReleaseBundle): void {
  if (bundle.input_provenance !== "REGISTERED_EXTERNAL_PRODUCT") {
    requireThat(bundle.product_binding === undefined, "UNEXPECTED_PRODUCT_BINDING", 400);
    return;
  }
  const p = bundle.product_binding;
  requireThat(bundle.schema_version === 2 && p && p.schema_version === 1, "PRODUCT_BINDING_REQUIRED", 400);
  requireThat(Object.keys(p).sort().join(",") === ["schema_version", "product_id", "repository_url", "logical_prefix",
    "baseline", "candidate", "egap_commit", "authority", "remote_attestation"].sort().join(",") &&
    [p.baseline, p.candidate].every(side => side && Object.keys(side).sort().join(",") === "commit,tree"),
    "PRODUCT_BINDING_FIELDS_INVALID", 400);
  const profile = profiles.find(item => item.id === p.product_id);
  requireThat(profile && p.repository_url === profile.repository_url && p.logical_prefix === profile.logical_prefix &&
    p.authority === "OPERATOR_REGISTERED_LOCAL_CHECKOUT" && p.remote_attestation === false, "PRODUCT_REGISTRATION_MISMATCH", 400);
  requireThat(p.baseline?.commit === bundle.baseline_sha && p.candidate?.commit === bundle.candidate_sha &&
    [p.baseline.tree, p.candidate.tree, p.egap_commit].every(value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value)),
    "PRODUCT_REVISION_MISMATCH", 400);
  requireThat(bundle.review_request && [bundle.review_request.baseline, bundle.review_request.candidate]
    .every(side => side.source_ref === p.repository_url), "REVIEW_PRODUCT_SOURCE_MISMATCH", 400);
  for (const material of bundle.materials) {
    if (!["BASELINE_SOURCE", "CANDIDATE_SOURCE", "UNTRUSTED_DIFF"].includes(material.kind)) continue;
    const path = material.source_path;
    requireThat(material.repository_id === p.product_id && typeof path === "string" && path.length > 0 &&
      !path.includes("\\") && !path.includes(":") && !/[\x00-\x1f\x7f]/.test(path) &&
      !path.split("/").some(part => ["", ".", "..", ".git", ".aws", ".ssh", ".local"].includes(part) || part.startsWith(".env")) &&
      material.path === p.logical_prefix + path, "PRODUCT_MATERIAL_PATH_MISMATCH", 400);
    requireThat(material.git_commit === (material.kind === "BASELINE_SOURCE" ? p.baseline.commit : p.candidate.commit),
      "PRODUCT_MATERIAL_REVISION_MISMATCH", 400);
  }
}
