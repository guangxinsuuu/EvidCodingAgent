"""发布任务的只读材料装配；不执行候选，不调用模型，不授予实验派发权。

调用链：CLI / 可信本地操作员 → assemble_release 或 assemble_review_request
→ 固定 CatalogReader / Git blob
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
PLANNING_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "protocols/verify-agent/capabilities/commercial-decision.v1.json"
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
    if hasattr(git, "physical_path"):
        path = git.physical_path(path)
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


def _catalog_projection(reader, requirement_refs: list[str], materials: list[dict], *,
                        retain_missing_requirements: bool = False) -> dict:
    """只按白名单复制登记字段，不在完整资产上搜索后再删除几个已知泄漏字段。

    history、known_finding_codes、Oracle 预期、归因备注和路由提示不进入此投影。
    原目录保留完整审计内容；规划器只获得本任务可用的安全导航视图。
    """
    requirements = []
    full = reader.resolver._read_verified_bytes(RULES_PATH)
    full_text = full.decode("utf-8")
    registered = {row["id"] for row in reader.assets("requirement")}
    for identifier in requirement_refs:
        # 新请求可登记尚未接入的要求；缺失时不伪造规则，也不退回 R1–R6。
        # 旧 v1 仍按原语义拒绝未知 ID。真实来源完整性失败仍直接拒绝。
        if retain_missing_requirements and identifier not in registered:
            continue
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
        # 仅提供已登记字段导航；不是运行结果，也不授予采集/任意网络权限。
        "sources": [{**{key: row[key] for key in ("id", "kind", "maintainer_role", "authority_scope")},
                     "allowed_fields": row.get("collection", {}).get("allowed_fields", [])}
                    for row in reader.assets("source") if row["kind"] != "HISTORICAL_EVIDENCE"],
        "roles": [{key: row[key] for key in ("id", "responsibility", "assignment_status")}
                  for row in reader.assets("role")],
        "flows": [{key: row[key] for key in ("id", "from", "to", "source_refs", "owner_role")}
                  for row in reader.assets("data_flow") if "historical_reports" not in row["source_refs"]],
        "projection": "ALLOWLIST_NO_HISTORICAL_ANSWERS",
    }


def _planning_contract_overlay(catalog: dict) -> list[dict]:
    """受信装配器叠加纯fixture能力导航；不冒充旧目录commit中已登记的真实实验能力。

    读取安装侧唯一文件，不从 candidate_root 或模型给的路径取合同；TS Runtime 仍以
    自己安装的合同逐字数据核验。完整fixture只用于绑定，模型视图由Runtime另行限界。
    """
    _require(not PLANNING_CONTRACT_PATH.is_symlink(), "PLANNING_CONTRACT_SYMLINK")
    contract = json.loads(PLANNING_CONTRACT_PATH.read_text(encoding="utf-8"))
    _require(contract.get("id") == "commercial-decision.v1" and contract.get("execution_mode") == "PURE_FIXTURE"
             and contract.get("dispatch_enabled") is False, "PLANNING_CONTRACT_IDENTITY_INVALID")
    fixture = contract["fixture"]
    raw = json.dumps(fixture["context"], sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    _require(fixture.get("hash_encoding") == "canonical-json-sort-keys-utf8-v1"
             and hashlib.sha256(raw).hexdigest() == fixture.get("sha256"), "PLANNING_FIXTURE_DIGEST_MISMATCH")
    origin = "LOCAL_TRUSTED_BINDING_CONTRACT_OVERLAY"
    catalog["capabilities"].append({"id": contract["capability_ref"], "name": "商业判断纯fixture探针",
        "kind": "PURE_FIXTURE_PROBE", "parent_capability_ref": contract["parent_capability_ref"],
        "requirement_refs": contract["supported_requirements"], "dispatch_enabled": False,
        "planning_contract_ref": contract["id"], "catalog_origin": origin})
    catalog["scenarios"].append({"id": contract["scenario_ref"], "name": "隔离商业函数的合成输入比较",
        "requirement_refs": contract["supported_requirements"], "execution_mode": "PURE_FIXTURE",
        "stages": [{"stage_ref": contract["stage_ref"], "depends_on": [], "execution_mode": "PURE_FIXTURE"}],
        "catalog_origin": origin, "live_stage_prerequisites_satisfied": False})
    return [contract]


def _assemble_release(root: Path, baseline_sha: str, candidate_sha: str, goal: str,
                      requirement_refs: list[str] | None = None, *, candidate_root: Path | None = None,
                      catalog_commit: str = CATALOG_COMMIT,
                      input_provenance: str = "LOCAL_COMMITTED_RELEASE",
                      retain_missing_requirements: bool = False, product_id: str | None = None) -> dict:
    """冻结 B/C、真实差异及可读材料，返回尚未运行或获准派发的发布任务。

    root 是可信目录所在仓库。旧 candidate_root 入口仍只接受隔离合成版本对；
    product_id 则走安装侧独立产品登记，核对 origin/仓库根并保存真实物理路径。
    两个入口的来源资格不同，不能靠换标签把旧校准包升级为外部产品包。
    预算、模型地址、凭据和出站许可都不在 bundle 中，由 TS Runtime 另行准入。
    对读取失败、敏感内容、字节超限采取拒绝；范围外变更保留明确未分析缺口。
    """
    root = Path(root).resolve()
    candidate_root = Path(candidate_root).resolve() if candidate_root is not None else root
    external = product_id is not None
    _require(input_provenance in ({"REGISTERED_EXTERNAL_PRODUCT"} if external else
             {"LOCAL_COMMITTED_RELEASE", "CALIBRATION_SYNTHETIC"}), "RELEASE_PROVENANCE_INVALID")
    _require(external or candidate_root == root or input_provenance == "CALIBRATION_SYNTHETIC", "RELEASE_SYNTHETIC_LABEL_REQUIRED")
    _require(isinstance(goal, str) and 1 <= len(goal.strip()) <= 2000, "RELEASE_GOAL_INVALID")
    refs = ["R1", "R2", "R3", "R4", "R5", "R6"] if requirement_refs is None else requirement_refs
    _require(isinstance(refs, list) and (bool(refs) or retain_missing_requirements) and len(refs) <= 16
             and all(isinstance(ref, str) for ref in refs)
             and len(set(refs)) == len(refs), "RELEASE_REQUIREMENTS_INVALID")
    # 只允许旧 Resolver 读 rules 原件；完整历史验证不是此规划上下文装配的副作用。
    scope = ReadScope(principal_id="local-release-assembler", purpose="LOCAL_PLAN",
                      allowed_materials=frozenset({RULES_PATH}), model_egress_allowed=False)
    reader = load_catalog(root, catalog_commit, scope)
    from .product_repository import ProductGitReader
    git = ProductGitReader(candidate_root, product_id=product_id, authority_root=root) if external else LocalGitReader(candidate_root)
    _commit(git, baseline_sha)
    _commit(git, candidate_sha)
    _require(git._git("merge-base", baseline_sha, candidate_sha).strip().decode("ascii") == baseline_sha,
             "RELEASE_BASELINE_NOT_ANCESTOR")
    materials: list[dict] = []
    catalog = _catalog_projection(reader, refs, materials, retain_missing_requirements=retain_missing_requirements)
    planning_contracts = _planning_contract_overlay(catalog)
    changes, gaps = [], []
    raw_changes = _changes(git, baseline_sha, candidate_sha)
    for status, path in raw_changes:
        # 新产品仓库的真实路径与历史逻辑名同时保留；这里只转换命名，不复制源码入 EGAP。
        physical_path = path
        if external:
            path = git.logical_path(path)
        # 历史报告的文件名也可能直接泄漏故障答案。保留不可逆目录项身份与未分析事实；
        # 操作员可用冻结 B/C 审计原始 Git 清单，模型不能通过名称检索取得旧结论。
        history = physical_path.startswith("reports/")
        projected_path = "excluded-history:" + hashlib.sha256(path.encode("utf-8")).hexdigest() if history else path
        change = {"path": projected_path, "status": status, "category": _category(path), "analysis_status": "MATERIAL_AVAILABLE"}
        if external:
            change.update(repository_id=product_id)
            if not history:
                change["source_path"] = physical_path
            if physical_path.startswith("tools/") or physical_path in {"Dockerfile", ".dockerignore"}:
                change["category"] = "DEPLOYMENT"
        if not (git.allowed(path) if external else _allowed_source(path)):
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
    # 能力合同审查的是确切基线入口；入口未变化时也必须提供基线原件，不能只看候选。
    # 摘录校准程序缺少完整入口时保留缺失事实，由绑定器明确 NOT_BOUND，不合成源码。
    existing = {material["material_ref"] for material in materials}
    for contract in planning_contracts:
        path = contract["entrypoint"]["path"]
        for label, commit in (("baseline", baseline_sha), ("candidate", candidate_sha)):
            ref = label + ":" + path
            if ref in existing:
                continue
            try:
                raw = git.read(commit, path)
            except MaterialError as error:
                if error.code == "SOURCE_FILE_MISSING":
                    continue
                raise
            materials.append(_material(ref, label.upper() + "_SOURCE", path, commit, path, raw))
            existing.add(ref)
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
        "changes": changes, "materials": materials, "catalog": catalog, "planning_contracts": planning_contracts, "gaps": gaps,
        "current_candidate_coverage": [], "evaluation_status": "NOT_RUN",
    }
    if external:
        bundle["product_binding"] = git.binding(baseline_sha, candidate_sha, root)
        for material in materials:
            if material["kind"] in {"BASELINE_SOURCE", "CANDIDATE_SOURCE", "UNTRUSTED_DIFF"}:
                material.update(repository_id=product_id, source_path=git.physical_path(material["path"]))
    _require(len(json.dumps(bundle, ensure_ascii=False).encode("utf-8")) <= MAX_BUNDLE_BYTES,
             "RELEASE_BUNDLE_TOO_LARGE")
    return bundle


def assemble_release(root: Path, baseline_sha: str, candidate_sha: str, goal: str,
                     requirement_refs: list[str] | None = None, *, candidate_root: Path | None = None,
                     catalog_commit: str = CATALOG_COMMIT,
                     input_provenance: str = "LOCAL_COMMITTED_RELEASE") -> dict:
    """保留 v1 入口：未指定要求时仍使用旧默认，未知/空要求仍拒绝。

    新版正式请求只经过 assemble_review_request，不以额外参数悄悄放宽旧契约。
    """
    return _assemble_release(root, baseline_sha, candidate_sha, goal, requirement_refs,
                             candidate_root=candidate_root, catalog_commit=catalog_commit,
                             input_provenance=input_provenance)


def assemble_review_request(root: Path, request: object, *, candidate_root: Path | None = None,
                            catalog_commit: str = CATALOG_COMMIT,
                            input_provenance: str = "LOCAL_COMMITTED_RELEASE", product_id: str | None = None) -> dict:
    """ReviewRequest v1 → ReleaseBundle v2，保留准确任务及所有材料缺项。

    格式、Git 对象、固定目录及材料字节分别校验。请求只能引用现有规则，不能
    用 material_ref 开放文件读取；未接入/错配的要求保留供 Runtime 澄清。
    CONFIRMED_LOCAL 仅表示操作员的本地确认声明，不升级目录或实验执行权限。
    """
    # 延迟导入使既有 v1 CLI 不额外要求新协议的 Python 校验依赖。
    from .review_request import validate_review_request

    value = validate_review_request(request)
    refs = [row["id"] for row in value["requirements"]]
    bundle = _assemble_release(root, value["baseline"]["commit"], value["candidate"]["commit"],
                               value["goal"], refs, candidate_root=candidate_root,
                               catalog_commit=catalog_commit, input_provenance=input_provenance,
                               retain_missing_requirements=True, product_id=product_id)
    if product_id is not None:
        _require(all(value[side]["source_ref"] == bundle["product_binding"]["repository_url"]
                     for side in ("baseline", "candidate")), "REVIEW_PRODUCT_SOURCE_MISMATCH")
    bundle.update(schema_version=2, review_request=value)
    registered = {row["id"]: row for row in bundle["catalog"]["requirements"]}
    if not refs:
        bundle["gaps"].append({"code": "REVIEW_REQUIREMENTS_MISSING", "requirement_refs": [],
                               "detail": "审查请求没有要求；允许调查或澄清，不生成默认验收依据。"})
    for row in value["requirements"]:
        gap = None
        if row["id"] not in registered:
            gap = ("REVIEW_REQUIREMENT_NOT_REGISTERED", "要求尚未绑定到固定目录的可读规则。")
        elif row["material_ref"] != registered[row["id"]]["material_ref"]:
            gap = ("REVIEW_REQUIREMENT_MATERIAL_MISMATCH", "请求的材料引用与固定目录绑定不一致。")
        if gap:
            bundle["gaps"].append({"code": gap[0], "requirement_refs": [row["id"]], "detail": gap[1]})
    # 原请求原样保留。请求已声明的缺项/状态由 Runtime 统一评估，不在装配器
    # 生成第二份会漂移的权威准入状态，更不自动消除冲突或给出模型答案。
    _require(len(json.dumps(bundle, ensure_ascii=False).encode("utf-8")) <= MAX_BUNDLE_BYTES,
             "RELEASE_BUNDLE_TOO_LARGE")
    return bundle


def main(argv: list[str] | None = None) -> int:
    """只写操作员指定的新文件；拒绝覆盖，且不将完整正文打印到通用终端日志。"""
    parser = argparse.ArgumentParser(description="冻结真实 Git 版本对及受控阅读材料；不运行实验")
    parser.add_argument("--review-request", type=Path, help="正式审查请求；与旧版版本/目标/要求参数互斥")
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    parser.add_argument("--goal")
    parser.add_argument("--requirement", action="append")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--product-id", help="安装侧登记的独立产品；必须同时提供正式请求与候选仓库")
    parser.add_argument("--input-provenance", choices=["LOCAL_COMMITTED_RELEASE", "CALIBRATION_SYNTHETIC", "REGISTERED_EXTERNAL_PRODUCT"], default="LOCAL_COMMITTED_RELEASE")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _require(args.product_id is None or (args.review_request is not None and args.candidate_root is not None),
                 "REGISTERED_PRODUCT_REVIEW_REQUEST_REQUIRED")
        if args.review_request is not None:
            _require(all(value is None for value in (args.baseline, args.candidate, args.goal, args.requirement)),
                     "REVIEW_REQUEST_LEGACY_ARGUMENT_CONFLICT")
            from .review_request import read_review_request
            bundle = assemble_review_request(args.root, read_review_request(args.review_request),
                                             candidate_root=args.candidate_root, input_provenance=args.input_provenance, product_id=args.product_id)
        else:
            _require(all(value is not None for value in (args.baseline, args.candidate, args.goal)),
                     "RELEASE_ARGUMENTS_REQUIRED")
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
