"""固定版本目录消费：CLI → load_catalog → 原校验器 → 索引/受控 ReferenceResolver。

这是平台只读组件。索引描述入口及其关系，不导入入口，不连接业务系统，不产生执行许可。
完整本地读取范围可复用全部既有校验；正文权限不足时只报告元数据校验，不能假称已验全文。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .reference_resolver import LocalGitReader, MaterialError, ReadScope, ReferenceResolver
from .validate_catalog import (
    FILES, PACK, ROOT, CatalogError, parse_yaml, referenced_files,
    validate_documents, verify_history, verify_provenance, walk,
)


CATALOG_COMMIT = "6b1d4815b4178c070f4442edfc2b5606069a3f11"
GAP_PATH = "reports/06-source-catalog/20261111-local-draft/gaps.json"
ASSET_TABLES = {
    "role": ("ownership.yaml", "roles"),
    "source": ("sources.yaml", "sources"),
    "requirement": ("sources.yaml", "requirements"),
    "capability": ("test-catalog.yaml", "capabilities"),
    "scenario": ("cross-team-scenarios.yaml", "scenarios"),
    "data_flow": ("data-flows.yaml", "flows"),
    "retention_policy": ("data-flows.yaml", "retention_policies"),
}


def _check(condition, code):
    if not condition:
        raise MaterialError(code)


def local_plan_scope(root: Path = ROOT, catalog_commit: str = CATALOG_COMMIT) -> ReadScope:
    """可信本地 CLI 的范围构造器；不是模型工具，不能由 task JSON 覆盖。

本轮用户授权读取这个本地实验仓库的登记资料，因此允许这 66 项的本地阅读。
这里没有企业身份授权、外部取数或模型出口；将来服务接入必须另提供实际身份策略。
"""
    git = LocalGitReader(root)
    manifest = parse_yaml(git.read(catalog_commit, str(PACK / "provenance.yaml")))
    return ReadScope(principal_id="local-project-operator", purpose="LOCAL_PLAN",
                     allowed_materials=frozenset(row["path"] for row in manifest["files"]),
                     model_egress_allowed=False)


class CatalogReader:
    """保持原始 DRAFT 状态的目录快照；所有公开返回值都与内部索引隔离。"""

    def __init__(self, root: Path, catalog_commit: str, scope: ReadScope):
        _check(isinstance(scope, ReadScope) and scope.allow_metadata, "ACCESS_DENIED")
        _check(scope.purpose == "LOCAL_PLAN", "MATERIAL_PURPOSE_DENIED")
        self.catalog_commit, self.scope = catalog_commit, scope
        self._git = LocalGitReader(root)
        catalog_bytes = {name: self._git.read(catalog_commit, str(PACK / name)) for name in FILES}
        docs = {name: parse_yaml(raw) for name, raw in catalog_bytes.items()}
        counts = validate_documents(docs)
        manifest = parse_yaml(self._git.read(catalog_commit, str(PACK / "provenance.yaml")))
        self.catalog_basis_commit = docs[FILES[0]]["catalog_basis_commit"]
        _check(manifest.get("catalog_basis_commit") == self.catalog_basis_commit, "PROVENANCE_BASIS_MISMATCH")
        self.resolver = ReferenceResolver(self._git, manifest.get("files"), scope)
        records = {row["path"]: row for row in manifest["files"]}
        _check(set(records) == referenced_files(docs), "PROVENANCE_CLOSURE_MISMATCH")
        _check(all(row["git_commit"] == self.catalog_basis_commit for row in records.values()),
               "PROVENANCE_REVISION_INVALID")
        hashes = manifest.get("catalog_documents", {})
        _check(set(hashes) == set(FILES), "CATALOG_DIGEST_SET_INVALID")
        _check(all(hashlib.sha256(catalog_bytes[name]).hexdigest() == hashes[name] for name in FILES),
               "CATALOG_CONTENT_DRIFT")
        # 验证器的固定版本回调替代工作树读取；受限身份不能借完整校验偷读正文。
        full_read = scope.allow_content and set(records) <= scope.allowed_materials
        if full_read:
            def registered_read(commit, path):
                # 同一来源回调仍走 Resolver 权限与正文检查；不因完整校验而开放旁路。
                _check(records[path]["git_commit"] == commit, "PROVENANCE_REVISION_INVALID")
                return self.resolver._read_verified_bytes(path)
            counts["pinned_source_files"] = verify_provenance(
                root, PACK, docs, registered_reader=registered_read,
                catalog_reader=lambda path: self._git.read(catalog_commit, path))
            counts["historical_observed_stages"] = verify_history(
                root, docs, read_file=self.resolver._read_verified_bytes)
        self.validation = {
            "status": "VALID_DRAFT" if full_read else "VALID_DRAFT_METADATA_ONLY",
            "counts": counts,
            "material_integrity": "SHA256_VERIFIED" if full_read else "NOT_FULLY_READ_SCOPE_RESTRICTED",
            "dispatch_authorized": False,
            "enterprise_admission": "BLOCKED_PENDING_REGISTRATION",
            "current_candidate_result": "NOT_RUN",
        }
        self._indices = {kind: {row["id"]: row for row in docs[filename][key]}
                         for kind, (filename, key) in ASSET_TABLES.items()}
        self._indices["stage"] = {stage["stage_ref"]: stage
                                  for scenario in self._indices["scenario"].values()
                                  for stage in scenario["stages"]}

    def _metadata_access(self):
        _check(self.scope.allow_metadata, "ACCESS_DENIED")

    def get_asset(self, kind: str, asset_id: str) -> dict:
        """按准确类型/ID 取副本；未知类型或未登记对象不会变成空结果。"""
        self._metadata_access()
        _check(isinstance(kind, str) and kind in self._indices, "ASSET_KIND_INVALID")
        _check(isinstance(asset_id, str) and asset_id in self._indices[kind], "ASSET_NOT_FOUND")
        return deepcopy(self._indices[kind][asset_id])

    def assets(self, kind: str) -> list[dict]:
        self._metadata_access()
        _check(isinstance(kind, str) and kind in self._indices, "ASSET_KIND_INVALID")
        return deepcopy(list(self._indices[kind].values()))

    def material_identity(self, material_ref: str) -> dict:
        """为仅引用的材料附固定版本与摘要；不因装配需要而读取未授权正文。"""
        self._metadata_access()
        record = self.resolver._record(material_ref)
        if material_ref not in self.scope.allowed_materials:
            return {"material_ref": material_ref, "status": "DENIED",
                    "error_code": "MATERIAL_ACCESS_DENIED", "content_projected": False}
        return {"material_ref": material_ref, "git_commit": record["git_commit"],
                "sha256": record["sha256"], "digest_algorithm": record["digest_algorithm"],
                "status": "REFERENCE_ONLY", "content_projected": False}

    def describe_asset(self, asset_id: str, kind: str | None = None) -> dict:
        """展开登记事实和关系；历史资格保留自己的版本，不转为当前可调用能力。"""
        self._metadata_access()
        _check(isinstance(asset_id, str), "ASSET_NOT_FOUND")
        if kind is not None:
            asset = self.get_asset(kind, asset_id)
        else:
            kinds = [key for key, table in self._indices.items() if asset_id in table]
            _check(bool(kinds), "ASSET_NOT_FOUND")
            _check(len(kinds) == 1, "ASSET_KIND_REQUIRED")
            kind = kinds[0]
            asset = self.get_asset(kind, asset_id)
        relations = []
        for node in walk(asset):
            for key, value in node.items():
                if key.endswith(("_ref", "_refs", "_role", "_roles")) or key == "depends_on":
                    relations.append({"field": key, "value": deepcopy(value)})
        return {"status": "FOUND", "catalog_commit": self.catalog_commit,
                "catalog_basis_commit": self.catalog_basis_commit, "catalog_status": "DRAFT",
                "asset_kind": kind, "id": asset_id, "asset": asset, "relationships": relations,
                "dispatch_authorized": False}

    def _scenario_relationships(self, scenario_id: str) -> set[tuple[str, str]]:
        """只沿显式引用和重建依赖找候选关联；不按名字声称已证明覆盖。"""
        scenario = self.get_asset("scenario", scenario_id)
        related = {("scenario", scenario_id)}
        pending = [scenario]
        ref_kinds = {"source_ref": "source", "source_refs": "source",
                     "requirement_refs": "requirement", "capability_ref": "capability",
                     "capability_refs": "capability", "rebuild_capability_refs": "capability",
                     "depends_on": "stage"}
        while pending:
            asset = pending.pop()
            for node in walk(asset):
                if "stage_ref" in node:
                    related.add(("stage", node["stage_ref"]))
                for key, value in node.items():
                    kind = "role" if key.endswith(("_role", "_roles")) else ref_kinds.get(key)
                    if kind:
                        for ref in value if isinstance(value, list) else [value]:
                            pair = (kind, ref)
                            if pair not in related:
                                related.add(pair)
                                pending.append(self.get_asset(kind, ref))
        return related

    def search_catalog(self, query: str | None = None, *, kind: str | None = None,
                       maintainer_role: str | None = None, scenario_id: str | None = None,
                       capability_kind: str | None = None, business_boundary: str | None = None) -> dict:
        """AND 组合静态筛选；返回匹配依据和未证明覆盖标记，不把命中当验收结论。"""
        self._metadata_access()
        for value in (query, business_boundary):
            _check(value is None or isinstance(value, str) and 0 < len(value.strip()) <= 200, "SEARCH_FILTER_INVALID")
        _check(kind is None or isinstance(kind, str) and kind in self._indices, "ASSET_KIND_INVALID")
        if maintainer_role is not None:
            self.get_asset("role", maintainer_role)
        if capability_kind is not None:
            _check(isinstance(capability_kind, str) and capability_kind in
                   {row["kind"] for row in self._indices["capability"].values()}, "SEARCH_FILTER_INVALID")
        related = self._scenario_relationships(scenario_id) if scenario_id is not None else None
        matches = []
        for asset_kind, table in self._indices.items():
            if kind is not None and kind != asset_kind:
                continue
            for asset_id, row in table.items():
                reasons = ["registered_asset"]
                if related is not None:
                    if (asset_kind, asset_id) not in related:
                        continue
                    reasons.append("explicit_relationship:" + scenario_id)
                if maintainer_role is not None:
                    if row.get("maintainer_role") != maintainer_role:
                        continue
                    reasons.append("maintainer_role:" + maintainer_role)
                if capability_kind is not None:
                    if asset_kind != "capability" or row.get("kind") != capability_kind:
                        continue
                    reasons.append("capability_kind:" + capability_kind)
                searchable = json.dumps(row, ensure_ascii=False).casefold()
                if any(value is not None and value.casefold() not in searchable for value in (query, business_boundary)):
                    continue
                reasons.extend(label + ":" + value for label, value in
                               (("text_match", query), ("business_text_match", business_boundary)) if value is not None)
                matches.append({"asset_kind": asset_kind, "id": asset_id, "name": row.get("name"),
                                "match_reasons": reasons, "coverage_status": "NOT_PROVEN_BY_SEARCH"})
        return {"status": "MATCHES" if matches else "NO_MATCH", "catalog_commit": self.catalog_commit,
                "matches": matches, "dispatch_authorized": False}

    def gap_records(self) -> dict:
        """固定目录提交中的补充元数据，不伪装成 provenance 的第 67 项历史材料。"""
        self._metadata_access()
        raw = self._git.read(self.catalog_commit, GAP_PATH)
        try:
            gaps = json.loads(raw)
            _check(isinstance(gaps, dict) and isinstance(gaps.get("gaps"), list)
                   and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in gaps["gaps"]),
                   "GAP_DOCUMENT_INVALID")
        except (ValueError, UnicodeError):
            raise MaterialError("GAP_DOCUMENT_INVALID") from None
        return {"source": {"path": GAP_PATH, "git_commit": self.catalog_commit,
                           "sha256": hashlib.sha256(raw).hexdigest()}, "gaps": deepcopy(gaps["gaps"])}


def load_catalog(root: Path, catalog_commit: str, scope: ReadScope) -> CatalogReader:
    """工厂入口：任何读取失败均保留稳定错误码，不把来源失败解释为目录无匹配。"""
    try:
        return CatalogReader(root, catalog_commit, scope)
    except CatalogError as error:
        raise MaterialError(str(error)) from None
