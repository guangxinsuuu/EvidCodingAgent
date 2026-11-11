#!/usr/bin/env python3
"""只读校验本地DRAFT目录：CLI → YAML解析 → 引用/阶段检查 → 来源字节核对。

本工具不导入测试或产品模块、不连接业务系统、不派发实验。VALID_DRAFT只表示
登记与证据引用一致，不代表业务规则获批、能力已校准或当前候选通过。
运行：tools/registry/.venv/bin/python tools/registry/validate_catalog.py
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]
PACK = Path("registry/packs/saas-support")
FILES = ("ownership.yaml", "sources.yaml", "test-catalog.yaml",
         "cross-team-scenarios.yaml", "data-flows.yaml")
MAX_BYTES = 2 * 1024 * 1024


class CatalogError(ValueError):
    """只输出稳定错误码；不回显可能包含原始业务文本的解析异常。"""


def require(condition, code):
    if not condition:
        raise CatalogError(code)


class CatalogLoader(yaml.SafeLoader):
    """拒绝别名与重复键，防止人读到的配置和程序最终读取的值不一致。"""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise CatalogError("YAML_ALIAS_FORBIDDEN")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            require(isinstance(key, str), "YAML_STRING_KEYS_REQUIRED")
            require(key not in result, "YAML_DUPLICATE_KEY")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_yaml(raw: bytes) -> dict:
    """仅接受有界单文档映射；禁用自定义对象构造，不执行来源代码。"""
    require(len(raw) <= MAX_BYTES, "CATALOG_TOO_LARGE")
    try:
        value = yaml.load(raw.decode("utf-8"), Loader=CatalogLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError):
        raise CatalogError("YAML_INVALID") from None
    require(isinstance(value, dict), "CATALOG_MAPPING_REQUIRED")
    return value


def safe_file(root: Path, relative: str) -> Path:
    """目录只能指向仓库普通文件；不跟随软链接，也不读取ignored私有实验材料。"""
    require(isinstance(relative, str), "SOURCE_PATH_INVALID")
    path = PurePosixPath(relative)
    require(bool(relative) and not path.is_absolute() and ".." not in path.parts
            and str(path) == relative and "\\" not in relative, "SOURCE_PATH_INVALID")
    require(path.parts[0] not in {".git", ".codex", ".agents", "artifacts", "output"}
            and not any(part.startswith(".env") or part.endswith(".private.json")
                        for part in path.parts), "PRIVATE_SOURCE_FORBIDDEN")
    current = root
    for part in path.parts:
        current = current / part
        require(not current.is_symlink(), "SOURCE_SYMLINK_FORBIDDEN")
    require(current.is_file(), "SOURCE_FILE_MISSING")
    return current


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def index(rows, code):
    require(isinstance(rows, list) and rows, code)
    result = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("id"), str)
                and re.fullmatch(r"[A-Za-z0-9_.-]+", row["id"]), code)
        require(row["id"] not in result, "DUPLICATE_ASSET_ID")
        result[row["id"]] = row
    return result


def references(value, targets, code):
    require(isinstance(value, list) and all(isinstance(x, str) for x in value), code)
    require(len(value) == len(set(value)) and all(x in targets for x in value), code)


def validate_documents(docs: dict) -> dict:
    """纯元数据校验；各状态单独检查，目录成功不隐式激活任何执行或审批。"""
    require(set(docs) == set(FILES), "CATALOG_FILE_SET_INVALID")
    basis = docs[FILES[0]].get("catalog_basis_commit")
    require(isinstance(basis, str) and re.fullmatch(r"[0-9a-f]{40}", basis), "BASIS_COMMIT_REQUIRED")
    for doc in docs.values():
        require(doc.get("schema_version") == 1 and doc.get("pack_id") == "saas-support"
                and doc.get("status") == "DRAFT" and doc.get("scope") == "LOCAL_SYNTHETIC"
                and doc.get("catalog_basis_commit") == basis and "approval_ref" in doc
                and doc["approval_ref"] is None, "DRAFT_SCOPE_REQUIRED")
    roles = index(docs[FILES[0]].get("roles"), "ROLES_REQUIRED")
    sources = index(docs[FILES[1]].get("sources"), "SOURCES_REQUIRED")
    requirements = index(docs[FILES[1]].get("requirements"), "REQUIREMENTS_REQUIRED")
    capabilities = index(docs[FILES[2]].get("capabilities"), "CAPABILITIES_REQUIRED")
    scenarios = index(docs[FILES[3]].get("scenarios"), "SCENARIOS_REQUIRED")
    flows = index(docs[FILES[4]].get("flows"), "FLOWS_REQUIRED")
    retention = index(docs[FILES[4]].get("retention_policies"), "RETENTION_REQUIRED")
    for role in roles.values():
        require(role.get("assignment_status") == "UNASSIGNED" and "principal_ref" in role
                and role["principal_ref"] is None, "UNCONFIRMED_PERSON_MUST_REMAIN_UNASSIGNED")
    for source in sources.values():
        require(all(k in source for k in ("kind", "maintainer_role", "semantic_owner_role", "authority_scope",
                    "provenance_refs", "collection", "review_status")) and source["provenance_refs"], "SOURCE_CONTEXT_REQUIRED")
        require(isinstance(source["collection"], dict) and source["collection"].get("automatic_collection_enabled") is False
                and source["collection"].get("allowed_fields"), "SOURCE_COLLECTION_SCOPE_REQUIRED")
    for document in docs.values():
        for node in walk(document):
            for key, value in node.items():
                require(key not in {"password", "token", "client_secret", "api_key", "database_url", "shell", "command"},
                        "SECRET_OR_EXECUTABLE_PAYLOAD_FORBIDDEN")
                if key == "approval_ref":
                    require(value is None, "DRAFT_CANNOT_ASSERT_APPROVAL")
                elif key == "review_status":
                    require(value in {"PENDING_REVIEW", "LOCAL_CALIBRATION_PENDING_USER_REVIEW"}, "DRAFT_CANNOT_ASSERT_APPROVAL")
                elif key.endswith("_role"):
                    require(value in roles, "ROLE_REFERENCE_UNKNOWN")
                elif key.endswith("_roles"):
                    references(value, roles, "ROLE_REFERENCE_UNKNOWN")
                elif key == "source_refs":
                    references(value, sources, "SOURCE_REFERENCE_UNKNOWN")
                elif key == "source_ref":
                    require(value in sources, "SOURCE_REFERENCE_UNKNOWN")
                elif key == "requirement_refs":
                    references(value, requirements, "REQUIREMENT_REFERENCE_UNKNOWN")
                elif key.endswith("capability_refs"):
                    references(value, capabilities, "CAPABILITY_REFERENCE_UNKNOWN")
                elif key == "capability_ref":
                    require(value in capabilities, "CAPABILITY_REFERENCE_UNKNOWN")
                elif key == "retention_policy_ref" and value is not None:
                    require(value in retention, "RETENTION_REFERENCE_UNKNOWN")
                elif key == "upstream_flow_refs":
                    references(value, flows, "DATA_FLOW_REFERENCE_UNKNOWN")
                elif key in {"dispatch_enabled", "automatic_collection_enabled", "applies_to_new_candidate"}:
                    require(value is False, "CATALOG_CANNOT_AUTHORIZE_EXECUTION")
                elif key == "current_evaluation_status":
                    require(value == "NOT_RUN", "HISTORY_CANNOT_BECOME_CURRENT_RESULT")
    for capability in capabilities.values():
        require(capability.get("dispatch_enabled") is False, "CATALOG_CANNOT_AUTHORIZE_EXECUTION")
        require(all(isinstance(capability.get(k), list) and capability[k]
                    for k in ("provenance_refs", "preconditions", "observations", "limitations")),
                "CAPABILITY_CONTEXT_REQUIRED")
        require(isinstance(capability.get("side_effects"), list), "CAPABILITY_EFFECTS_REQUIRED")
        require(isinstance(capability.get("entrypoint"), dict), "CAPABILITY_ENTRYPOINT_REQUIRED")
    stages = {}
    for scenario in scenarios.values():
        require(scenario.get("dispatch_enabled") is False and scenario.get("current_evaluation_status") == "NOT_RUN",
                "SCENARIO_DRAFT_STATE_REQUIRED")
        for stage in scenario.get("stages", []):
            ref = scenario["id"] + "." + stage["id"]
            require(stage.get("stage_ref") == ref and ref not in stages, "STAGE_ID_INVALID")
            require(stage.get("preconditions") and stage.get("required_observations"), "STAGE_OBSERVATION_REQUIRED")
            expected = stage.get("verification_only_expectation", {})
            require(expected.get("domain") == "VERIFICATION_ONLY"
                    and expected.get("send_to_business_source") is False
                    and expected.get("send_to_sut") is False, "ORACLE_DOMAIN_BOUNDARY_REQUIRED")
            require(expected.get("oracle_symbol") == "expectation_for"
                    and expected.get("oracle_path") == "tools/multi-system/release_oracle.py"
                    and expected.get("capability_ref") == "check.business", "ORACLE_ENTRYPOINT_INVALID")
            steps = stage.get("steps", [])
            require(steps and [x.get("order") for x in steps] == list(range(1, len(steps) + 1)), "STAGE_ORDER_INVALID")
            require(expected.get("arguments") == {"label": scenario["id"], "stage": stage["id"]},
                    "ORACLE_CASE_BINDING_INVALID")
            if stage["id"] != "initial":
                require(scenario["id"] + ".initial" in stage.get("depends_on", []), "INITIAL_STAGE_PREREQUISITE_REQUIRED")
                actions = [x["capability_ref"] for x in steps]
                mutation = "usage.send_seal" if stage["id"] == "after_seal" else "claim.supplement"
                require(mutation in actions and "sut.reevaluate" in actions
                        and actions.index(mutation) < actions.index("sut.reevaluate"), "MUTATION_MUST_PRECEDE_REEVALUATION")
            stages[ref] = stage
    visiting, visited = set(), set()
    def visit(ref):
        require(ref not in visiting, "STAGE_DEPENDENCY_CYCLE")
        if ref in visited:
            return
        visiting.add(ref)
        for dependency in stages[ref].get("depends_on", []):
            require(dependency in stages, "STAGE_DEPENDENCY_UNKNOWN")
            visit(dependency)
        visiting.remove(ref)
        visited.add(ref)
    for ref in stages:
        visit(ref)
    require(len(scenarios) == 12 and len(stages) == 14, "LOCAL_SCENARIO_BASELINE_INCOMPLETE")
    for policy in retention.values():
        require(policy.get("retention_value") is None and policy.get("approval_ref") is None,
                "RETENTION_UNCONFIRMED")
    for flow in flows.values():
        require(flow.get("data_flow_status") == "DRAFT" and flow.get("approval_ref") is None
                and flow.get("enterprise_admission") == "BLOCKED_PENDING_REGISTRATION", "DATA_FLOW_NOT_APPROVED")
    return {"roles": len(roles), "sources": len(sources), "requirements": len(requirements),
            "capabilities": len(capabilities), "scenarios": len(scenarios), "stages": len(stages),
            "data_flows": len(flows), "retention_policies": len(retention)}


def referenced_files(docs):
    """收集静态出处；历史私有证据仅为报告内的间接引用，不在此解析或复制。"""
    refs = set()
    for doc in docs.values():
        for node in walk(doc):
            for key, value in node.items():
                if key in {"provenance_refs", "evidence_refs", "test_paths"}:
                    require(isinstance(value, list) and all(isinstance(p, str) for p in value), "FILE_REFERENCES_INVALID")
                    refs.update(value)
                elif key in {"report_ref", "finding_ref", "oracle_path"} and value is not None:
                    refs.add(value)
            if isinstance(node.get("entrypoint"), dict):
                refs.add(node["entrypoint"]["path"])
    return refs


def verify_provenance(root, pack, docs, *, registered_reader=None, catalog_reader=None):
    """复用同一组身份/闭包/AST检查，支持工作树审查和固定版本消费两种读取方式。

    默认维持原 CLI 的严格工作树与 Git 双重核对。CatalogReader 提供两个只读回调时，
    所有来源和 AST 都从登记 commit 读取，清单从固定目录 commit 读取；工作树变化
    不再被误判为历史登记错误。调用方必须先验证读取权限，hash 不代表 Owner 批准。
    """
    require((registered_reader is None) == (catalog_reader is None), "PROVENANCE_READERS_REQUIRED")
    read_catalog = catalog_reader or (lambda path: safe_file(root, path).read_bytes())
    manifest = parse_yaml(read_catalog(str(pack / "provenance.yaml")))
    basis = docs[FILES[0]]["catalog_basis_commit"]
    require(manifest.get("catalog_basis_commit") == basis, "PROVENANCE_BASIS_MISMATCH")
    records = manifest.get("files", [])
    require(isinstance(records, list) and all(isinstance(x, dict) and isinstance(x.get("path"), str) for x in records),
            "PROVENANCE_FILES_INVALID")
    by_path = {x["path"]: x for x in records}
    require(len(by_path) == len(records) and set(by_path) == referenced_files(docs), "PROVENANCE_CLOSURE_MISMATCH")
    for path, record in by_path.items():
        require(record.get("git_commit") == basis and record.get("digest_algorithm") == "SHA256_RAW_BYTES", "PROVENANCE_REVISION_INVALID")
        if registered_reader is None:
            file = safe_file(root, path)
            require(hashlib.sha256(file.read_bytes()).hexdigest() == record.get("sha256"), "SOURCE_CONTENT_DRIFT")
            result = subprocess.run(["git", "show", basis + ":" + path], cwd=root, capture_output=True, timeout=10)
            require(result.returncode == 0 and hashlib.sha256(result.stdout).hexdigest() == record["sha256"], "GIT_SOURCE_MISMATCH")
        else:
            require(hashlib.sha256(registered_reader(record["git_commit"], path)).hexdigest() == record.get("sha256"),
                    "GIT_SOURCE_MISMATCH")
    catalog_hashes = manifest.get("catalog_documents", {})
    require(set(catalog_hashes) == set(FILES), "CATALOG_DIGEST_SET_INVALID")
    for name in FILES:
        require(hashlib.sha256(read_catalog(str(pack / name))).hexdigest() == catalog_hashes[name],
                "CATALOG_CONTENT_DRIFT")
    # 静态AST只确认入口真实存在，不import、不运行测试枚举或模块顶层代码。
    for capability in docs[FILES[2]]["capabilities"]:
        entry = capability["entrypoint"]
        if entry.get("symbol") and entry["path"].endswith(".py"):
            raw = (safe_file(root, entry["path"]).read_bytes() if registered_reader is None
                   else registered_reader(by_path[entry["path"]]["git_commit"], entry["path"]))
            nodes = ast.parse(raw).body
            for part in entry["symbol"].split("."):
                found = [n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part]
                require(len(found) == 1, "ENTRYPOINT_SYMBOL_MISSING")
                nodes = found[0].body
    return len(by_path)


def verify_history(root, docs, *, read_file=None):
    """比较原历史映射，拒绝串候选/错 Job；可注入经过授权的固定版本来源读取器。"""
    read_file = read_file or (lambda path: safe_file(root, path).read_bytes())
    directory = "reports/05-baseline/20261110-release-verification/"
    summary = json.loads(read_file(directory + "summary.json"))
    objects = json.loads(read_file(directory + "business-objects.json"))
    by_label = {x["label"]: x for x in objects["cases"]}
    for capability in docs[FILES[2]]["capabilities"]:
        qualification = capability.get("qualification", {})
        require(qualification.get("applies_to_new_candidate") is False, "QUALIFICATION_SCOPE_REQUIRED")
        report = json.loads(read_file(qualification["report_ref"]))
        if report.get("target_candidate"):
            require(qualification.get("candidate_ref") == report["target_candidate"], "QUALIFICATION_CANDIDATE_MISMATCH")
        else:
            require(qualification.get("candidate_ref") is None
                    and qualification.get("candidate_binding_status") == "NOT_EXPLICIT_IN_REFERENCED_REPORT",
                    "QUALIFICATION_CANDIDATE_UNPROVEN")
    count = 0
    for scenario in docs[FILES[3]]["scenarios"]:
        require(scenario["id"] in by_label, "HISTORY_SCENARIO_UNKNOWN")
        actual = {x["stage"]: x for x in by_label[scenario["id"]]["actual_stages"]}
        for stage in scenario["stages"]:
            history = stage.get("history", {})
            require(history.get("candidate_commit") == summary["target_candidate"], "HISTORY_CANDIDATE_MISMATCH")
            if stage["id"] in actual:
                record = actual[stage["id"]]
                require(history.get("execution_status") == "DONE" and all(history.get(k) == record[k]
                    for k in ("job_id", "snapshot_sha256", "eligibility", "proposed_action")), "HISTORY_STAGE_MISMATCH")
                count += 1
            else:
                require(history.get("job_id") is None and history.get("execution_status") == "NOT_ADMITTED"
                        and by_label[scenario["id"]]["execution_errors"], "HISTORY_UNOBSERVED_STAGE_MISMATCH")
    require(count == summary["observed_stages"], "HISTORY_OBSERVATION_COUNT_MISMATCH")
    return count


def validate(root=ROOT, pack=PACK):
    docs = {name: parse_yaml(safe_file(root, str(pack / name)).read_bytes()) for name in FILES}
    counts = validate_documents(docs)
    counts["pinned_source_files"] = verify_provenance(root, pack, docs)
    counts["historical_observed_stages"] = verify_history(root, docs)
    return {"status": "VALID_DRAFT", "counts": counts, "dispatch_authorized": False,
            "enterprise_admission": "BLOCKED_PENDING_REGISTRATION", "current_candidate_result": "NOT_RUN"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.root.resolve()), ensure_ascii=False, indent=2))
        return 0
    except (CatalogError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        error = sys.exception()
        print(json.dumps({"status": "INVALID", "error_code": str(error) if isinstance(error, CatalogError)
                          else "CATALOG_VALIDATION_UNCONFIRMED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
