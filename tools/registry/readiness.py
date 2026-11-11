"""任务就绪度投影：描述当前边界，不签发授权或业务 PASS。

调用链：task_assembler.assemble_test_context → build_readiness。
本模块仅消费已装配的数据，不访问文件、凭据、环境或业务系统。
"""

from copy import deepcopy


# 这里只定义“已登记缺口影响什么动作”，缺口事实与 ID 仍从固定 gaps.json 读取。
# 若目录后来增加新 ID，保持保守的待分类状态，不静默给予权限。
_ROUTING = {
    "REAL_OWNER_ASSIGNMENT": (
        ["business_owner", "qa_lead"], ["ENTERPRISE_APPROVAL", "TEAM_ADOPTION"],
        "维护角色可用于本地计划；实际人员与职责接受仍待登记，不能声称已有企业审批。"),
    "ENTERPRISE_RULE_AUTHORITY": (
        ["business_owner"], ["FORMAL_BUSINESS_ACCEPTANCE"],
        "可阅读实验规则；不能把本地校准条款提升为公司已批准的验收标准。"),
    "ENTERPRISE_SOURCE_SCOPE": (
        ["identity_runtime_maintainer"], ["ENTERPRISE_SOURCE_READ", "BUSINESS_API_EXECUTION"],
        "本轮只读固定本地材料；未来实例、账号、租户与 ACL 需单独核验。"),
    "RETENTION_AND_RESIDENCY": (
        ["egap_maintainer", "identity_runtime_maintainer"], ["ENTERPRISE_DATA_INGESTION", "MODEL_EGRESS", "EXTERNAL_EXPORT"],
        "缺少企业数据治理确认不否定已允许的本地目录读取，也不授予任何模型传输或导出权限。"),
    "RAW_MODEL_AND_HTTP_ARCHIVE": (
        ["egap_maintainer", "sut_maintainer"], ["COMPLETE_MODEL_ATTRIBUTION", "TRUSTED_RAW_OBSERVATION"],
        "历史原始模型请求/返回与原生错误正文存在缺口；重新构造不能冒充当时实录。前置 Job 失败调查不要求先调用模型。"),
    "REGISTERED_EXECUTION_PROFILES": (
        ["egap_maintainer", "qa_lead"], ["EXPERIMENT_DISPATCH"],
        "组件入口可定位，但 Runner/Observer/Rebuild 包装及环境资格尚未登记，当前不可派发。"),
    "PROVIDER_API_CONTRACT": (
        ["billing_maintainer"], ["PROVIDER_SEMANTICS_CONFIRMATION", "PRODUCT_ACCEPTANCE"],
        "适配代码与历史 HTTP 观察可供调查；接口适用状态、版本与缺失语义仍需提供方契约确认。"),
}


def route_catalog_gaps(reader, gap_records):
    """为原有缺口附加本任务影响与已有维护角色，不复制一套缺口事实。

    未知缺口不阻断已经明确获准的目录读取，但其影响仍标成待分类；本函数
    无权由缺口是否存在推断实际操作许可，许可由可信 scope 单独约束。
    """
    routed = []
    for gap in gap_records["gaps"]:
        roles, actions, implication = _ROUTING.get(gap["id"], (
            ["egap_maintainer"], ["UNCLASSIFIED_REQUIRES_REVIEW"], "新目录缺口尚未完成任务影响分类。"))
        row = deepcopy(gap)
        row.update(source=deepcopy(gap_records["source"]), requested_from_roles=deepcopy(roles),
                   role_assignments=[reader.get_asset("role", role) for role in roles],
                   affected_actions=deepcopy(actions), task_implication=implication,
                   blocks_local_catalog_read=False, routing_status="TASK_INTERPRETATION_NOT_APPROVAL")
        routed.append(row)
    return routed


def build_readiness(reader, requirements, materials, capabilities, evidence):
    """返回六个独立维度；即使材料可读，也不把目录身份当产品资格。

    输入由 TaskAssembler 完成固定版本读取。唯一授权依据是 reader.scope；
    用户任务 JSON 中没有批准字段。本轮操作上限始终为 PLAN_ONLY。
    """
    statuses = sorted({material["status"] for material in materials})
    if statuses == ["READABLE"]:
        material_status = "READABLE"
    elif statuses == ["METADATA_ONLY"]:
        material_status = "METADATA_ONLY"
    else:
        material_status = "PARTIAL_OR_UNAVAILABLE"
    scope = reader.resolver.scope
    local_allowed = bool(scope.allow_metadata and scope.purpose == "LOCAL_PLAN")
    # 固定字节可读不等于其中所需字段/规则片段可解释；解析与定位失败
    # 必须保留在对应维度，不能因 Git/SHA 校验成功而宣称证据完备。
    rule_states = sorted({r["material"]["status"] for r in requirements})
    rules_readable = rule_states == ["READABLE"]
    issue = evidence["historical_issue"]
    issue_interpretable = issue["status"] == "READABLE" and "observations" in issue
    return {
        "catalog": {"status": reader.validation["status"], "validation": deepcopy(reader.validation),
                    "catalog_commit": reader.catalog_commit, "business_approval_granted": False},
        "materials": {"status": material_status, "observed_states": statuses,
                      "material_refs": [m["material_ref"] for m in materials],
                      "scope": "REGISTERED_FIXED_LOCAL_MATERIALS_ONLY",
                      "live_source_availability": "NOT_PROBED"},
        "rules": {"status": "LOCAL_CALIBRATION_PENDING_REVIEW" if rules_readable else "RULE_MATERIAL_PARTIAL_OR_UNAVAILABLE",
                  "material_states": rule_states,
                  "requirement_refs": [r["id"] for r in requirements],
                  "formal_business_acceptance_allowed": False,
                  "mapping_status": "DRAFT_REQUIRES_OWNER_QA_CONFIRMATION"},
        "capabilities": {"status": "REGISTERED_NOT_DISPATCHABLE",
                         "registered_count": len(capabilities), "callable_count": 0,
                         "pending_profiles": [{"capability_ref": c["id"], "profile_gaps": c["profile_gaps"]}
                                              for c in capabilities]},
        "authorization": {"status": "LOCAL_PLAN_ONLY" if local_allowed else "DENIED",
                          "principal_id": scope.principal_id, "purpose": scope.purpose,
                          "local_plan_allowed": local_allowed,
                          "body_read_scope": "RESOLVER_ENFORCED_PER_MATERIAL",
                          "execute_allowed": False, "model_egress_allowed": False,
                          "source_of_authority": "TRUSTED_CALLER_SCOPE_NOT_TASK_FIELDS"},
        "evidence": {"status": "HISTORICAL_READING_ONLY" if issue_interpretable else "HISTORICAL_MATERIAL_UNAVAILABLE",
                     "historical_issue_state": issue["status"],
                     "historical_issue_interpretable": issue_interpretable,
                     "current_evaluation_status": evidence["current_evaluation_status"],
                     "current_candidate_coverage": [], "current_candidate_acceptance_allowed": False,
                     "historical_pass_is_current_pass": False,
                     "historical_model_raw_capture": "NOT_AVAILABLE_IN_EXISTING_ARCHIVE",
                     "new_observations_collected": False},
    }
