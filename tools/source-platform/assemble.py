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
            or type(issue.get("revision")) is not int or issue["revision"] not in (1,2) or issue.get("authority_scope") != "SYNTHETIC_LOCAL_CALIBRATION":
        raise SourceClientError("JIRA_SOURCE_IDENTITY_INVALID")
    requirements = []
    for row in issue["requirements"]:
        known = row["id"] in {"R1", "R2", "R3", "R4", "R5", "R6"}
        if known and row.get("rule_ref") != "rules/refund-rules/versions/lab-v1-extracted":
            raise SourceClientError("JIRA_RULE_VERSION_MISMATCH")
        requirements.append({**{k: row[k] for k in ("id", "kind", "statement", "owner_ref")},
            "material_ref": "rule:" + row["id"] if known else "jira:EGAPLAB-100",
            "authority": "LOCAL_CALIBRATION", "status": "PENDING"})
    return validate_review_request({"schema_version": 1, "request_id": "jira:EGAPLAB-100", "revision": issue["revision"],
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


def attach_sources(bundle:dict,client:SourceClient,objects:list[dict]):
    """固定登记 → 实时授权原件 → 有来源的材料；绝不合并上游建议到需求/覆盖。

这里只进行装配前的身份检查。完整跨对象语义由 Runtime.validateSourceBindings
再次验证，并在每次使用时重新读取来源。诊断原件目录中的指针不自动跟随下载。
"""
    if client.config["principal_id"]!="lab:planner-reader":raise SourceClientError("PLANNER_READER_REQUIRED")
    kinds={"diagnostics":"UPSTREAM_DIAGNOSIS_INPUT","providers":"DECLARED_PROVIDER_CONTRACT","acceptance":"DECLARED_QA_CONTRACT"}
    for obj in objects:
        receipt=client.read(obj["namespace"],obj["object_id"],obj["version"])
        value=json.loads(receipt["content"])
        if obj["namespace"]=="diagnostics" and value.get("candidate")!={
            "repository_url":bundle["product_binding"]["repository_url"],"commit":bundle["candidate_sha"]}:
            raise SourceClientError("PROJECT1_CANDIDATE_MISMATCH")
        source=receipt["metadata"]["source"];ref=obj["namespace"]+":"+obj["object_id"]
        if receipt["publisher_id"]!="lab:"+obj["publisher"] or receipt["metadata"]["profile"]!=obj["profile"] \
                or source["path"]!=obj["path"] or source["repository_url"]!=f"https://github.com/guangxinsuuu/{obj['repository']}.git":
            raise SourceClientError("UPSTREAM_PUBLICATION_MISMATCH")
        material=_material(ref,kinds[obj["namespace"]],ref,source["commit"],source["path"],receipt["content"].encode())
        material.update(external_source_ref=ref,source_byte_start=0,source_byte_end=len(receipt["content"].encode()))
        bundle["materials"].append(material);bundle["external_sources"].append(snapshot(receipt))


def assemble(client: SourceClient, product_repo: Path, baseline: str, candidate: str,*,qa_client=None,upstream_client=None,jira_version="1") -> dict:
    """HTTP 读取均在装配前完成；来源失败直接拒绝，不交付部分成功任务包。"""
    if client.config["principal_id"] != "lab:planner-reader":
        raise SourceClientError("PLANNER_READER_REQUIRED")
    if jira_version not in ("1","2"):raise SourceClientError("JIRA_VERSION_NOT_REGISTERED")
    jira = client.read("jira", "EGAPLAB-100", jira_version)
    if str(json.loads(jira["content"]).get("revision"))!=jira_version:raise SourceClientError("JIRA_REVISION_MISMATCH")
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
    if qa_client:
        objects=json.loads((ROOT/"config/phase-acceptance-publications.v1.json").read_text())["objects"]
        attach_sources(bundle,qa_client,[obj for obj in objects if obj["object_id"]=="phase-review-contract"])
    if upstream_client:
        objects=json.loads((ROOT/"config/upstream-publications.v1.json").read_text())["objects"]
        attach_sources(bundle,upstream_client,objects)
        bundle["gaps"].append({"code":"PROJECT1_ORIGINALS_NOT_INGESTED",
            "detail":"模拟 Project 1 交接只携带归档指纹和假设；没有导入原件、授权修复或当前候选覆盖。"})
    bundle["gaps"].append({"code": "SIMULATED_SOURCES_PENDING_REVIEW",
                           "detail": "虚拟 Jira 与规则已独立读取；不代表真实企业身份、批准或当前候选覆盖。"})
    if len(json.dumps(bundle, ensure_ascii=False).encode()) > MAX_BUNDLE_BYTES:
        raise SourceClientError("RELEASE_BUNDLE_TOO_LARGE")
    return bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reader-config", type=Path, required=True)
    parser.add_argument("--product-repo", type=Path, required=True)
    parser.add_argument("--qa-reader-config",type=Path);parser.add_argument("--upstream-reader-config",type=Path)
    parser.add_argument("--jira-version",choices=("1","2"),default="1")
    parser.add_argument("--baseline", required=True); parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if "docs" in output.parts or output.name.lower() == "readme.md":
        raise SourceClientError("OUTPUT_FORBIDDEN")
    bundle = assemble(SourceClient.from_file(args.reader_config), args.product_repo, args.baseline, args.candidate,
        qa_client=SourceClient.from_file(args.qa_reader_config) if args.qa_reader_config else None,
        upstream_client=SourceClient.from_file(args.upstream_reader_config) if args.upstream_reader_config else None,jira_version=args.jira_version)
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
