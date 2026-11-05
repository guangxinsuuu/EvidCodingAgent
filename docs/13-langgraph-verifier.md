# 13 · 用 LangGraph 规划实验，保持事实和裁决独立

实施状态：`NOT_STARTED`。本章所有模块、Graph、详细规划对象和脚本均为拟建；没有运行模型或产生业务验证结果。

前一章：[12 · Codex Coding Worker](12-codex-coding-worker.md)。下一章：[14 · Final Gate 与证据](14-final-gate-and-evidence.md)。依赖 [06 · Contract Registry](06-contract-registry.md)、[07 · Observer 与 Experiment Barrier](07-observer-and-experiment-barrier.md)、[08 · 原生 Oracle](08-native-oracles.md) 和第 10–11 章的持久化与预算。

## 目标与边界

本章完成一个可恢复的版本验证规划器：以稳定版本与待发布候选的真实变化、批准的新需求和既有不变量为输入，分析风险，从批准目录中选择检查，接收独立执行结果，再决定继续探索、按授权请求修复或进入最终验收。主入口先验证已有新功能候选；只有有效 Finding 才可能触发 Coding。它应能解释“变化影响了什么、为什么测这些、为什么省略其他探索”，但不能自行写出可信 PASS。

验证独立性由受保护的规则、执行身份、环境、观察来源和证据写权限共同建立。Python Verifier可以选择实验、质疑结果和提出Finding；不能改业务代码、修改已批准业务真相或批准自己的发布。Coder可按scope编写开发测试，但其报告只能作为开发反馈；换一个LLM或另起同权限子进程不足以取得独立Oracle资格。首次模型接入前，先以确定性Runner/Oracle和坏观察器测试验证这条信任边界。

先用确定性 Planner 完成 Graph 与账本对接，再接入模型做风险解释和实验排序。否则一次失败会同时混合 Graph 状态、模型输出和测试基础设施，无法定位原因。模型可提出假设；Runner/Observer 提供事实；Oracle 裁决断言；Final Gate 判断覆盖与放行资格。

## 为什么用 LangGraph，但不把它变成第二个 Runtime

LangGraph 适合表达有分支、循环、暂停和恢复的验证策略。项目采用 Python LangGraph 独立进程；TypeScript 控制面继续拥有任务、预算和结果登记。双方共用版本化 JSON Schema，并生成/校验各语言 DTO，不能直接依赖 TS 类型作为网络协议。Python/LangGraph 版本在实现时按兼容性锁定，目前未安装或运行该 Verifier。

官方将 checkpointer 定义为线程状态持久化机制；内存 saver 重启会丢失。checkpointer 与跨线程 store 的用途不同。[官方 Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

重放会重新执行后续节点，包括其中的模型/API 调用。因此，checkpoint 不是副作用恰好一次的保证；新线程的业务准入也必须先写入项目账本，不能以“查不到 checkpoint”推断任务从未被接受。[官方 Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)

本项目据此规定：PostgreSQL 的 Run/CheckRun/EffectOperation/BudgetReservation 是权威事实；Graph 存其引用和规划摘要。数据库事务先登记请求与 outbox，再触发 Graph。Graph checkpoint 的 PostgreSQL 存储使用独立表/权限，不与领域记录混为一张状态对象。不要把 checkpointer 的某个 durability 选项当成分布式事务。

## 拟建模块

| 拟建路径 | 职责 |
|---|---|
| `services/verifier/egap_verifier/graph.py` | Graph 拓扑、分支和停止出口。 |
| `services/verifier/egap_verifier/checkpointer.py` | 固定版本 saver 接入与生命周期管理。 |
| `services/verifier/egap_verifier/graph_state.py` | 仅引用和必要摘要的 GraphState。 |
| `services/verifier/egap_verifier/context_builder.py` | 本次适用规则/事实/工具能力的授权语义组装。 |
| `services/verifier/egap_verifier/hypotheses.py` | 假设身份、去重、状态和证据关联。 |
| `services/verifier/egap_verifier/change_impact.py` | 确定性变更提取与模型风险提案分区。 |
| `services/verifier/egap_verifier/planner.py` | 确定性/模型 Planner 的共同接口。 |
| `packages/verification/src/intent-validator.ts` | 模板、参数、身份、环境、预算验证。 |
| `services/verifier/egap_verifier/check_client.py` | 通过控制面提交/查询检查，不直连业务 DB。 |
| `services/verifier/tests/test_replay.py` | checkpoint 重放与逻辑检查去重。 |

所有 tool schema 放入共享 contracts；Python Graph 节点不能自建一套与控制面不一致的 JSON 类型。跨进程消息通过 schema 运行时校验并验证调用身份，生成的 TypeScript/Python 类型不等于鉴权。`intent-validator.ts` 留在可信控制面侧；Graph 只提交提案。

## 检查点 13.1：把模式和状态说清楚

WorkOrder/Run 独立冻结 `task_mode=RELEASE_VERIFICATION|INCIDENT_REPAIR`、`repair_policy=DISABLED|ON_VALIDATED_FINDING` 和 `completion_mode=FINAL_ONLY|DRAFT_PR_CI`，不能从其中一个字段推断另一个。Release 主入口包含 `stable_release_manifest_ref`、`submitted_candidate_ref`、`release_changeset_ref`、`requirement_delta_ref`、`historical_coverage_refs`；Incident 入口保留已知事故与修复上下文。`base_commit_sha/base_input_digest` 始终指任务冻结的比较基线，Release 中是 stable；发生修复时另外记录 `CodingAttempt.repair_input_candidate_ref`，不能把 stable 偷换成修复前候选。

本章的实验模式与第 18 章评测组别不是同一概念：

| Graph 实验模式 | 模型可决定什么 | 可信服务必须限制什么 |
|---|---|---|
| `EXISTING_CHECKS`，对应 v3 模式 A | 在批准检查目录中排序。 | mandatory 不可删除，版本不可替换。 |
| `PARAMETERIZED`，对应 v3 模式 B | 在已批准模板内选已批准工单/政策fixture、日期/工具Schema组合、屏障与seed 等。 | 参数域、资源、环境、重复次数和预算。 |
| `NOVEL_PROPOSAL`，对应 v3 模式 C | 提出目录外检查或观察需求。 | 首期仅记录提案，不自动执行，不升为 release oracle。 |

首期只实现前两种自动执行路径。新实验需要新的模板、独立评审、正反校准和 Profile 版本，不能因为模型产生了一段测试代码就自动加入 Final。

以下用 TypeScript 记法展示跨语言 GraphState 协议投影；以下均为拟建协议投影，实际Python DTO由共享版本化JSON Schema校验，未实现前不宣称运行能力：

```ts
type VerificationGraphState = {
  run_ref: string;
  candidate_ref: string;
  frozen_context_ref: string;
  task_mode: "RELEASE_VERIFICATION" | "INCIDENT_REPAIR"; // 冻结上下文投影
  repair_policy: "DISABLED" | "ON_VALIDATED_FINDING"; // 不是 Graph 自行授权
  stable_release_manifest_ref: string | null;
  submitted_candidate_ref: string | null;
  release_changeset_ref: string | null;
  requirement_delta_ref: string | null;
  historical_coverage_refs: string[];
  graph_revision: string;
  impact_map_ref: string | null;
  selection_decision_ref: string | null;
  hypothesis_refs: string[];
  check_run_refs: string[];
  observation_refs: string[];
  validated_finding_refs: string[];
  coverage_snapshot_ref: string | null;
  budget_snapshot_ref: string; // 展示快照，扣减以账本为准
  exploration_closed: boolean; // 仅映射 Runtime 权威值，Graph 不能自行重开
  no_progress_count: number;
  next_action: "PLAN" | "WAIT" | "REPAIR" | "FINAL" | "STOP";
};
```

不把原始日志、业务数据库行、密钥或整份源码塞进 checkpoint。保存内容引用时仍需检查当前访问权限；引用不是 bearer token。模型假设与 ObservationBundle 不能共用一个“facts”数组，否则推测容易在下一轮变成事实。

## 检查点 13.2：先实现无模型的完整 Graph

Release 从已接收的外部候选开始：第 10 章在 BASELINING 核对资格，通过 `RELEASE_CANDIDATE_ACCEPTED` 进入 DISCOVERY_VERIFYING，不要求先调用 Coding。先用确定性规则按06/08/09批准映射选择具备观察能力的SUP检查及本次政策/schema发布的功能兼容断言；语义质量首期独立报告，不声明自动对客资格，再逐步接模型。下面是项目伪代码，不是可以复制执行的 LangGraph API：

```text
LoadFrozenContext
  -> ValidateTaskInputs (Release: stable/candidate/approved requirement delta)
  -> ReconcileRegisteredChecks
  -> BuildImpactMap
  -> AssessCoverage
  -> SelectNextIntent
  -> ValidateIntent
  -> RegisterOrReadCheckRun
  -> WaitForTrustedResult
  -> ReadObservationAndResult
  -> ValidateResultTuple
  -> RouteExecutionStatus
       QUEUED / RUNNING    -> WAIT
       UNKNOWN            -> ReconcileExecution -> WAIT or explicit unresolved outcome
       ERROR / CANCELLED  -> ExecutionRecoveryOrCancellation; never PASS
       COMPLETED          -> RouteAssertionStatus
         NOT_EVALUATED    -> WAIT or INCOMPLETE
         INCONCLUSIVE     -> EvidenceOrContractInvestigation; never automatic repair
         FAIL            -> RouteAttribution
           CANDIDATE_DEFECT + validated finding + repair authorized + exploration open + CODING budget
                         -> RequestRepair -> end current candidate loop
           CANDIDATE_DEFECT + repair disabled/not authorized -> retain finding; no Coding or acceptance
           CANDIDATE_DEFECT + required CODING budget exhausted -> Runtime stop decision; no Final readiness
           CANDIDATE_DEFECT + exploration_closed -> retain Final failure; no new Coding/Discovery
           ENVIRONMENT / ACCESS            -> RouteToOwnerOrRecovery
           CONTRACT_AMBIGUITY              -> BLOCKED
           UNKNOWN                         -> InvestigateCause
         PASS + NONE      -> UpdateCoverage -> DecideNextWork
  -> DecideNextWork
       useful discovery + DISCOVERY budget -> SelectNextIntent
       discovery complete + final_readiness eligible -> RequestFinal
       current phase required exploration pool exhausted + final_readiness eligible
                         -> FINAL_DRAIN_READY -> FINAL_VERIFYING
       cancelled / global deadline / cannot meet Final readiness -> STOP
```

`RegisterOrReadCheckRun` 调用第 10 章已有控制面协议：逻辑 CheckKey 由可信服务计算；登记 CheckRun、预算 reservation 和 outbox 必须原子或遵循已证明的事务协议。Graph 不预先占预算后又让 Runner 重新占一次。派发响应丢失时用原 command/CheckKey 查询，不换 seed 或 request ID 假装是新实验。

`RequestRepair` 提交当前候选的 ValidatedFinding 引用给 Runtime。Runtime 重新核对 `repair_policy=ON_VALIDATED_FINDING`、动作/路径授权、当前 scope、未关闭探索和 CODING 额度，才可转换到 `CODING` 并产生 Attempt；记录 `repair_input_candidate_ref` 为被发现缺陷的候选，任务 stable 基线不变。`DISABLED` 的 Finding 是验证结果，不能偷偷启动修复或把候选标合格。Graph 无权改候选文件。修复产生新候选后重新计算 stable→当前候选的变化和需求覆盖，可附上修复前→修复后差异；旧 checkpoint 不可通过替换 `candidate_ref` 冒充相同实验。

未调用 Coding 不代表 NO_CHANGE。外部新功能 candidate 相对 stable 为 `CHANGED` 时，直接通过独立 Final 也可按 completion_mode 成功/交付，`repair_success=false`。Coder 提议未修改时保留其可信输入候选（Release 为 submitted/current repair input），不能退回 stable。只有最终完整输入与任务 base 一致且任务需求仍满足，才可能是 `change_kind=UNCHANGED`；它仍需第 12 章可信 Collector 产生真实候选和独立 Discovery/Final，不能把 NO_CHANGE_PROPOSED 或空 diff 直接送终态。

先 execution_status、再 assertion_status、最后 attribution 的顺序不可省略。`NONE` 仅用于 COMPLETED+PASS；null 仅用于 NOT_EVALUATED 且尚未归因，失败/不确定而原因未知才用 UNKNOWN；FAIL/INCONCLUSIVE 不得为 null/NONE，完整真值表见第 08 章。禁止单独匹配 `attribution=UNKNOWN` 就把正常结果送去对账，也禁止 execution UNKNOWN 因带 CANDIDATE_DEFECT 字样而直接修复。非法组合由 Validator 拒绝，不能由模型猜测修正。

预算选择使用冻结的 `budget_pool=CODING|DISCOVERY|FINAL|RECONCILIATION|CLEANUP`。`repair_policy=DISABLED` 的 Release 可将 CODING 配额设为 0，不能因此停止 DISCOVERY 或 Final。只有当前阶段实际必需的探索池耗尽才触发第 11 章收尾判断。`final_readiness` 是当前候选的送验判断及引用，不是 Final PASS 或新增 Run 状态；需冻结身份、有效合同/权限、环境/观察能力、无当前已知候选资产Finding、无阻断未知和足够 FINAL/MANDATORY 预留，具体事务以第 11/14 章为准。触发耗尽时 Runtime 先原子关闭探索派发并设置 `exploration_closed=true`，满足条件才以 `FINAL_DRAIN_READY` 事件进入 FINAL_VERIFYING。存在可有界确认的未知时仅按第 11 章先对账，决策截止前仍不满足则 STOPPED。收尾期间发现新失败也不能重新 Coding/Discovery，只保留失败并按政策终结。不得先标 STOPPED 再恢复业务。已经 STOPPED/CANCELLED 的 Run 只允许对账与清理，不能用 Graph replay 重启 Coding/Discovery/Final。

第一轮完成标志：不用模型也能走完“计划→持久检查→可信结果→分支”，并能在每条边看到关联 ID 和拒绝原因。

## 检查点 13.3：业务分析与实验策略的详细协议

这里补足“有模块名但尚无可执行约定”的部分。以下对象/算法为拟建规格，当前没有对应运行中的Agent。首期只支持声明的变更范围；复杂Python数据流、动态导入、供应商模型内部变化不能假装已精确推导。

### 13.3a 统一引用、身份与Context Builder

对象通过共享JSON Schema校验，`additionalProperties=false`；数组有冻结上限，ID/摘要/日期格式明确。引用是定位符不是权限，读取时检查当前ACL/撤销。每个规划对象都绑定`schema_version, run_ref, candidate_ref, frozen_context_ref, object_revision`，对象内容不可原地覆盖。

```ts
type AssetRef = { id: string; revision: string; digest: string }; // digest=规范化SHA256
// 下列字段是拟建跨进程协议；实际JSON Schema生成/校验两端DTO。
type ContextBundle = {
  schema_version: 1; run_ref: string; candidate_ref: string;
  frozen_context_ref: string; object_revision: string;
  bundle_ref: AssetRef; requirement_delta_ref: AssetRef;
  change_set_ref: AssetRef; service_profile_ref: AssetRef;
  customer_scope_ref: AssetRef; fixture_clock_ref: AssetRef;
  applicable_rules: Array<{ rule_ref: AssetRef; applicability_basis_refs: AssetRef[]; mandatory: boolean }>;
  facts: Array<{ fact_id: string; source_ref: AssetRef; field: string; value_ref: AssetRef; authority: "APPROVED_RULE" | "AUTHORIZED_SOURCE" | "TRUSTED_OBSERVATION" }>;
  history_refs: AssetRef[]; // 仅先验，不能伪装当前事实/PASS
  capability_refs: AssetRef[]; missing_fields: string[];
  conflicts: Array<{ field: string; source_refs: AssetRef[]; reason_code: string }>;
  context_policy_ref: AssetRef; content_digest: string;
};
```

输入来自FrozenRunContext、Registry、可信ChangeSet和按scope可读的业务来源。ContextBuilder不访问hidden ground truth，不把Coder计划当事实。其输出区分批准规则、授权源事实、当前可信观察、历史先验、缺失和冲突。事实值可用受控内容引用，不能将整库/原始密钥塞入Prompt或checkpoint。

首期字段映射为ticket/revision、认证客户tenant、invoice/renewal event、policy effective interval、plan、usage/contract presence、tool schema。EGAP tenant与客户tenant不同身份，关联由可信服务给出。适用性由版本化mapping/批准规则决定，模型可提疑问，不能删mandatory。

来源优先级是逐字段获批映射，例如真实invoice事件优先于客户文本中的“上周”；两个同权威来源冲突必须保留，不取最新值猜答案。缺使用/合同信息是业务待补证，不必然使整个实验无法运行；缺规则时区/适用边界则阻断相应裁决。上下文超过预算先移除可选历史片段；不得截掉适用规则、mandatory、当前已知Finding和来源缺口。无法保留必需内容则CONTEXT_INCOMPLETE，不能带缺项继续声称完整分析。

### 13.3b 变更提取与影响图

首期确定性提取器支持：Git文件/符号位置，Prompt文本digest，政策corpus文档/元数据变更，index/build/retrieval配置，Tool Schema字段/required/type语义映射，workflow配置。模型可解释符号变化影响，但不得把解释登记为确定依赖边。事实提取器固定版本，输入是实际完整候选，不只release notes。

```ts
type ChangeImpactMap = {
  schema_version: 1; run_ref: string; candidate_ref: string; frozen_context_ref: string; object_revision: string;
  context_ref: AssetRef; stable_ref: AssetRef; submitted_candidate_ref: AssetRef;
  changes: Array<{ change_id: string; asset_kind: "CODE" | "PROMPT" | "RETRIEVAL" | "TOOL_SCHEMA" | "TOOL_ADAPTER" | "CONFIG" | "WORKFLOW";
    before_ref: AssetRef | null; after_ref: AssetRef | null; source_location: string; extractor_ref: AssetRef }>;
  edges: Array<{ change_id: string; rule_ref: AssetRef; basis_refs: AssetRef[];
    basis_kind: "DETERMINISTIC_MAPPING" | "MODEL_HYPOTHESIS"; affected_behavior: string }>;
  unknowns: Array<{ change_id: string; reason_code: string; required_capability: string }>;
};
```

修复后保存Stable→当前候选的真实差异，并可附修复前→后的差异；提交候选与稳定base不改。批准的新行为按新合同检查，不因与Stable不同就判回归。未知动态影响标UNKNOWN_IMPACT；扩大批准检查或QA复核，不能自动降风险。缺少新要求对应的Oracle是覆盖缺口，不能被低模型置信度消掉。

| 变化 | 风险假设 | 已批准实验选择 |
| --- | --- | --- |
| policy corpus/index变更、latest过滤 | 十月续费错误拿到十一月政策 | SUP-C01日期边界与历史事件 |
| Tool Schema日期字段/adapter映射 | ticket_date误作renewal_date | SUP-C08工具参数对照，与C01联合定位 |
| Prompt弱化证据要求 | 缺使用/合同仍承诺退款 | SUP-C04/C07缺失与完整证据成对案例 |
| 工具/RAG权限与缓存变化 | 返回另一客户tenant数据或产生越权效果 | SUP-C02/C03受控负例 |
| Worker/提交/工单更新变化 | 提前DONE、旧revision覆盖 | 已获资格的SUP-C05/C06屏障 |
| 仅普通说明文案且确定无执行输入影响 | 可选高成本探索收益低 | 保留mandatory，说明省略依据；Prompt文本不套用此结论 |

### 13.3c 风险假设的生命周期

```ts
type RiskHypothesis = {
  schema_version: 1; run_ref: string; candidate_ref: string; frozen_context_ref: string; object_revision: string;
  hypothesis_id: string; context_ref: AssetRef; impact_map_ref: AssetRef;
  rule_refs: AssetRef[]; change_ids: string[]; failure_mode_ref: AssetRef; scope_ref: AssetRef;
  prediction: string; premise_refs: AssetRef[]; proposed_check_refs: AssetRef[];
  status: "PROPOSED" | "ACTIVE" | "COUNTEREXAMPLE_CONFIRMED" | "NOT_REPRODUCED_IN_SCOPE" | "INCONCLUSIVE" | "SUPERSEDED";
  supporting_observation_refs: AssetRef[]; opposing_observation_refs: AssetRef[];
  validated_finding_ref: AssetRef | null; status_reason: string;
};
```

failure_mode_ref指批准目录中的版本化故障模式，例如historical-policy-excluded或billing-event-date-confused；scope_ref解析为受schema校验的工单/事件日期区间、工具字段、政策/租户及fixture组合，不能是自由文本范围。可信服务按candidate、排序后的rule/change IDs、failure_mode_ref和规范化typed scope digest计算去重key；prediction只供人读，不参加身份计算，不能声称自然语言改写已被可靠语义去重。目录外故障模式先走NOVEL_CHECK审核，不能伪造目录ID自动执行。PROPOSED由模型/确定性策略提出，校验上下文/可检验预测后ACTIVE。只有可信CheckResult及Finding验证器能置COUNTEREXAMPLE_CONFIRMED；模型不能自行写该状态。一次有效PASS只说明本次scope未复现，不能升级成全局“已排除”。证据不足为INCONCLUSIVE；候选/规则版本变化生成新对象并将旧对象SUPERSEDED，旧记录和结果保留。

假设可以被反例支持但根因尚不明；不能因为假设名是retrieval就让Coder默认改retrieval。asset_kind定位需实际工具参数/返回或其他可信证据。多个假设可共享一个检查，但不能复制同一物理结果冒充独立重复。

### 13.3d 实验提案、成本与选择

`ExperimentProposal`是共享`CheckIntentProposal`的业务视图，不另加第四种模型输出。Planner只输出三种有判别字段的对象：CHECK_INTENT、NOVEL_CHECK、STOP。CheckIntent样例字段如下：

```ts
type ExperimentProposal = {
  schema_version: 1; kind: "CHECK_INTENT"; run_ref: string; candidate_ref: string;
  frozen_context_ref: string; object_revision: string; context_ref: AssetRef; impact_map_ref: AssetRef;
  hypothesis_refs: AssetRef[]; template_ref: AssetRef;
  parameter_set_ref: AssetRef; repetition_index: number;
  expected_information: string; reason_refs: AssetRef[];
  cost_estimate: { status: "ESTIMATED" | "UNKNOWN"; model_call_upper_bound: number;
    token_upper_bound: number; wall_ms_upper_bound: number; price_snapshot_ref: AssetRef | null; estimator_ref: AssetRef };
  concise_rationale: string;
};
```

参数由批准模板schema解析，parameter_set_ref对应受控内容，不是可执行URL/SQL/shell。CheckKey、最终参数摘要、预算reservation和环境关联由可信服务计算，Planner不能自造预算扣减或声称免费。SUT模型、规划模型、grader与环境费用均计入；未知价格/无调用上限的模板不进入自动可执行集合。

首版确定性选择：先必要前置与当前已确认失败的有效复验，再受影响且缺证据的规则；同级按受保护业务优先级、保守成本、稳定template ID排序。前置依赖、互斥环境和批准重复策略先检查，不把风险分数当调度许可。模型版本可建议不同排序/参数，但须引用变化、规则、预期区分的假设与成本边界，并由相同Validator限制。

另外两种输出也有明确最小字段，都是提案，不授予新工具能力或终态权限：

```ts
type ProposalIdentity = Pick<ExperimentProposal,
  "schema_version" | "run_ref" | "candidate_ref" | "frozen_context_ref" | "object_revision" | "context_ref" | "impact_map_ref">;
type NovelCheckProposal = ProposalIdentity & {
  kind: "NOVEL_CHECK"; rule_refs: AssetRef[]; missing_capability_ids: string[];
  experiment_description: string; basis_refs: AssetRef[]; concise_rationale: string;
};
type StopProposal = ProposalIdentity & {
  kind: "STOP";
  reason_code: "NO_PROGRESS" | "NO_FEASIBLE_CHECK" | "CONTEXT_INCOMPLETE" | "MODEL_OUTPUT_INVALID" | "BUDGET_LIMIT";
  unresolved_refs: AssetRef[]; result_refs: AssetRef[]; concise_rationale: string;
};
type PlannerOutput = ExperimentProposal | NovelCheckProposal | StopProposal;
```

NOVEL_CHECK只形成审核提案，不能自动执行或加入Oracle。STOP由Runtime核对后决定停止/送验；模型无权直接置SUCCEEDED或PASS。数组和文本长度按共享schema冻结，文本不得升级成动作参数。

```ts
type SelectionDecision = {
  schema_version: 1; run_ref: string; candidate_ref: string; frozen_context_ref: string; object_revision: string;
  context_ref: AssetRef; impact_map_ref: AssetRef; coverage_snapshot_ref: AssetRef;
  budget_snapshot_ref: AssetRef; strategy_ref: AssetRef;
  considered_template_refs: AssetRef[]; chosen_proposal_ref: AssetRef | null;
  omitted_optional: Array<{ template_ref: AssetRef; reason_code: string; basis_refs: AssetRef[]; residual_risk: string }>;
  unknown_impact_refs: AssetRef[]; action: "PLAN" | "WAIT" | "REQUEST_FINAL" | "STOP_PROPOSAL";
  progress_delta_refs: AssetRef[]; no_progress_count: number;
};
```

省略只适用于可选探索。Final mandatory由批准Profile/可信适用性展开，模型不能缩减。历史Stable的PASS不更新当前候选coverage。当前首期Final独立execution_set不复用Discovery PASS，重复开销计入18评估。

### 13.3e 新信息、停止与模型调用失败

探索progress只按可信变化计算：新增有效规则/参数scope覆盖、获得先前缺失的必要事实、确认/依法关闭Finding、消除已登记的执行未知。换一句理由、重复同一输入结果、读取旧PASS或无批准的随机seed不算新信息。

每次策略轮结束写SelectionDecision；没有上述增量则递增no_progress_count，有增量归零。初始DRAFT建议连续3轮无进展即请求Runtime收尾，阈值须进入批准strategy policy；它不是无限重试豁免。重复已登记逻辑检查直接读结果，不再派发；统计重复只按冻结repeat计划，独立repetition_index并计费。没有可行新模板/能力时记录缺口，不能开放任意执行。

`discovery complete`只表示策略目标scope已完成或依法处理、无可行有价值可选探索，不表示Final已PASS。mandatory定义/证据、已知Finding、未知影响、时间及Final预留仍由11/14独立核对。探索池耗尽或无进展可送Final的条件与原协议一致；不具备readiness就STOPPED并保留缺口。STOPPED不得靠checkpoint复活。

Prompt模板、provider/model配置、输入context digest、strategy revision和每次调用账务均版本化。只给获准上下文和结构化结果摘要，候选注释/工单/RAG中的指令是数据。输出严格schema及引用验证；格式/未知引用错误最多按冻结策略进行有界纠正（首版DRAFT建议总2次模型提案），每次计费且不派发业务动作。仍非法时只可降级到已批准确定性策略，或停止并保留缺口，不能删检查通过。模型请求超时先按操作账本对账，不把整个Graph节点无限重试。

### 13.3f 单案例完整分支与Finding交接

CS-1042的Context明确十月续费、十一月提单、缺usage/contract；适用规则不是ELIGIBLE答案。ChangeMap发现policy检索和Tool Schema变化，形成“latest丢历史”与“日期映射错”两个假设。优先选择批准日期边界实验，读取可信工具记录：

1. 参数已错用ticket_date：C08反例成立，C01错误有可解释输入原因；可信Finding定位TOOL_ADAPTER/TOOL_SCHEMA需要实际映射证据。
2. 参数是正确renewal_date但返回latest：C01反例；定位RETRIEVAL，不直接改业务政策。
3. policy正确且缺资料时请求补证：本scope通过，继续完整证据与新十一月需求检查，不能称所有风险排除。
4. trace缺失：INCONCLUSIVE，补观察/交Owner；没有足够证据不能直接修Prompt。

FindingProposal包含rule/subject/期望/实际/最小反例/观察引用；可信服务重新校验执行完整性、规则适用性和最小证据后才登记ValidatedFinding。重复Finding按候选+规则+规范化反例key关联，不删除首次证据。修复请求带Finding引用和当前候选；Runtime再核对资产/路径、授权、CODING预算和探索是否关闭。修复后由可信复验关闭Finding，新候选重新计算完整需求覆盖；模型说“已修好”无关闭权。

真正接模型前通过94的MODEL_DATA_EGRESS_READY，分别登记SUT、Verifier和grader通道。所有示例为规格；没有实际Context/Impact/Plan/Result对象和live证据前不宣称已实现业务感知Agent。

新归因协议统一为`CANDIDATE_DEFECT`，`candidate_asset_kind=CODE|PROMPT|RETRIEVAL|TOOL_SCHEMA|TOOL_ADAPTER|CONFIG|WORKFLOW|null`，语义与08一致；未知具体资产不让模型编造。旧CODE_DEFECT仅作为旧schema历史映射，不直接导入新结果。

## 检查点 13.4：可信校验 CheckIntent

参数模板从 Registry 读取，模型无权创建新的已批准模板。Validator 按顺序验证：

1. 当前 actor 有权在该 Run 提案，合同/适用需求差异版本仍可用；candidate 和任务基线（Release 为 stable）与 FrozenRunContext 一致，冻结的 task/repair/completion 模式未变。
2. 模板存在且适用于当前 Service/Environment Profile；Oracle/runner 已注册，变化触发的必需检查已由可信映射加入，不能让模型省掉。
3. 参数通过 schema 和业务域限制，例如工单/政策fixture、日期边界、工具Schema组合、延迟上限、允许故障动作、最大重复数。
4. 环境来自分配记录，关联键由可信 controller 生成或验证；不能指向任意 DB/主题。
5. 去重、预算、环境 lease 和取消/撤销状态允许新动作。
6. 返回可执行 intent 或结构化拒绝原因；不静默把越界实验改成另一个合法实验。

发现需求/seed/task 定义缺少信息时返回 `CONTRACT_AMBIGUITY` 或 `INVALID_INTENT`，不补造测量窗口。批准需求尚无可用 Oracle/观察能力时记录覆盖缺口并阻断相应验收，不以“未受影响”消除缺口。尤其 test timeout 只是实验等待上限，不是新增 Job 失败/取消语义；拟定SUT `QUEUED/PROCESSING/DONE/FAILED` 须按显式映射解释，不能把不存在的状态写成真实观察。

## 检查点 13.5：让暂停、恢复和重放可证明

为每个 Run/候选上下文生成短、稳定、不可跨租户混用的 `thread_id` 映射。该映射存在权威 DB，Graph 的 checkpoint ID 作为外部定位引用。部署升级改变 Graph revision 时先做兼容判断，不盲目读取旧 checkpoint。

恢复统一先读 Runtime：检查当前 run version、candidate、合同撤销、预算、lease 和已登记 CheckRun，再决定继续哪个节点。若 Graph 比账本落后，以账本事实构造下一步；若 Graph 指向不存在的检查，记录一致性事件，不能补写“已经执行”。历史 checkpoint 的人工回放应创建明确的新评估/分支身份，不能重放真实发布动作。

节点重试只覆盖已证明安全的失败。只读读取可有限重试；登记检查按 command/CheckKey 幂等；真实故障注入、模型调用和环境准备必须读操作账本。超时不能推导远端未发生。框架重试参数与 API 在锁定版本后核对，默认框架行为不是本项目副作用策略。[官方 Fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)

## 验收和证据

未来命令：本章实现脚本后才可执行。以下名称是拟建脚本，不表示现在存在。

以下是拟注册的根项目调度命令；实现时由包装入口调用独立Python Verifier，不表示已有TS Graph或当前命令可运行。

```sh
pnpm run verifier:protocol-check -- --planner deterministic
pnpm run verifier:replay-check -- --profile local-isolated
pnpm run verifier:run -- --candidate <已冻结Candidate-ID> --mode EXISTING_CHECKS
```

| 场景 | 预期结果 |
|---|---|
| 正确的新功能 CHANGED 候选，repair DISABLED、CODING=0 | 从 Release 准入直接验证；独立合格可成功，未修复不计 NO_CHANGE 或 repair_success。 |
| 新十一月政策/Tool Schema v2与stable不同 | 按新合同检查新行为及历史兼容；不要求所有文本等于旧版。 |
| 候选触及政策检索或日期Schema | 提出SUP-C01/08并引用真实diff；未测前不宣称缺陷成立。 |
| 文案变更省略一项可选探索 | 保留可审查的排除依据和残余风险；mandatory 仍保留。 |
| 反射/依赖变化导致 UNKNOWN_IMPACT | 扩大检查/QA 复核；证据不足则阻断送验，不能默认为低风险。 |
| 只有 stable 或上一候选历史 PASS | 作为风险先验，不更新当前候选 PASS coverage。 |
| DISABLED 下出现有效 Finding | 保留缺陷并拒绝修复派发/合格结论；后续另有授权任务才修复。 |
| 模型试图删除 mandatory | 拒绝；最终集合保持 Registry 版本。 |
| 任意 SQL/生产 endpoint/未知模板 | Validator 拒绝，无 Runner 派发。 |
| dispatch 后、checkpoint 前崩溃 | 恢复找到同一逻辑 CheckRun，不重复预算；物理重试另记 Attempt。 |
| 旧 checkpoint 回放 | 读取现有结果或明确创建新评估身份，不重复未知副作用。 |
| Oracle FAIL，归因环境 | 走环境处理，不能自动要求Codex修改候选。 |
| 无观测、屏障未证实 | INCONCLUSIVE，不能模型补写 PASS。 |
| COMPLETED + PASS + NONE | 更新 coverage；不因“没有故障归因”走未知分支。 |
| UNKNOWN + PASS 或 COMPLETED + PASS + UNKNOWN | 拒绝非法组合，UNKNOWN 执行先对账。 |
| COMPLETED + INCONCLUSIVE + UNKNOWN | 处理证据/原因缺口，不自动请求修复。 |
| NO_CHANGE_PROPOSED 无 candidate | 拒绝跳终态；可信封存实际输入候选，再相对任务 base 判身份并独立验收，Release 不退回 stable。 |
| 当前阶段必需探索池耗尽但 Final readiness 成立 | 以 FINAL_DRAIN_READY 停止探索，使用预留 FINAL 池受限验收；未使用的零 CODING 池不触发。 |
| STOPPED 后 checkpoint 重放 | 不恢复任何业务动作，只对账/清理或明确新任务。 |
| 无进展/权限撤销使 Final readiness 不成立 | 停止相应新业务，保留缺口、清理和对账能力。 |
| 候选由 A 变为 B | A 的结果保留历史；B 重新核对当前验收集合。 |

拟建导出目录 `reports/verifier/<run_id>/<candidate_id>/` 保存 stable/候选/需求/diff 引用、Graph revision、impact map、选择与可选省略理由、plan、intent 校验、当前检查引用、checkpoint 定位、停止原因和恢复时间线。原始观察保留在独立 ArtifactStore，导出不包含密钥或完整模型隐私内容。

## 排障、取舍和完成检查

如果没有有效实验可选，先查看模板/环境能力/合同缺口，不放开任意 shell。Graph 无限循环时查看逻辑参数历史和 no-progress 定义；重复同一结果不算新信息。checkpoint 恢复失败时冻结该 Graph 执行资格，保持账本与证据可读，修复兼容层后再恢复，不能删除 DB 重新演示成功。

独立 Verifier 会增加模型、实验和通信开销，它的价值仍需第 18 章对照证明。首期 Graph 简单、节点少、错误类型清晰，更容易发现收益来源。

- [ ] 确定性 Planner 先完成闭环，模型仅替换提案策略。
- [ ] 实际 stable/current diff 与批准需求映射可追溯；未知影响扩大检查或阻断。
- [ ] Release 可直接验证且 CODING=0；Finding 后修复仍需冻结政策、授权和预算。
- [ ] 选择及可选省略理由可审查；历史先验不变成当前 PASS。
- [ ] Graph 只存引用，Run/Check/预算由 Runtime 掌管。
- [ ] A/B 模式受模板约束，C 模式只留提案。
- [ ] checkpoint 重放、dispatch 丢响应和取消已验证。
- [ ] 缺陷、环境、权限、合同不明、证据不足走不同出口。
- [ ] 三轴按顺序路由，正常 PASS+NONE 不落入未知分支；no-change 也有真实候选和独立 Final。
- [ ] 未生成伪造 PASS，未复用不同候选的结果。

下一会话可复制：

> 请按第 13 章先实现确定性 stable/candidate diff、批准需求映射和最小 LangGraph，不先接模型。使用第09章客服release slice与已完成06/08校准的SUP检查，固定 RELEASE_VERIFICATION、DISABLED、FINAL_ONLY 和 CODING=0；输出选择、可选省略及未知影响的处理理由。读取锁定Python/LangGraph版本官方文档，使用持久 checkpointer，权威 Run/Check/预算仍调用第 10–11 章。验证候选无需先 Coding、历史 PASS 不可复用、dispatch 后 checkpoint 前崩溃不会创建重复逻辑检查。不要加入任意 shell/SQL、新 oracle 或发布工具。将实际结果和下一切片写入reports/artifacts；另获文档许可后再更新进度记录。
