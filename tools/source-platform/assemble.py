#!/usr/bin/env python3
"""来源适配器：虚拟 Jira + 固定规则版本 → 正式请求 → 独立产品发布材料。

操作员固定产品 B/C；需求服务提供任务内容，业务发布者提供规则。三者不能互相
冒充权威。新增要求缺少检查能力时照常保留缺项，绝不为了可执行而丢掉要求。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from client import SourceClient, SourceClientError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.registry.release_assembler import assemble_review_request, _material, MAX_BUNDLE_BYTES
from tools.registry.review_request import validate_review_request


def normalize_issue(issue: dict, baseline: str, candidate: str, repository: str) -> dict:
    """只接受首期已登记任务类型；源系统没有的批准状态一律保持 PENDING。"""
    if issue.get("source_kind") != "SIMULATED_JIRA_ISSUE" or issue.get("issue_key") != "EGAPLAB-100" \
            or issue.get("revision") != 1 or issue.get("authority_scope") != "SYNTHETIC_LOCAL_CALIBRATION":
        raise SourceClientError("JIRA_SOURCE_IDENTITY_INVALID")
    requirements = []
    for row in issue["requirements"]:
        known = row["id"] in {"R1", "R2", "R3", "R4", "R5", "R6"}
        if known and row.get("rule_ref") != "rules/refund-rules/versions/lab-v1-extracted":
            raise SourceClientError("JIRA_RULE_VERSION_MISMATCH")
        requirements.append({**{k: row[k] for k in ("id", "kind", "statement", "owner_ref")},
            "material_ref": "rule:" + row["id"] if known else "jira:EGAPLAB-100",
            "authority": "LOCAL_CALIBRATION", "status": "PENDING"})
    return validate_review_request({"schema_version": 1, "request_id": "jira:EGAPLAB-100", "revision": 1,
        "requester_ref": issue["requester_ref"], "owner_ref": issue["owner_ref"], "goal": issue["summary"],
        "non_goals": issue["non_goals"], "requirements": requirements,
        "baseline": {"commit": baseline, "source_ref": repository, "reason": "操作员指定的比较基线；不代表历史 PASS 已核验。"},
        "candidate": {"commit": candidate, "source_ref": repository}, "predecessor_run_id": None,
        "known_gaps": [{"id": "source:pending-review", "requirement_refs": [r["id"] for r in requirements],
                        "description": "任务和规则来自独立模拟来源；维护者尚未确认公司验收标准。"}]})


def snapshot(receipt: dict) -> dict:
    """仅持久化来源身份，无 endpoint/token；每次使用由 Runtime 自己的 reader 重新授权。"""
    names = ("instance_id", "tenant_id", "namespace", "object_id", "version", "publication_id",
             "content_sha256", "publisher_id", "metadata")
    return {"ref": receipt["namespace"] + ":" + receipt["object_id"], **{k: receipt[k] for k in names}}


def assemble(client: SourceClient, product_repo: Path, baseline: str, candidate: str) -> dict:
    """HTTP 读取均在装配前完成；来源失败直接拒绝，不交付部分成功任务包。"""
    if client.config["principal_id"] != "lab:planner-reader":
        raise SourceClientError("PLANNER_READER_REQUIRED")
    jira = client.read("jira", "EGAPLAB-100", "1")
    rule = client.read("rules", "refund-rules", "lab-v1-extracted")
    request = normalize_issue(json.loads(jira["content"]), baseline, candidate,
                              "https://github.com/guangxinsuuu/EvidSupportSUT.git")
    bundle = assemble_review_request(ROOT, request, candidate_root=product_repo, product_id="evid-support",
                                    input_provenance="REGISTERED_EXTERNAL_PRODUCT", published_rules=rule)
    source = jira["metadata"]["source"]
    material = _material("jira:EGAPLAB-100", "UPSTREAM_REQUIREMENTS", "模拟 Jira 任务原件",
                         source["commit"], source["path"], jira["content"].encode())
    material.update(external_source_ref="jira:EGAPLAB-100", source_byte_start=0,
                    source_byte_end=len(jira["content"].encode()))
    bundle["materials"].append(material)
    bundle["external_sources"] = [snapshot(jira), snapshot(rule)]
    bundle["gaps"].append({"code": "SIMULATED_SOURCES_PENDING_REVIEW",
                           "detail": "虚拟 Jira 与规则已独立读取；不代表真实企业身份、批准或当前候选覆盖。"})
    if len(json.dumps(bundle, ensure_ascii=False).encode()) > MAX_BUNDLE_BYTES:
        raise SourceClientError("RELEASE_BUNDLE_TOO_LARGE")
    return bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reader-config", type=Path, required=True)
    parser.add_argument("--product-repo", type=Path, required=True)
    parser.add_argument("--baseline", required=True); parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if "docs" in output.parts or output.name.lower() == "readme.md":
        raise SourceClientError("OUTPUT_FORBIDDEN")
    bundle = assemble(SourceClient.from_file(args.reader_config), args.product_repo, args.baseline, args.candidate)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(bundle, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": "ASSEMBLED_FROM_SIMULATED_SOURCES", "sources": len(bundle["external_sources"]),
                      "requirements": bundle["requirement_refs"], "coverage": [], "final_qualification": False}))


if __name__ == "__main__":
    try: main()
    except Exception as error:
        print(json.dumps({"status": "REJECTED", "error_code": getattr(error, "code", "SOURCE_ASSEMBLY_FAILED")}))
        raise SystemExit(2)
