# 13 · 用 LangGraph 规划实验，保持事实和裁决独立

实施状态：`NOT_STARTED`。本章所有模块、Graph 和脚本均为拟建；没有运行模型或产生业务验证结果。

前一章：[12 · Codex Coding Worker](12-codex-coding-worker.md)。下一章：[14 · Final Gate 与证据](14-final-gate-and-evidence.md)。依赖 [06 · Contract Registry](06-contract-registry.md)、[07 · Observer 与 Experiment Barrier](07-observer-and-experiment-barrier.md)、[08 · 原生 Oracle](08-native-oracles.md) 和第 10–11 章的持久化与预算。

## 目标与边界

本章完成一个可恢复的版本验证规划器：以稳定版本与待发布候选的真实变化、批准的新需求和既有不变量为输入，分析风险，从批准目录中选择检查，接收独立执行结果，再决定继续探索、按授权请求修复或进入最终验收。主入口先验证已有新功能候选；只有有效 Finding 才可能触发 Coding。它应能解释“变化影响了什么、为什么测这些、为什么省略其他探索”，但不能自行写出可信 PASS。

验证独立性由受保护的规则、执行身份、环境、观察来源和证据写权限共同建立。Python Verifier可以选择实验、质疑结果和提出Finding；不能改业务代码、修改已批准业务真相或批准自己的发布。Coder可按scope编写开发测试，但其报告只能作为开发反馈；换一个LLM或另起同权限子进程不足以取得独立Oracle资格。首次模型接入前，先以确定性Runner/Oracle和坏观察器测试验证这条信任边界。

先用确定性 Planner 完成 Graph 与账本对接，再接入模型做风险解释和实验排序。否则一次失败会同时混合 Graph 状态、模型输出和测试基础设施，无法定位原因。模型可提出假设；Runner/Observer 提供事实；Oracle 裁决断言；Final Gate 判断覆盖与放行资格。

## 为什么用 LangGraph，但不把它变成第二个 Runtime

LangGraph 适合表达有分支、循环、暂停和恢复的验证策略。项目在本次 ML 业务切换中改为 Python LangGraph 独立进程；TypeScript 控制面继续拥有任务、预算和结果登记。双方共用版本化 JSON Schema，并生成/校验各语言 DTO，不能直接依赖 TS 类型作为网络协议。Python/LangGraph 版本在实现时按兼容性锁定，目前未安装或运行该 Verifier。

官方将 checkpointer 定义为线程状态持久化机制；内存 saver 重启会丢失。checkpointer 与跨线程 store 的用途不同。[官方 Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

重放会重新执行后续节点，包括其中的模型/API 调用。因此，checkpoint 不是副作用恰好一次的保证；新线程的业务准入也必须先写入项目账本，不能以“查不到 checkpoint”推断任务从未被接受。[官方 Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)

本项目据此规定：PostgreSQL 的 Run/CheckRun/EffectOperation/BudgetReservation 是权威事实；Graph 存其引用和规划摘要。数据库事务先登记请求与 outbox，再触发 Graph。Graph checkpoint 的 PostgreSQL 存储使用独立表/权限，不与领域记录混为一张状态对象。不要把 checkpointer 的某个 durability 选项当成分布式事务。

## 拟建模块

| 拟建路径 | 职责 |
|---|---|
| `services/verifier/egap_verifier/graph.py` | Graph 拓扑、分支和停止出口。 |
| `services/verifier/egap_verifier/checkpointer.py` | 固定版本 saver 接入与生命周期管理。 |
| `services/verifier/egap_verifier/graph_state.py` | 仅引用和必要摘要的 GraphState。 |
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
| `PARAMETERIZED`，对应 v3 模式 B | 在已批准模板内选已批准图片 fixture、schema/model 组合、延迟、seed 等。 | 参数域、资源、环境、重复次数和预算。 |
| `NOVEL_PROPOSAL`，对应 v3 模式 C | 提出目录外检查或观察需求。 | 首期仅记录提案，不自动执行，不升为 release oracle。 |

首期只实现前两种自动执行路径。新实验需要新的模板、独立评审、正反校准和 Profile 版本，不能因为模型产生了一段测试代码就自动加入 Final。

以下用 TypeScript 记法展示跨语言 GraphState 协议投影；实际 Python DTO 由共享 JSON Schema 校验，字段名在实施时统一固定：

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

Release 从已接收的外部候选开始：第 10 章在 BASELINING 核对资格，通过 `RELEASE_CANDIDATE_ACCEPTED` 进入 DISCOVERY_VERIFYING，不要求先调用 Coding。先用确定性规则按第 06/08/09 章批准映射选择 ML-01…ML-06 中批准且具备观察能力的检查及本次 release slice 的功能/兼容断言；ML-07 未获数据与阈值批准时明确不具备模型质量资格，再逐步接模型。下面是项目伪代码，不是可以复制执行的 LangGraph API：

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
           CODE_DEFECT + validated finding + repair authorized + exploration open + CODING budget
                         -> RequestRepair -> end current candidate loop
           CODE_DEFECT + repair disabled/not authorized -> retain finding; no Coding or acceptance
           CODE_DEFECT + required CODING budget exhausted -> Runtime stop decision; no Final readiness
           CODE_DEFECT + exploration_closed -> retain Final failure; no new Coding/Discovery
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

先 execution_status、再 assertion_status、最后 attribution 的顺序不可省略。`NONE` 仅用于 COMPLETED+PASS；null 仅用于 NOT_EVALUATED 且尚未归因，失败/不确定而原因未知才用 UNKNOWN；FAIL/INCONCLUSIVE 不得为 null/NONE，完整真值表见第 08 章。禁止单独匹配 `attribution=UNKNOWN` 就把正常结果送去对账，也禁止 execution UNKNOWN 因带 CODE_DEFECT 字样而直接修复。非法组合由 Validator 拒绝，不能由模型猜测修正。

预算选择使用冻结的 `budget_pool=CODING|DISCOVERY|FINAL|RECONCILIATION|CLEANUP`。`repair_policy=DISABLED` 的 Release 可将 CODING 配额设为 0，不能因此停止 DISCOVERY 或 Final。只有当前阶段实际必需的探索池耗尽才触发第 11 章收尾判断。`final_readiness` 是当前候选的送验判断及引用，不是 Final PASS 或新增 Run 状态；需冻结身份、有效合同/权限、环境/观察能力、无当前已知代码 Finding、无阻断未知和足够 FINAL/MANDATORY 预留，具体事务以第 11/14 章为准。触发耗尽时 Runtime 先原子关闭探索派发并设置 `exploration_closed=true`，满足条件才以 `FINAL_DRAIN_READY` 事件进入 FINAL_VERIFYING。存在可有界确认的未知时仅按第 11 章先对账，决策截止前仍不满足则 STOPPED。收尾期间发现新失败也不能重新 Coding/Discovery，只保留失败并按政策终结。不得先标 STOPPED 再恢复业务。已经 STOPPED/CANCELLED 的 Run 只允许对账与清理，不能用 Graph replay 重启 Coding/Discovery/Final。

第一轮完成标志：不用模型也能走完“计划→持久检查→可信结果→分支”，并能在每条边看到关联 ID 和拒绝原因。

## 检查点 13.3：增加变更影响与模型 Planner

先做可信的输入和映射，再加入模型建议：

1. 校验 stable manifest、submitted/current candidate manifest 的 commit/tree 与完整输入 digest，保存 diff 工具版本和来源摘要。读取真实源码、配置、依赖解析和批准 schema 的差异，不能只读取 release notes。`release_changeset_ref` 对应提交时的变化；修复后 impact map 必须另含 stable→当前候选的真实差异，保留提交版本引用。
2. 提取 ChangedSymbol 及文件/行位置、接口/数据流/依赖边；将其映射到 `requirement_delta_ref` 的批准变化和既有不可破坏的不变量。批准的新行为按新合同检查，不能仅因与 stable 不同就报回归；没有需求依据的行为变化不能由模型补写批准理由。
3. 建立“变更→受影响行为/合同规则→风险假设→批准检查/观察能力→当前覆盖缺口”的映射。每个条目保留来源、映射规则版本、确定性事实与模型假设的区别。路径没变不等于行为没受影响；依赖/配置变化同样进入分析。
4. 输出选择记录，逐项说明所选检查解决哪个风险、参数与成本边界，以及未选择的**可选探索**为何省略、依据和残余风险。缺少证据时标未知，不能用模型低风险评分代替排除证据。强制项只允许执行或显式阻断，不存在模型省略理由。

首个主展示采用第 09 章自有衍生版本 Stable V3.4→Candidate V4.0；不是上游版本或生产运行史。Stable 先建立按提交冻结的模型/预处理 manifest 与可信观察；V4 增加模型 B、预处理/schema v2、批准的 v1 兼容和 Worker 重构。模型注册可先用受控本地 manifest，MLflow/Feast 不是首期依赖。拟建映射：

| 实际变化与批准要求 | 风险/覆盖目标 | 选择与省略依据 |
|---|---|---|
| 模型 B/可变 alias 解析或 Worker 领取路径变化 | 排队期间别名切换造成实际模型与提交绑定不一致 | 选 ML-02：受控暂停领取、提交 A、切换别名 B、恢复；核对提交 manifest 与实际加载/结果摘要，不能只比较日志版本字符串。 |
| 结果写入与 Job 完成顺序重构 | 已宣布成功却无完整持久结果 | 选 ML-01 结果写入屏障与完整状态轨迹；观察不充分为 INCONCLUSIVE，不从轮询没看到推导“从未发生”。 |
| 输入预处理/schema v2、新模型输入维度变化 | 旧 v1 兼容破坏或预处理版本错配 | 选 ML-03/ML-06：批准 fixture/snapshot、形状/顺序/预处理摘要与 v1/v2 矩阵；新模型预测允许按需求变化，不要求等于旧模型。 |
| 队列重领、确认或失败处理变化 | 重复结果、旧 Worker 覆写、新 Worker 无法恢复 | 选 ML-04/ML-05 的已校准重复/崩溃窗口；平台 Runtime 恢复不替代 SUT Worker 恢复。 |
| 仅说明文案变化且无执行/配置路径变化 | 经确定性映射确认业务状态影响低 | 保留适用 mandatory；省略无关联高成本探索并保留依据。文案属于模型输入或可执行配置时不套用本结论。 |

首期检查分层由批准 Profile 决定：当前候选 mandatory smoke/safety、变更功能检查、受限参数探索、输入/结果 schema 兼容，以及 release tier 触发的更广回归。ML-01…ML-06 是待校准的业务规则目录，不因被 Graph 引用就已经批准；每项缺能力时阻断该范围，不能删 mandatory。ML-07 另需冻结评估集、标签/切片、统计方法、性能环境与阈值；先实现版本正确不表示新模型质量足以发布。


稳定版本历史证据可帮助定位已知边界、历史缺陷和已有测试能力；没有必要每次重跑 stable 的全部历史测试。只有比较性实验确需当前对照、基线身份/资格存疑或批准策略要求时才安排 stable 重放并单独记账。首版不把 historical coverage 或任何其他候选的结果当作当前 candidate PASS，也不替代 Final 的当前 mandatory。

首期不声称完整 Python/ONNX 程序证明。动态导入、生成代码、模型算子、依赖升级或映射缺失标为 `UNKNOWN_IMPACT`：按批准风险策略扩大检查/人工 QA 复核并留痕；可用检查或预算不能消除妨碍验收的不确定性时阻断送验。未知影响不能转成“未受影响”或静默减小 Final 集合。

随后将批准规则、ChangedSymbol、需求差异、历史先验、**当前候选**结果摘要、剩余预算和实验模板交给 Planner。只要求输出可审查假设和简短理由，不要求内部思维链。模型建议由可信映射/Validator 执行，不能提升历史证据的权威等级。

接真实 Planner 前核对 [94 · 数据流转、区域与保留](94-data-flow-and-retention.md) 的 MODEL_DATA_EGRESS_READY，单独登记 Verifier 实际 provider/账号/通道；不能继承 Coding 同品牌的区域或保留结论。提示词、模型输出、工具事件和 Graph checkpoint/store 均列入实际流向；若框架保存了全文，应按实际字段登记而非声称“只存引用”。公开源码/合成演练可按限定等级准入；真实企业内容另需 ENTERPRISE_DATA_READY。这里只定义实施前核验，不表示当前已有数据批准。

Planner 的输出类型应只允许 `CheckIntentProposal`、`NovelCheckProposal`、`StopProposal` 三类。任何自由 shell、SQL、URL 或生产 endpoint 字符串都不能绕过模板变成动作。候选源码、注释和日志中的“忽略某项检查”属于数据，不改变工具定义。

拟建工具最小集：`get_contract_bundle`、`get_change_set`、`get_check_status`、`submit_check_intent`、`get_observation_summary`、`propose_finding`、`request_repair`。每个调用携带受认证上下文，服务端重新判断 tenant/run/scope；模型不能通过 body 自选租户或环境。

排序先使用可解释基线：已证实失败重放优先，其次受影响且未覆盖的高风险规则。以后可以采用风险×未覆盖×预计信息增益÷成本的启发式，但它仅影响探索顺序；权重、版本和输入记录在 plan，不能以模型信心修改 Final mandatory。

## 检查点 13.4：可信校验 CheckIntent

参数模板从 Registry 读取，模型无权创建新的已批准模板。Validator 按顺序验证：

1. 当前 actor 有权在该 Run 提案，合同/适用需求差异版本仍可用；candidate 和任务基线（Release 为 stable）与 FrozenRunContext 一致，冻结的 task/repair/completion 模式未变。
2. 模板存在且适用于当前 Service/Environment Profile；Oracle/runner 已注册，变化触发的必需检查已由可信映射加入，不能让模型省掉。
3. 参数通过 schema 和业务域限制，例如输入图片尺寸/大小、schema 与模型组合、延迟上限、允许故障动作、最大重复数。
4. 环境来自分配记录，关联键由可信 controller 生成或验证；不能指向任意 DB/主题。
5. 去重、预算、环境 lease 和取消/撤销状态允许新动作。
6. 返回可执行 intent 或结构化拒绝原因；不静默把越界实验改成另一个合法实验。

发现需求/seed/task 定义缺少信息时返回 `CONTRACT_AMBIGUITY` 或 `INVALID_INTENT`，不补造测量窗口。批准需求尚无可用 Oracle/观察能力时记录覆盖缺口并阻断相应验收，不以“未受影响”消除缺口。尤其 test timeout 只是实验等待上限，不是新增 Job 失败/取消语义；上游 `queued/processing/done/failed` 须按显式映射解释，不能把不存在的状态写成真实观察。

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
| 批准的模型B/预处理v2新行为与stable不同 | 按新合同选ML-02/03/06对应检查；预测变化本身不是回归，质量判断另需ML-07。 |
| 候选触及模型解析或结果完成顺序 | 提出ML-C02或ML-C01，引用真实diff和制品变化；未测前不宣称缺陷成立。 |
| 文案变更省略一项可选探索 | 保留可审查的排除依据和残余风险；mandatory 仍保留。 |
| 反射/依赖变化导致 UNKNOWN_IMPACT | 扩大检查/QA 复核；证据不足则阻断送验，不能默认为低风险。 |
| 只有 stable 或上一候选历史 PASS | 作为风险先验，不更新当前候选 PASS coverage。 |
| DISABLED 下出现有效 Finding | 保留缺陷并拒绝修复派发/合格结论；后续另有授权任务才修复。 |
| 模型试图删除 mandatory | 拒绝；最终集合保持 Registry 版本。 |
| 任意 SQL/生产 endpoint/未知模板 | Validator 拒绝，无 Runner 派发。 |
| dispatch 后、checkpoint 前崩溃 | 恢复找到同一逻辑 CheckRun，不重复预算；物理重试另记 Attempt。 |
| 旧 checkpoint 回放 | 读取现有结果或明确创建新评估身份，不重复未知副作用。 |
| Oracle FAIL，归因环境 | 走环境处理，不能自动要求 Codex 改业务。 |
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

> 请按第 13 章先实现确定性 stable/candidate diff、批准需求映射和最小 LangGraph，不先接模型。使用第 09 章 release slice 与已完成第06/08章校准的ML检查，固定 RELEASE_VERIFICATION、DISABLED、FINAL_ONLY 和 CODING=0；输出选择、可选省略及未知影响的处理理由。读取锁定Python/LangGraph版本官方文档，使用持久 checkpointer，权威 Run/Check/预算仍调用第 10–11 章。验证候选无需先 Coding、历史 PASS 不可复用、dispatch 后 checkpoint 前崩溃不会创建重复逻辑检查。不要加入任意 shell/SQL、新 oracle 或发布工具。将实际结果和下一切片写入reports/artifacts；另获文档许可后再更新进度记录。
