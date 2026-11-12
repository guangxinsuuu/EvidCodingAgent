"""发布任务的只读材料装配；不执行候选，不调用模型，不授予实验派发权。

调用链：CLI / 可信本地操作员 → assemble_release → 固定 CatalogReader / Git blob
→ 精简目录和不可变材料 bundle → TS Runtime 登记并生成权威摘要 → Agent 的 READ。
旧 task_assembler 的 PLAN_ONLY 权限保持不变；本模块也不签发模型出口许可。
候选源码、diff 和目录正文均为待分析数据，其中的指令不能成为执行权限。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .catalog_reader import CATALOG_COMMIT, load_catalog
from .reference_resolver import LocalGitReader, MaterialError, ReadScope, _registered_path
from .validate_catalog import ROOT


MAX_CHANGES = 64
MAX_MATERIAL_BYTES = 96 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024
RULES_PATH = "business-source-pack/v1/policies/rules.md"
# 这是可信装配器的本地产品范围，不接受模型提交的新前缀或任意绝对路径。
SOURCE_PREFIXES = (
    "services/genai-support-sut/src/", "services/genai-support-sut/migrations/",
    "services/genai-support-sut/prompts/", "services/genai-support-sut/config/",
    "services/commercial-source/src/", "services/commercial-source/migrations/",
)
SOURCE_FILES = frozenset({
    "services/genai-support-sut/pyproject.toml", "services/genai-support-sut/uv.lock",
    "services/commercial-source/pyproject.toml", "services/commercial-source/uv.lock",
    "config/active-business.json", "config/genai-support-source-policy.json",
    "packages/contracts/src/support.ts", "sut/genai-support/upstream.lock.json",
})
# 提供可导航的真实调用路径，而非启动时把所有源码塞给模型。只有被选 READ 才进入上下文。
RELATED_SOURCE_FILES = (
    "services/genai-support-sut/src/genai_support/business_tools.py",
    "services/genai-support-sut/src/genai_support/integrations/workflow.py",
    "services/genai-support-sut/src/genai_support/integrations/commercial.py",
    "services/genai-support-sut/src/genai_support/integrations/lago.py",
    "services/genai-support-sut/src/genai_support/integrations/persistence.py",
    "services/genai-support-sut/src/genai_support/model_gateway.py",
)
_SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:ghp_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})\b"
)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise MaterialError(code)


def _commit(git: LocalGitReader, value: str) -> None:
    """只接受完整 commit 对象；分支名、HEAD、tag 或 blob 不能代替候选身份。"""
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None,
             "RELEASE_REVISION_INVALID")
    _require(git._git("cat-file", "-t", value).strip() == b"commit", "RELEASE_REVISION_INVALID")


def _category(path: str) -> str:
    if path.startswith("business-source-pack/"):
        return "PROPOSED_BUSINESS_RULE"
    if "/migrations/" in path:
        return "MIGRATION"
    if "/prompts/" in path or "prompt" in Path(path).name.lower():
        return "PROMPT"
    if Path(path).name in {"uv.lock", "pyproject.toml", "package.json", "pnpm-lock.yaml"}:
        return "DEPENDENCY"
    if path.startswith("config/") or "/config/" in path or Path(path).suffix in {".json", ".yaml", ".yml", ".toml"}:
        return "CONFIG_OR_SCHEMA"
    return "SOURCE" if any(path.startswith(prefix) for prefix in SOURCE_PREFIXES) else "OUTSIDE_PRODUCT_PROFILE"


def _allowed_source(path: str) -> bool:
    return path in SOURCE_FILES or any(path.startswith(prefix) for prefix in SOURCE_PREFIXES)


def _source_mode(git: LocalGitReader, commit: str, path: str) -> str:
    """Git 文件权限也是变化事实；纯 chmod 不能被相同的文本摘要掩盖。"""
    entry = git._git("ls-tree", "-z", commit, "--", path).split(b"\t", 1)[0].split()
    _require(len(entry) == 3, "SOURCE_READ_FAILED")
    return entry[0].decode("ascii")


def _material(material_ref: str, kind: str, title: str, commit: str, path: str, raw: bytes) -> dict:
    """摘要覆盖实际投影字节；差异/规则片段的摘要不冒充完整 Git blob 摘要。"""
    _require(len(raw) <= MAX_MATERIAL_BYTES, "RELEASE_MATERIAL_TOO_LARGE")
    _require(not _SECRET.search(raw), "MATERIAL_SECRET_CONTENT_FORBIDDEN")
    _require(b"\0" not in raw, "RELEASE_BINARY_MATERIAL_UNSUPPORTED")
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        raise MaterialError("MATERIAL_ENCODING_UNSUPPORTED") from None
    return {"material_ref": material_ref, "kind": kind, "title": title,
            "git_commit": commit, "path": path, "sha256": hashlib.sha256(raw).hexdigest(), "content": content}


def _changes(git: LocalGitReader, baseline: str, candidate: str) -> list[tuple[str, str]]:
    """完整列举变更，再决定哪些可读；禁用 textconv、external diff、rename 猜测。

    rename 保留为明确的删除和新增。禁止候选 Git 配置在计算 diff 时执行外部程序。
    私有路径的变更直接拒绝，既不读正文，也不把路径当作模型可读材料登记。
    """
    raw = git._git("diff", "--name-status", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
                   baseline, candidate, "--")
    _require(len(raw) <= 32768, "RELEASE_CHANGE_SET_TOO_LARGE")
    parts = raw.rstrip(b"\0").split(b"\0") if raw else []
    _require(len(parts) % 2 == 0 and len(parts) // 2 <= MAX_CHANGES, "RELEASE_CHANGE_SET_TOO_LARGE")
    result = []
    for index in range(0, len(parts), 2):
        try:
            status, path = parts[index].decode("ascii"), parts[index + 1].decode("utf-8")
        except UnicodeError:
            raise MaterialError("RELEASE_CHANGE_PATH_INVALID") from None
        _require(status in {"A", "M", "D", "T"}, "RELEASE_CHANGE_STATUS_UNSUPPORTED")
        _registered_path(path)
        result.append((status, path))
    return sorted(result, key=lambda row: row[1])


def _catalog_projection(reader, requirement_refs: list[str], materials: list[dict]) -> dict:
    """只按白名单复制登记字段，不在完整资产上搜索后再删除几个已知泄漏字段。

    history、known_finding_codes、Oracle 预期、归因备注和路由提示不进入此投影。
    原目录保留完整审计内容；规划器只获得本任务可用的安全导航视图。
    """
    requirements = []
    full = reader.resolver._read_verified_bytes(RULES_PATH)
    full_text = full.decode("utf-8")
    for identifier in requirement_refs:
        requirement = reader.get_asset("requirement", identifier)
        _require(requirement["provenance_refs"] == [RULES_PATH], "RELEASE_REQUIREMENT_SOURCE_UNSUPPORTED")
        section = requirement["section"]
        match = re.search(rf"(?ms)^## {re.escape(section)}(?:\s[^\n]*)?\n.*?(?=^## |\Z)", full_text)
        _require(match is not None, "RELEASE_REQUIREMENT_SECTION_MISSING")
        material_ref = "rule:" + identifier
        title = match.group().splitlines()[0].removeprefix("## ").strip()
        materials.append(_material(material_ref, "REGISTERED_RULE_EXCERPT", title,
                                   reader.catalog_basis_commit, RULES_PATH, match.group().encode("utf-8")))
        requirements.append({"id": identifier, "title": title, "section": section, "source_ref": requirement["source_ref"],
                             "material_ref": material_ref, "review_status": requirement["review_status"]})
    capabilities = [{key: row[key] for key in ("id", "name", "kind", "requirement_refs", "dispatch_enabled")}
                    for row in reader.assets("capability")]
    scenarios = [{"id": row["id"], "name": row["name"], "requirement_refs": row["requirement_refs"],
                  "stages": [{"stage_ref": stage["stage_ref"], "depends_on": stage["depends_on"]}
                             for stage in row["stages"]]}
                 for row in reader.assets("scenario")]
    return {
        "status": "DRAFT", "scope": "LOCAL_SYNTHETIC", "dispatch_authorized": False,
        "requirements": requirements, "capabilities": capabilities, "scenarios": scenarios,
        "sources": [{key: row[key] for key in ("id", "kind", "maintainer_role", "authority_scope")}
                    for row in reader.assets("source") if row["kind"] != "HISTORICAL_EVIDENCE"],
        "roles": [{key: row[key] for key in ("id", "responsibility", "assignment_status")}
                  for row in reader.assets("role")],
        "flows": [{key: row[key] for key in ("id", "from", "to", "source_refs", "owner_role")}
                  for row in reader.assets("data_flow") if "historical_reports" not in row["source_refs"]],
        "projection": "ALLOWLIST_NO_HISTORICAL_ANSWERS",
    }


def assemble_release(root: Path, baseline_sha: str, candidate_sha: str, goal: str,
                     requirement_refs: list[str] | None = None, *, candidate_root: Path | None = None,
                     catalog_commit: str = CATALOG_COMMIT,
                     input_provenance: str = "LOCAL_COMMITTED_RELEASE") -> dict:
    """冻结 B/C、真实差异及可读材料，返回尚未运行或获准派发的发布任务。

    root 是可信目录所在仓库；candidate_root 仅供操作员给出隔离合成 Git 版本对。
    两者不同时必须显式标记 CALIBRATION_SYNTHETIC，不会创建真实 PR 或改变产品。
    预算、模型地址、凭据和出站许可都不在 bundle 中，由 TS Runtime 另行准入。
    对读取失败、敏感内容、字节超限采取拒绝；范围外变更保留明确未分析缺口。
    """
    root = Path(root).resolve()
    candidate_root = Path(candidate_root).resolve() if candidate_root is not None else root
    _require(input_provenance in {"LOCAL_COMMITTED_RELEASE", "CALIBRATION_SYNTHETIC"}, "RELEASE_PROVENANCE_INVALID")
    _require(candidate_root == root or input_provenance == "CALIBRATION_SYNTHETIC", "RELEASE_SYNTHETIC_LABEL_REQUIRED")
    _require(isinstance(goal, str) and 1 <= len(goal.strip()) <= 2000, "RELEASE_GOAL_INVALID")
    refs = ["R1", "R2", "R3", "R4", "R5", "R6"] if requirement_refs is None else requirement_refs
    _require(isinstance(refs, list) and 0 < len(refs) <= 16 and all(isinstance(ref, str) for ref in refs)
             and len(set(refs)) == len(refs), "RELEASE_REQUIREMENTS_INVALID")
    # 只允许旧 Resolver 读 rules 原件；完整历史验证不是此规划上下文装配的副作用。
    scope = ReadScope(principal_id="local-release-assembler", purpose="LOCAL_PLAN",
                      allowed_materials=frozenset({RULES_PATH}), model_egress_allowed=False)
    reader = load_catalog(root, catalog_commit, scope)
    git = LocalGitReader(candidate_root)
    _commit(git, baseline_sha)
    _commit(git, candidate_sha)
    _require(git._git("merge-base", baseline_sha, candidate_sha).strip().decode("ascii") == baseline_sha,
             "RELEASE_BASELINE_NOT_ANCESTOR")
    materials: list[dict] = []
    catalog = _catalog_projection(reader, refs, materials)
    changes, gaps = [], []
    raw_changes = _changes(git, baseline_sha, candidate_sha)
    for status, path in raw_changes:
        # 历史报告的文件名也可能直接泄漏故障答案。保留不可逆目录项身份与未分析事实；
        # 操作员可用冻结 B/C 审计原始 Git 清单，模型不能通过名称检索取得旧结论。
        projected_path = "excluded-history:" + hashlib.sha256(path.encode("utf-8")).hexdigest() if path.startswith("reports/") else path
        change = {"path": projected_path, "status": status, "category": _category(path), "analysis_status": "MATERIAL_AVAILABLE"}
        if not _allowed_source(path):
            change["analysis_status"] = "NOT_ANALYZED_OUTSIDE_PRODUCT_PROFILE"
            gaps.append({"code": "CHANGE_OUTSIDE_PRODUCT_PROFILE", "detail": projected_path})
            changes.append(change)
            continue
        old = b"" if status == "A" else git.read(baseline_sha, path)
        new = b"" if status == "D" else git.read(candidate_sha, path)
        for label, commit, raw in (("baseline", baseline_sha, old), ("candidate", candidate_sha, new)):
            if (label == "baseline" and status == "A") or (label == "candidate" and status == "D"):
                continue
            ref = label + ":" + path
            materials.append(_material(ref, label.upper() + "_SOURCE", path, commit, path, raw))
            change[label + "_material_ref"] = ref
            change[label + "_mode"] = _source_mode(git, commit, path)
        # utf8、大小和敏感内容已由 _material 校验；diff 不调用任何候选代码或 filter。
        diff = "".join(difflib.unified_diff(old.decode("utf-8").splitlines(keepends=True),
                                         new.decode("utf-8").splitlines(keepends=True),
                                         fromfile="baseline/" + path, tofile="candidate/" + path, n=3))
        if change.get("baseline_mode") != change.get("candidate_mode"):
            diff = (f"old mode {change.get('baseline_mode', 'absent')}\n"
                    f"new mode {change.get('candidate_mode', 'absent')}\n" + diff)
        ref = "diff:" + path
        materials.append(_material(ref, "UNTRUSTED_DIFF", path + " 的版本差异", candidate_sha, path, diff.encode("utf-8")))
        change["diff_material_ref"] = ref
        changes.append(change)
    existing = {material["material_ref"] for material in materials}
    for path in RELATED_SOURCE_FILES:
        ref = "candidate:" + path
        if ref in existing:
            continue
        try:
            raw = git.read(candidate_sha, path)
        except MaterialError as error:
            if error.code == "SOURCE_FILE_MISSING":
                continue
            raise
        materials.append(_material(ref, "CANDIDATE_SOURCE", path, candidate_sha, path, raw))
    if not changes:
        gaps.append({"code": "NO_VERSION_CHANGES", "detail": "版本对没有变化；尚未独立验证，不能据此签发 NO_CHANGE 或 PASS。"})
    gaps.extend([
        {"code": "EXECUTION_NOT_IMPLEMENTED", "detail": "V0.1 仅允许受控阅读及检查提案，目录未授权业务实验派发。"},
        {"code": "LOCAL_RULES_PENDING_REVIEW", "detail": "R1–R6 是本地合成校准规则，不能代替企业确认的合同或发布批准。"},
        {"code": "NO_CURRENT_CANDIDATE_COVERAGE", "detail": "本次尚未执行业务实验；历史结果不继承为当前候选覆盖。"},
    ])
    bundle = {
        "schema_version": 1, "task_mode": "RELEASE_VERIFICATION", "scope": "LOCAL_SYNTHETIC_PLANNING",
        "input_provenance": input_provenance, "goal": goal.strip(), "baseline_sha": baseline_sha,
        "candidate_sha": candidate_sha, "catalog_commit": reader.catalog_commit,
        "catalog_basis_commit": reader.catalog_basis_commit, "requirement_refs": list(refs),
        "changes": changes, "materials": materials, "catalog": catalog, "gaps": gaps,
        "current_candidate_coverage": [], "evaluation_status": "NOT_RUN",
    }
    _require(len(json.dumps(bundle, ensure_ascii=False).encode("utf-8")) <= MAX_BUNDLE_BYTES,
             "RELEASE_BUNDLE_TOO_LARGE")
    return bundle


def main(argv: list[str] | None = None) -> int:
    """只写操作员指定的新文件；拒绝覆盖，且不将完整正文打印到通用终端日志。"""
    parser = argparse.ArgumentParser(description="冻结真实 Git 版本对及受控阅读材料；不运行实验")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--requirement", action="append")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--input-provenance", choices=["LOCAL_COMMITTED_RELEASE", "CALIBRATION_SYNTHETIC"], default="LOCAL_COMMITTED_RELEASE")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        bundle = assemble_release(args.root, args.baseline, args.candidate, args.goal, args.requirement,
                                  candidate_root=args.candidate_root, input_provenance=args.input_provenance)
        # 仅输出 bundle，不能借 CLI 覆盖冻结 docs/README 或已有实验材料。
        resolved = args.output.resolve()
        _require("docs" not in resolved.parts and resolved.name.lower() != "readme.md", "RELEASE_OUTPUT_FORBIDDEN")
        # 在创建瞬间即设为私有；不能先按默认 umask 写全文，随后才 chmod。
        descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    except (MaterialError, OSError) as error:
        print(json.dumps({"status": "REJECTED", "error_code": getattr(error, "code", "RELEASE_OUTPUT_FAILED")}))
        return 1
    print(json.dumps({"status": "ASSEMBLED_NOT_EXECUTED", "changes": len(bundle["changes"]),
                      "materials": len(bundle["materials"]), "input_provenance": bundle["input_provenance"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
