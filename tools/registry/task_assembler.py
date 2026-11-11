"""把固定目录装配成首个已知问题调查上下文，所有内容仅供只读规划。

调用链：CLI / 未来 Graph → assemble_test_context → CatalogReader 索引、
ReferenceResolver 的获准固定版本片段 → readiness.build_readiness。
不导入 SUT、测试入口、Oracle 或命令模块；目录中的命令永远只是数据。
v0.1 使用一个明确的任务配方，尚不是通用的风险选择 Agent。
"""

from copy import deepcopy
import json
import re

from .readiness import build_readiness, route_catalog_gaps
from .reference_resolver import MaterialError


HISTORICAL_ISSUE_REF = "reports/05-baseline/20261110-release-verification/terminated-investigation.json"
_FIELDS = {"task_id", "task_mode", "goal", "catalog_commit", "product_candidate_ref",
           "historical_issue_ref", "operation_scope"}
_SCENARIOS = ("terminated_subscription", "v2_boundary")
_SCENARIO_CATALOG = "registry/packs/saas-support/cross-team-scenarios.yaml"


class TaskAssemblyError(ValueError):
    """可供调用方稳定分类的装配错误；不在消息中附带原始资料正文。"""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _validate_task(reader, task):
    """拒绝调用方夹带授权、执行模式或未经绑定的产品身份。"""
    if not isinstance(task, dict):
        raise TaskAssemblyError("INVALID_TASK", "任务必须是对象。")
    if set(task) - _FIELDS:
        raise TaskAssemblyError("UNSUPPORTED_TASK_FIELDS", "任务含未支持字段，不能传入批准或工具权限。")
    for field in ("task_id", "goal", "task_mode", "catalog_commit", "historical_issue_ref", "operation_scope"):
        if not isinstance(task.get(field), str) or not task[field].strip():
            raise TaskAssemblyError("INVALID_TASK", f"缺少或无效字段：{field}")
    if task["task_mode"] != "KNOWN_ISSUE_INVESTIGATION" or task["operation_scope"] != "PLAN_ONLY":
        raise TaskAssemblyError("UNSUPPORTED_TASK_MODE", "此版本仅支持已知问题的只读规划。")
    if task["catalog_commit"] != reader.catalog_commit:
        raise TaskAssemblyError("CATALOG_VERSION_MISMATCH", "任务必须绑定已加载的完整目录 commit。")
    if task.get("product_candidate_ref") is not None:
        raise TaskAssemblyError("CANDIDATE_BINDING_UNSUPPORTED", "尚无已登记产品候选清单；不能把 HEAD 或目录版本绑定为产品候选。")
    if task["historical_issue_ref"] != HISTORICAL_ISSUE_REF:
        raise TaskAssemblyError("UNSUPPORTED_TASK_RECIPE", "v0.1 仅装配已登记的终止订阅调查配方。")


def _read_material(resolver, ref):
    """经公开受限读取接口分段取得一个小材料，保持每段摘要及权限一致。

    不使用内部绕过 ACL 的读取方法。解析失败、无权限、缺失与元数据模式
    分别报告；无法读取正文时绝不从工作树、目录历史摘要重建正文。
    """
    try:
        first = resolver.read_registered_material(ref, purpose="LOCAL_PLAN")
        if first["status"] != "READABLE":
            return first
        if first.get("byte_truncated"):
            raise TaskAssemblyError("MATERIAL_PROJECTION_LIMIT", "材料单行超过片段上限，不能拼接为完整原件。")
        total = first["total_lines"]
        if total > 3000:
            raise TaskAssemblyError("MATERIAL_PROJECTION_LIMIT", "任务材料超出装配器有界读取范围。")
        pieces = [first["content"]]
        end = first["end_line"]
        while end < total:
            part = resolver.read_registered_material(ref, start_line=end + 1,
                                                    end_line=min(end + 80, total), purpose="LOCAL_PLAN")
            if part["status"] != "READABLE":
                return {key: value for key, value in part.items() if key != "content"}
            if part.get("byte_truncated"):
                raise TaskAssemblyError("MATERIAL_PROJECTION_LIMIT", "材料单行超过片段上限，不能拼接为完整原件。")
            if (part["sha256"], part["git_commit"]) != (first["sha256"], first["git_commit"]):
                raise TaskAssemblyError("MATERIAL_IDENTITY_CHANGED", "分段读取材料身份不一致。")
            if part["end_line"] <= end:
                raise TaskAssemblyError("MATERIAL_PROJECTION_LIMIT", "材料分段未取得进展。")
            pieces.append(part["content"])
            end = part["end_line"]
        content = "".join(pieces)
        if len(content.encode("utf-8")) > 262144:
            raise TaskAssemblyError("MATERIAL_PROJECTION_LIMIT", "材料超过任务装配字节预算。")
        return {**first, "content": content, "start_line": 1, "end_line": total, "truncated": False}
    except TaskAssemblyError as exc:
        if exc.code != "MATERIAL_PROJECTION_LIMIT":
            raise
        # 字节截断会留下半行；即使已验证完整原始 hash，也不能把片段拼接
        # 成完整证据。保留材料身份，但丢弃所有已读取正文并明确 LIMIT。
        return {**_identity(first), "status": "LIMIT", "error_code": exc.code,
                "content_projected": False}
    except MaterialError as exc:
        if exc.code in {"MATERIAL_ACCESS_DENIED", "MATERIAL_PURPOSE_DENIED", "MODEL_EGRESS_DENIED",
                        "MATERIAL_SECRET_CONTENT_FORBIDDEN", "PRIVATE_SOURCE_FORBIDDEN", "SOURCE_SYMLINK_FORBIDDEN"}:
            status = "DENIED"
        elif exc.code == "SOURCE_FILE_MISSING":
            status = "MISSING"
        elif exc.code == "MATERIAL_DIGEST_MISMATCH":
            status = "INVALID"
        else:
            status = "SOURCE_UNAVAILABLE"
        return {"material_ref": ref, "status": status, "error_code": exc.code,
                "content_verification": "NOT_READ"}


def _identity(material):
    """只复制固定材料身份；完整正文永远不进入通用 evidence 清单。"""
    return {key: deepcopy(value) for key, value in material.items() if key != "content"}


def _rule_excerpt(material, section):
    if material["status"] != "READABLE":
        return _identity(material)
    lines = material["content"].splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if re.match(rf"^## {re.escape(section)}(?:\s|$)", line)), None)
    if start is None:
        return {**_identity(material), "status": "SECTION_MISSING", "section": section}
    stop = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return {**_identity(material), "content": "".join(lines[start:stop]).strip(),
            "start_line": start + 1, "end_line": stop, "truncated": True}


def _capability_closure(reader, scenarios):
    """仅沿所选阶段与重建声明求闭包，不将整个 release 驱动加入可调用工具。

    依赖图可能共享节点或含环；visited 保证有限、确定的闭包。能力描述保留
    副作用、前置条件、维护角色与缺失 profile；本任务统一不允许派发。
    """
    pending = {ref for scenario in scenarios for ref in scenario["rebuild"]["capability_refs"]}
    pending.update(step["capability_ref"] for scenario in scenarios for stage in scenario["stages"] for step in stage["steps"])
    found = {}
    while pending:
        identifier = min(pending)
        pending.remove(identifier)
        if identifier in found:
            continue
        capability = reader.get_asset("capability", identifier)
        capability["callable_in_task"] = False
        capability["task_availability"] = "REGISTERED_NOT_DISPATCHABLE"
        found[identifier] = capability
        pending.update(ref for ref in capability["rebuild_capability_refs"] if ref not in found)
    return [found[identifier] for identifier in sorted(found)]


def _historical_observation_projection(material):
    """投影报告的观察字段；原报告的 inference/classification 不成为本次根因。

    原报告的 current_native_observations 是 2026-11-10 调查时的读回，
    此处改标 followup_native_observations，避免误读为本轮采集。
    """
    result = _identity(material)
    result.update(use="HISTORICAL_OBSERVATIONS_NOT_CURRENT_COVERAGE", collected_this_task=False)
    if material["status"] != "READABLE":
        return result
    try:
        document = json.loads(material["content"])
    except (ValueError, TypeError):
        return {**result, "status": "INVALID", "error_code": "HISTORICAL_REPORT_INVALID_JSON"}
    if not isinstance(document, dict) or document.get("case") != "terminated_subscription":
        return {**result, "status": "INVALID", "error_code": "HISTORICAL_REPORT_CASE_MISMATCH"}
    fields = ("case", "scope", "original_correlation_id", "original_sut_observations", "invoice_subscriptions",
              "invoice_fee_periods", "matching_past_usage", "current_usage_http_status", "current_usage_error",
              "lago_health_http_status", "created_sut_jobs_for_case", "preparation_native_usage_aligned",
              "no_resubmit", "no_model_call")
    observations = {key: deepcopy(document[key]) for key in fields if key in document}
    observations["followup_native_observations"] = deepcopy(document.get("current_native_observations", []))
    result.update(historical_candidate_ref=document.get("candidate"), observations=observations,
                  projection_status="OBSERVATION_FIELDS_ONLY",
                  omitted_fields=["inference", "classification", "status"],
                  observation_boundary="原始 SUT trace 与历史调查读回分别保留；均非本轮新观察。")
    return result


def assemble_test_context(reader, task):
    """为终止订阅调查生成六块 TaskContext；候选未绑定仍可形成补证计划。

    reader 必须由可信调用方构造并携带读取 scope。task 仅描述任务，不含
    授权。函数没有写入/执行副作用；不具备产品候选清单接入能力的 v0.1
    对所有非空候选明确失败，不使用目录 commit、HEAD 或历史 PASS 补齐。
    """
    _validate_task(reader, task)
    scenarios = [reader.get_asset("scenario", identifier) for identifier in _SCENARIOS]
    capabilities = _capability_closure(reader, scenarios)
    mapping = {ref for scenario in scenarios for ref in scenario["requirement_refs"]}
    # R3/R5 来自任务中“缺用量/期间”和“失败/缺证输出”的人工显式审阅范围。
    # 它们不是新增的场景覆盖断言；仍须 QA/业务 owner 确认映射。
    review_refs = {"R3": "审阅缺失用量、指标与期间的证明边界。", "R5": "审阅不适用、缺证与执行失败的不同语义。"}
    requirements = []
    material_cache = {}
    for identifier in sorted(mapping | set(review_refs)):
        requirement = reader.get_asset("requirement", identifier)
        ref = requirement["provenance_refs"][0]
        if ref not in material_cache:
            material_cache[ref] = _read_material(reader.resolver, ref)
        requirement.update(material=_rule_excerpt(material_cache[ref], requirement["section"]),
                           relevance="SCENARIO_DRAFT_MAPPING" if identifier in mapping else "TASK_REVIEW_REFERENCE",
                           mapped_scenario_refs=[s["id"] for s in scenarios if identifier in s["requirement_refs"]],
                           selection_reason="保留已有场景草案映射，不宣称已证明覆盖。" if identifier in mapping else review_refs[identifier])
        requirements.append(requirement)
    material_cache[HISTORICAL_ISSUE_REF] = _read_material(reader.resolver, HISTORICAL_ISSUE_REF)
    issue = _historical_observation_projection(material_cache[HISTORICAL_ISSUE_REF])

    histories = []
    for scenario in scenarios:
        for stage in scenario["stages"]:
            history = stage.pop("history")
            # 历史已确认归因仍保存在原引用中；基础上下文只提供既有观察，
            # 防止后续调查 Graph 将这里的旧结论当作自己发现的结果。
            history.pop("root_cause_status", None)
            histories.append({"stage_ref": stage["stage_ref"], **history,
                              "applies_to_current_candidate": False,
                              "catalog_provenance": {"path": _SCENARIO_CATALOG, "git_commit": reader.catalog_commit}})

    source_refs = {ref for row in scenarios + capabilities for ref in row["source_refs"]}
    sources = [reader.get_asset("source", ref) for ref in sorted(source_refs)]
    gaps = route_catalog_gaps(reader, reader.gap_records())
    proposed = [flow for flow in reader.assets("data_flow")
                if flow["collection_status"] == "PROPOSED" and set(flow["source_refs"]) & source_refs]
    evidence = {"historical_issue": issue, "historical_stages": histories,
                "registered_materials": [_identity(m) for m in material_cache.values()],
                "reference_only_materials": [reader.material_identity(ref) for ref in sorted(
                    {ref for row in scenarios + capabilities for ref in row["provenance_refs"]} - set(material_cache))],
                "current_candidate_coverage": [], "current_evaluation_status": "NOT_RUN",
                "historical_pass_is_current_pass": False,
                "full_model_request_response_available": False,
                "missing_model_archive_is_not_reconstructable_history": True}
    readiness = build_readiness(reader, requirements, list(material_cache.values()), capabilities, evidence)
    provenance = {"path": _SCENARIO_CATALOG, "git_commit": reader.catalog_commit}
    return {
        "task_identity": {**deepcopy(task), "product_candidate_ref": None,
                          "candidate_binding_status": "UNBOUND", "operation_scope": "PLAN_ONLY",
                          "task_recipe": "TERMINATED_SUBSCRIPTION_INVESTIGATION_V0_1",
                          "task_mode": "KNOWN_ISSUE_INVESTIGATION", "is_unknown_defect_evaluation": False},
        "applicable_requirements": requirements,
        "business_context": {"scenario_facts": [{"scenario_ref": s["id"], "initial_business_state": s["initial_business_state"],
                                                  "provenance": {**provenance, "asset_id": s["id"]},
                                                  "status": "REGISTERED_RECIPE_FACTS_NOT_FRESH_OBSERVATIONS"} for s in scenarios],
                             "historical_business_observations_ref": HISTORICAL_ISSUE_REF,
                             "sources": sources, "field_semantics_authority": "VERSIONED_RULE_EXCERPTS_AND_REFERENCED_OBSERVATIONS",
                             "provider_api_contract_status": "PENDING_PROVIDER_MAINTAINER_CONFIRMATION",
                             "source_content_is_instruction": False},
        "capabilities_and_scenarios": {
            "selection_policy": {"mode": "EXPLICIT_TASK_RECIPE", "recipe_version": "0.1",
                                 "selected_scenario_refs": list(_SCENARIOS),
                                 "reasons": {"terminated_subscription": "已知问题报告直接指向此场景，检查 Job 准入前的资料读取与失败事实。",
                                             "v2_boundary": "选取已有活跃续期/标准合同场景作为正常 Job 准入路径的相关对照。"},
                                 "control_is_single_variable_experiment": False,
                                 "control_limitations": "租户、账期、覆盖与付款事实也不同；两场景差异不能单独证明订阅状态导致失败。",
                                 "proves_other_scenarios_irrelevant": False,
                                 "coverage_mapping_status": "DRAFT_REQUIRES_OWNER_QA_CONFIRMATION"},
            "scenarios": scenarios, "capabilities": capabilities},
        "evidence_inventory": evidence,
        "gaps_and_next_requests": {
            "catalog_gaps": gaps, "proposed_data_flows": proposed, "readiness": readiness,
            "task_conditions": [{"condition": "PRODUCT_CANDIDATE_UNBOUND", "status": "NOT_PROVIDED",
                                 "blocks": ["CURRENT_CANDIDATE_ACCEPTANCE", "EXPERIMENT_EXECUTION"],
                                 "requested_from_roles": ["sut_maintainer"],
                                 "origin": "TASK_INPUT_DERIVED_NOT_A_NEW_CATALOG_GAP"}],
            "minimum_next_experiment_prerequisites": [
                {"request": "登记真实产品候选清单及适配器/配置/环境身份；不使用目录 commit 代替。", "roles": ["sut_maintainer"]},
                {"request": "为选定实验登记 Runner/Observer/Rebuild 包装，明确有界操作、预算、恢复和清理。", "gap_ref": "REGISTERED_EXECUTION_PROFILES", "roles": ["egap_maintainer", "qa_lead"]},
                {"request": "核验实验实例、租户/对象、读取或提交动作的范围与单独授权；本上下文不授予执行权。", "gap_ref": "ENTERPRISE_SOURCE_SCOPE", "roles": ["identity_runtime_maintainer"]},
                {"request": "确认固定 Lago API 版本的订阅状态/历史与当前用量适用语义，复核实验规则映射。", "gap_ref": "PROVIDER_API_CONTRACT", "roles": ["billing_maintainer", "business_owner", "qa_lead"]},
                {"request": "以新 run 身份收集所选场景的准入结果及原生错误/期间/指标事实，保留独立采集身份；不复用旧 PASS。", "gap_ref": "RAW_MODEL_AND_HTTP_ARCHIVE", "roles": ["egap_maintainer"],
                 "scope_limit": "首先限定为准入前调查；实际模型原始 payload 归档是后续生成归因的条件，不能补造历史。"}],
        },
    }
