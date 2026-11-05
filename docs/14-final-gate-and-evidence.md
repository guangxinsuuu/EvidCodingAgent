# 14 · 把独立证据汇总成确定性的 Final Gate

实施状态：`NOT_STARTED`。本章为待实现规格；没有真实 CheckResult、VerificationBundle 或 Final 通过记录。

前一章：[13 · LangGraph Verifier](13-langgraph-verifier.md)。下一章：[15 · 安全与平台资格](15-security-and-qualification.md)。先完成第 06–08 章的批准合同、可信 Observer、Oracle，以及第 10–11 章的持久检查和预算。本章不依赖尚未创建的本 Run PR 或 CI。

GenAI Final绑定完整release manifest：代码/依赖、provider/model配置、Prompt、corpus/index/retrieval、Tool Schema/adapter、输出schema及workflow。独立模型/工具记录核对实际请求/返回与manifest，远端model ID不等于权重冻结。批准合同、Oracle/grader、数据/重复与阈值属于独立VerificationContext。首期Gate只声明待审草稿及批准硬规则范围，语义报告不签发自动对客资格。

## 目标：任何“通过”都能追到同一份候选和真实执行

Final Gate 的输入是冻结合同、候选和可信执行记录；输出是可解释的确定性决定。模型可以解释报告，但不能决定缺少哪项证据可以忽略，也不能签发 PASS。一次演示应包含两条路径：当前候选全量适用 mandatory 完成；以及只替换候选、删掉一个原始证据或撤销合同，就会立即失去相应资格。

WorkOrder/Run 准入时冻结 task_mode、repair_policy 和独立的 `completion_mode: FINAL_ONLY | DRAFT_PR_CI`，并写入 FrozenRunContext。`FINAL_ONLY` 不请求审批、发布或 CI；有变更的 `DRAFT_PR_CI` 才继续后续外部交付。是否调用过 Codex 不构成验收或发布前置条件。模式/策略不允许在运行中修改；对已完成的 FINAL_ONLY 候选追加发布，需要新建有明确授权的 DRAFT_PR_CI 任务并重新准入、验证，不能直接翻转原 Run。

本章有三种时间不同的判断：

| 判断 | 发生时点 | 证明什么 |
|---|---|---|
| 候选 Final | 当前候选冻结后、提交人审前 | 该候选符合指定 Acceptance Profile 的业务要求。 |
| 平台 qualification | 第 15 章独立任务；发布前须有效 | 指定 Runtime/Runner/Gateway/策略版本具备适用安全、恢复能力。 |
| 发布后 CI | 第 17 章真实 PR 创建以后 | 指定远端 head/base 的 CI 事实。 |

候选 Final 不能要求“本 Run 的 PR CI 已通过”，否则尚未允许发布却要求发布后证据，形成循环。申请真实发布时，业务 Final 通过也不能绕过尚未通过的平台 qualification。完整发布资格在第 15–17 章组合，当前章节先建立可靠的候选判断器。

## 为什么不用模型评分或一个 testsPassed 布尔值

`exit_code=0` 只说明某进程正常退出；“Job FAILED”可能是批准的非法输入场景结果；“DONE + NEEDS_MORE_EVIDENCE”也可以是合法补证草稿；“执行完成”也可能得到 INCONCLUSIVE。三条轴必须保留：

| 轴 | 值 | 例子 |
|---|---|---|
| `execution_status` | QUEUED/RUNNING/COMPLETED/ERROR/CANCELLED/UNKNOWN | runner 已完成，但观察不完整。 |
| `assertion_status` | PASS/FAIL/INCONCLUSIVE/NOT_EVALUATED | 屏障是否生效未知，因此不能判提前DONE。 |
| `attribution` | NONE/CANDIDATE_DEFECT/ENVIRONMENT/ACCESS/CONTRACT_AMBIGUITY/UNKNOWN，未进入裁决时可 null | COMPLETED+PASS 必须 NONE；未知故障原因才用 UNKNOWN。 |

先检查 execution_status，再检查 assertion_status，最后使用 attribution 路由，合法组合以第 08 章真值表为准。执行 UNKNOWN 先对账，不接受其中声称的 PASS；断言 INCONCLUSIVE 进入证据/合同缺口处理，不自动修复候选。正常结果必须 COMPLETED+PASS+NONE，不能以 UNKNOWN 代表“无故障”；null 仅用于 NOT_EVALUATED 且尚未归因，FAIL/INCONCLUSIVE 不得为 null/NONE。PASS 的成功路径还必须绑定完整身份、适用范围和可信采集。签名只能证明某身份提交了数据，不能证明 Oracle 语义正确；Oracle 仍要在第 08 章通过 healthy/seed/不确定观察对照。

旧 AAA 的 [kafka-validation.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/harness-worker/src/kafka-validation.ts>) 将 worker-owned 验证与候选分开，可用来学习独立验证接口；但其中 fixture 消费者状态和检查数量不能当成真实客服Job、政策/工具调用、授权效果与草稿提交证据。v3 的[候选证据标准与首批检查说明](../../EGAP-Order-Approval/docs/eventuate-production-plan-v3.md)是本章设计输入，具体 DTO 和 Gate 仍需实现。

## 拟建文件

| 拟建路径 | 职责 |
|---|---|
| `packages/contracts/src/check-result.ts` | 运行结果 DTO 与三轴状态。 |
| `packages/verification/src/evidence-binding.ts` | 核对 candidate/bundle/check/attempt/environment/collector。 |
| `packages/verification/src/coverage.ts` | 从冻结 Acceptance Profile 展开适用 mandatory 闭包。 |
| `packages/verification/src/final-gate.ts` | 纯决定函数，返回原因码和引用。 |
| `apps/control-plane/src/finalization-service.ts` | 读取权威快照、事务复核和登记决定。 |
| `packages/artifacts/src/evidence-reader.ts` | 对象存在性、摘要、读权限、保留状态核查。 |
| `packages/verification/src/report.ts` | 从同一结果生成机器 JSON 和中文人读报告。 |
| `tests/integration/final-gate.spec.ts` | 缺证据、错误版本、适用性、撤销和并发变更。 |

Gate 不是新的独立微服务；放在模块化控制面中，保持纯判断与事务外壳分离，便于测试和审计。ArtifactStore 的写入与读取权限仍是独立安全边界。

## 检查点 14.1：补全证据谱系

先实现下面的绑定关系，每个箭头都要能查到对象和摘要：

```text
WorkOrder + FrozenRunContext
          -> CandidateManifest
          -> CheckIntent -> logical CheckRun
                         -> physical CheckExecutionAttempt
                         -> ObservationBundle -> CheckResult
          -> Coverage + ValidatedFinding
          -> VerificationBundle -> GateDecision
```

CheckResult中的`candidate_asset_kind`遵循08：CODE/PROMPT/RETRIEVAL/TOOL_SCHEMA/TOOL_ADAPTER/CONFIG/WORKFLOW；CANDIDATE_DEFECT且定位有证据时填写，其他情况null。旧CODE_DEFECT需显式版本映射，不改历史。Finding关闭以可信复验/适用性判定为准，不接受模型自称修复。

每份 CheckResult 至少包含 tenant/run、candidate/tree、ContractBundle、check/template/oracle/runner 版本、参数摘要、seed、execution_set_id/phase/repetition_index、环境 Profile revision、实际 EnvironmentInstance、fixture reset、execution_attempt、lease epoch、collector 身份、原始观察引用、三轴状态、清理结果和生成时间。真正的执行身份由 runner/collector 认证上下文核验，不能只相信上传 JSON。

WorkOrder/FrozenRunContext 还绑定独立的 `task_mode=RELEASE_VERIFICATION|INCIDENT_REPAIR`、`repair_policy=DISABLED|ON_VALIDATED_FINDING` 与 completion_mode。Release 保留 `stable_release_manifest_ref, submitted_candidate_ref, release_changeset_ref, requirement_delta_ref, historical_coverage_refs`；当前 Candidate 可直接来自可信外部接收，不要求存在 CodingAttempt。若曾修复，每次 Attempt 的 repair_input_candidate_ref 和新 Candidate 的 parent_candidate_id 都进入谱系，保留提交候选原本的功能变化。

逻辑 CheckKey 的规范化由第 10 章统一实现，必须包含 `execution_set_id`、`phase`、`repetition_index`，并保留 `environment_instance_id` 和 `fixture_reset_id` 等实际环境身份。不同 Discovery/Final 执行集合及新的统计重复拥有不同逻辑键。只有这些冻结身份不变且重试安全时，才在同一 CheckRun 下增加 physical attempt；若更换环境实例或重置 fixture，则建立新的 CheckRun，通过 `retry_of_check_run_id` 关联来源。不能把新 seed 当作原尝试、把新环境冒充旧环境，或删除首次失败只留下最后绿色结果。

证据复用按 Acceptance Profile 明确规则。Stable 历史覆盖供变更风险、已有检查能力与必要基线资格核对使用，不要求每次重跑全部 Stable 历史测试；它不能证明当前 Candidate 正确。首期当前候选 Final 使用专门的 Final execution_set，不把 Discovery 结果改名为 Final，不跨 Run/candidate 复用 PASS。以后若引入复用，必须另行批准等价条件和复用策略，保留原 CheckRun/attempt 与环境/reset 身份，不删除 CheckKey 字段来提高命中率。新实例的可重复重建可支持新的执行，不会让它变成原实例的同一份证据。

对 `DRAFT_PR_CI`，CandidateManifest 必须在 Final 前具备经可信组件核验的 `candidate_commit_sha`、`candidate_tree_sha`、`base_commit_sha`、真实 Git parent/父列表和对象包摘要。Gate 核对对象闭包、完整父链、封存文件与批准 base，并核对 Runner 实际使用该树。Release 的 base 可以是批准祖先，不要求直接 parent=base；`parent_candidate_id` 是候选谱系，不能替代 Git 父链。缺少这些字段不能先放行文件包、发布时再补造 commit；返回 `PUBLICATION_CANDIDATE_NOT_SEALED`。真正 UNCHANGED 的候选保留 base commit/tree 及其真实 parent，不制造空 commit。

所有模式的 change_kind 都由第 12 章可信接收/冻结器根据完整输入判定。`base_commit_sha/base_input_digest` 固定指任务比较基线：Release 为稳定版本，Incident 为原故障/输入版本；不同于每次修复的 repair_input_candidate_ref。只有 `base_input_digest=candidate_input_digest` 才能认定 UNCHANGED，且仍须当前独立 Discovery/Final 满足需求。模型 NO_CHANGE_PROPOSED、null snapshot、没有 CodingAttempt、旧 baseline PASS 都不能替代这个判断。平台未修改的外部新功能候选仍可为 CHANGED；源码相同但构建、依赖或配置改变也不能认定 UNCHANGED。

输入 digest 不同而 Git tree 相同，不可自动视作可发布 CHANGED。未授权、不可复现或无法映射到批准 Git 发布包的构建输入漂移阻断接收/Final 发布资格；代码修复 scope 的 CHANGED 发布必须有真实授权 Git 变更，不能制造空 commit/PR。Prompt、检索配置或工具Schema若在获准Git发布包中发生真实变更，可作为CHANGED候选；仅供应商侧模型漂移/远端配置变化不能制造空commit，须另立适用任务和交付scope，不隐含纳入CREATE_DRAFT_PR。

## 检查点 14.2：展开首批 mandatory，而不是凭编号猜等价

SaaS Support Final以批准Acceptance Profile的适用闭包为准。目录不等于已实现/批准；最小scope与后续扩展分别取得资格：

| 规则 | Final必须核对 | 范围边界 |
| --- | --- | --- |
| SUP-01 | 真实续费事件、规则区间/优先级、实际检索及所选政策 | 不以工单日期/latest替代；日期未批准则阻断 |
| SUP-02 | 认证tenant及实际工具/RAG返回对象的完整授权记录 | 客户body/模型tenant字段不构成权限 |
| SUP-03 | 建议、调用、拒绝与敏感效果分别核验 | 首期只模拟工具；发布Approval不授权退款 |
| SUP-04 | 关键结构化claim与账单/适用政策一致 | 引用存在不证明正文语义，文本仍待人工审核 |
| SUP-05 | 旧ticket revision不能覆写新结果 | 精确屏障/条件写/完整轨迹到位后准入 |
| SUP-06 | draft达到批准持久性边界才DONE，释放后活性可证 | Job DONE不代表Ticket RESOLVED或退款发生 |
| SUP-07 | 缺信息/冲突正确补证，完整正反事实不一律转人工 | 规则不明交Owner，不由模型补造 |
| SUP-08 | Tool Schema v1/v2的事件日期、账单/币种/revision映射 | 真实参数/返回证据，不只工具名或200 |

主展示为自有实验Stable V3.4→正确Candidate V4.0：新增十一月政策与Tool Schema v2，保留十月事件兼容。latest-policy-only和wrong-date分别作为SYNTHETIC_MUTATION。来源参考、业务扩展、正确候选与变异分别登记；不能把公开演示当隐藏评估。

RequirementDelta区分新行为与保留不变量；回退Stable删除新政策/schema能力不能通过。Discovery可以减少无关探索，Final仍执行当前Profile全部适用强制安全、新功能、兼容和tier回归。UNKNOWN_IMPACT扩大检查/QA复核，缺能力阻断，不临时删mandatory。

SemanticQualityReport单列rubric/grader/校准/重复/不确定性和费用；首期是报告而非硬规则替代。若日后升为Gate，必须另行批准准入阈值及处理规则。语义好评不能覆盖确定性FAIL或证据UNKNOWN，结构化PASS也不能宣称客户回复已完整评估。

实现 coverage 时从冻结 Acceptance Profile 解析规则→检查→断言映射，逐个记录适用依据、所需证据等级、所需重复策略和实际有效结果。`NOT_APPLICABLE` 应是独立的适用性裁定，附批准依据，不作为绕过 mandatory 的临时 assertion_status。SKIP、ERROR、超预算、观察缺失均不是 PASS。

## 检查点 14.3：实现可解释的 Gate 决定

先区分送验条件与验收通过：`final_readiness` 只表示可启动当前候选的完整 Final，不能预填任何 mandatory PASS。它须确认候选完整冻结且可构建、合同/权限有效、环境与 Observer/Oracle 能力就绪、当前候选无未解决已知候选资产Finding、没有妨碍可信执行的在途未知或完整性阻断，以及 FINAL 池的 MANDATORY 子用途和相关恢复/清理资源已预留、总截止时间允许。原候选遗留失败只有在可信复现/修复验证关闭或明确不适用于当前候选时才不阻断；不能由模型标记“已修复”关闭。

预算统一采用 `budget_pool=CODING|DISCOVERY|FINAL|RECONCILIATION|CLEANUP`。FINAL reservation 必填 `final_purpose=MANDATORY|DELIVERY`，其他池不得填；MANDATORY 用于独立强制验收，DELIVERY 仅用于有变更 DRAFT_PR_CI 的已批准交付，审批等待/发布/CI 不能挤占 MANDATORY。总账、池与子用途账原子约束见第 11 章，Final 通过不自动代表 DELIVERY 资源或授权有效。

当前阶段所需 Coding/Discovery 池耗尽或探索时限到达时，Runtime 先关闭探索派发并原子设置 `exploration_closed=true`；Release 的 repair_policy=DISABLED、CODING=0 本身不触发 drain。仅在上述送验条件成立后，以 `FINAL_DRAIN_READY` 事件进入 FINAL_VERIFYING，继续预先保留的受限 Final。可有界确认的未知先使用 RECONCILIATION，决策截止仍不具备 readiness 则按第 11 章 STOPPED，保留未完成项。该事件不是业务 PASS，也不是新 Run 状态。Final 收尾中发现失败不能重新开启 Coding/Discovery。已经 STOPPED 的 Run 不得借尚有 FINAL 额度或 checkpoint 恢复业务，只允许对账/清理或明确新任务。

纯决定函数输入一个已验证的只读快照。以下为项目伪代码：

```text
evaluateCandidateFinal(snapshot):
  reject if candidate is not frozen or content digest cannot be verified
  require frozen task_mode, repair_policy and completion_mode match Run/WorkOrder
  if task_mode is RELEASE_VERIFICATION:
    require Stable/submitted candidate/ChangeSet/RequirementDelta reference closure
    require current candidate satisfies approved new requirements and retained invariants
    do not require a CodingAttempt; do not import historical Stable PASS
  require trusted change_kind and complete base/candidate input digests
  if change_kind is UNCHANGED:
    require base_input_digest equals candidate_input_digest
    require real trusted Candidate and current independent Discovery/Final
  if completion_mode is DRAFT_PR_CI:
    require sealed Git commit/tree/base and actual ancestry/object closure
    require runner input tree matches the exact publishable candidate
    if change_kind is CHANGED:
      require actual authorized Git changes and all build input changes in approved scope
  reject unauthorized or unrepresented build input drift, even if Git tree is unchanged
  reject if bundle/reference closure/owner approval is invalid or revoked
  expand applicability and mandatory assertions from frozen acceptance profile
  reject untrusted collector, mismatched identities, missing/corrupt observations
  for every required assertion:
    require approved execution/repetition policy and sufficient evidence level
    require valid result for this candidate and this scope
    require execution COMPLETED, then assertion PASS, then attribution NONE
    otherwise route execution/evidence/failure cause in that order and record reason
  require no unresolved blocking finding for this candidate/scope
  require relevant UNKNOWN and cleanup/integrity blockers resolved
  return ELIGIBLE | BLOCKED | INCOMPLETE with reasons and evidence refs
```

`ELIGIBLE/BLOCKED/INCOMPLETE` 是拟建 `GateDecision` 的结果，不能与 Run 状态或单项 assertion_status 混用。`INCOMPLETE` 表示缺结果、缺观察或未完成；`BLOCKED` 表示已知失败/失效条件。具体原因码如 `MANDATORY_MISSING`、`WRONG_CANDIDATE`、`EVIDENCE_HASH_MISMATCH`、`CONTRACT_REVOKED`、`PLATFORM_QUALIFICATION_MISSING` 应归属正确的阶段；最后一个只在组合发布资格时使用。

Final 通过证明该范围的候选符合已批准验收。规则不清或缺环境证据时，报告精确缺口，不用总体平均分补偿。WorkOrder 中已知目标缺陷或当前 Blocking Finding 未解决时，即使部分测试通过也不能关闭任务；错误不修改不是成功。

Runtime 必须先判断可信 `change_kind`，再按 completion_mode 分流；以下出口均要求当前候选 Final 合格、任务目标满足、相关 UNKNOWN 和清理阻断已解决：

| change_kind | completion_mode | Run 出口 | publication_status / ci_status |
|---|---|---|---|
| UNCHANGED | FINAL_ONLY | FINAL_VERIFYING → NO_CHANGE | NOT_APPLICABLE / NOT_APPLICABLE |
| UNCHANGED | DRAFT_PR_CI | FINAL_VERIFYING → NO_CHANGE；不创建 Approval/空 PR | NOT_APPLICABLE / NOT_APPLICABLE |
| CHANGED | FINAL_ONLY | FINAL_VERIFYING → SUCCEEDED | NOT_REQUESTED / NOT_REQUESTED |
| CHANGED | DRAFT_PR_CI | 组合适用资格后 WAITING_APPROVAL → PUBLISHING → WAITING_CI → SUCCEEDED | 按真实发布/CI 事实更新 |

NO_CHANGE 是相对任务比较基线且经独立验收支持的无变化终态，不是 Coder 无快照或平台未修复的同义词。外部新功能候选未经 Codex 修改即可作为 CHANGED 通过，随后按 completion_mode 完成。新 Candidate 可以有新记录 ID，而完整输入与 base 相等；记录 ID 不能制造 CHANGED，源码相同也不能独自制造 UNCHANGED。回退 Stable 但缺少批准新功能时任务不满足，不能进入 NO_CHANGE。报告区分“候选无需修复通过”“修复后通过”“相对基线无变化”与外部交付，评估阶段再依据隐藏 ground truth 校准，不能把无修复通过算作 repair_success。

NO_CHANGE 和 FINAL_ONLY 都不派发 DELIVERY。若原任务预留了 DELIVERY，只有可信确认未发送后才释放该子用途预留；未决操作与清理义务仍分别保留 RECONCILIATION/CLEANUP 资源，不能终态时一键清空全部预留。有变更 DRAFT_PR_CI 在进入交付前另核验 DELIVERY 余额、用途与精确授权。

## 检查点 14.4：防止验完后内容变化

纯函数之外的 finalization-service 按以下方式登记：

1. 读取 run version、当前 candidate、冻结上下文、当前撤销视图和结果集合，形成 evaluation snapshot。
2. 核查对象存在性与摘要，生成不可变 VerificationBundle；大对象验证在事务外完成，但对象不可变性必须成立。
3. 在短数据库事务内重新核对 run/candidate/version、合同撤销与结果索引版本，条件写入 GateDecision。任一变化则丢弃待登记结论并重新评估。
4. 保存 manifest digest、规则/结果引用、决定原因和判定器版本，提交 outbox 通知；UI 只读这份决定。
5. 人审和实际发布前仍重新判断当前资格。一个历史 GateDecision 不会因候选后来变化而被修改成失败；追加失效记录或计算当前不可用状态，保留历史事实。

ArtifactStore 若不能保证登记内容不可改写，摘要校验与本地文件系统权限还需修复；不能凭数据库记录了 hash 就假定内容锁定。批准、发布、恢复链路都需重新验证精确对象。

## 检查点 14.5：报告和验收

报告至少包含：范围/tier、task_mode/repair_policy/completion_mode、可信 change_kind、base_input_digest/candidate_input_digest、Stable/提交/当前候选与修复输入谱系、ChangeSet/RequirementDelta/历史覆盖引用、合同/环境/runner/oracle 版本、execution_mode、包含新功能/兼容要求的完整 mandatory 覆盖表、首次失败及重试、ValidatedFinding、Final readiness 与最终决定各自的证据、平台资格状态、发布/CI 的 NOT_REQUESTED/NOT_APPLICABLE、尚未发生或实际状态。历史与当前执行分别标记，模型摘要可附加，事实表由机器记录生成。

未来命令：本章实现脚本后才可执行。

```sh
pnpm run verification:final -- --candidate <Candidate-ID> --acceptance <Profile-ID>
pnpm run verification:report -- --bundle <VerificationBundle-ID>
```

| 正反例 | 必须观察到的结果 |
|---|---|
| 同候选所有适用 mandatory 完整通过 | 候选 ELIGIBLE，有独立证据；发布资格仍单列。 |
| 外部 Release 候选无需修复、CODING=0 | 正常独立 Final；相对 Stable 的 CHANGED 按 completion_mode 成功/交付，不要求 CodingAttempt。 |
| 只测十一月新请求，缺十月历史兼容/工具v1-v2组合 | 当前Release覆盖不完整，一个API响应不能替代兼容验收。 |
| 旧版smoke通过，但新政策/schema或历史兼容失败 | SUP及RequirementDelta阻断，旧集合绿灯不能替代新功能。 |
| 合法新行为与 Stable 不同 / 候选回退 Stable 删除新功能 | 前者按批准需求判定；后者新功能检查失败，不判 NO_CHANGE。 |
| Stable 历史全绿、当前候选缺独立结果 | INCOMPLETE；历史不变当前 PASS，也不强制无理由重跑全部 Stable。 |
| 候选 A 通过，替换成 B | A 证据留存，B 缺对应有效结果，不能复用绿灯。 |
| 进程成功但没有观察/屏障失效 | INCOMPLETE/INCONCLUSIVE，不能给 PASS。 |
| 缺一个 mandatory 或临时改成 SKIP | 阻止放行，显示具体 assertion ID。 |
| 初次 FAIL 后 PASS | 失败不消失；按批准的重试/flake 策略判断，不重试到绿。 |
| 原始对象被替换或跨租户引用 | 摘要/权限拒绝，记录完整性事件。 |
| Gate 计算期间候选/合同状态变化 | 条件提交失败，必须重算。 |
| DRAFT_PR_CI 的当前 PR 尚不存在 | 候选 Final 可以计算；报告 CI 为未执行，不形成循环。 |
| 业务 Final 通过但平台资格缺失 | 不能进入真实发布；第 15 章补齐适用资格。 |
| CHANGED 的 FINAL_ONLY 合格且无相关 UNKNOWN/清理阻断 | Run 为 SUCCEEDED，发布与 CI 均 NOT_REQUESTED。 |
| UNCHANGED 候选独立 Final 合格且无阻断 | 两种 mode 均 NO_CHANGE，发布/CI 均 NOT_APPLICABLE，无审批/空 PR。 |
| Coder 仅提议无改动、没有新快照 | 核验并保留 repair_input_candidate_ref；不回退 Stable，不绕过未解决 Finding。 |
| 源码不变但依赖/配置改变，或已知目标缺陷未解决 | 不能认定合格 NO_CHANGE；按输入变化或缺陷继续阻断。 |
| 输入 digest 改变、Git tree 相同且变化不在批准发布包内 | 阻断，不能用空 commit/PR 取得 CHANGED 发布资格。 |
| COMPLETED + PASS + UNKNOWN / execution UNKNOWN + PASS | 非法三轴组合拒绝，不能加入有效 coverage。 |
| DISCOVERY 耗尽且 Final readiness 成立 | FINAL_DRAIN_READY 进入受限 Final，不先 STOPPED 再恢复。 |
| DELIVERY 耗用企图占用 MANDATORY 保护额 | 预算入口拒绝，Final 验收额度不被发布/CI 挤占。 |
| 当前候选仍有已知候选资产Finding | 不满足送验条件；无修复预算时 STOPPED，不能把 Final 当掩盖缺陷的流程。 |
| DRAFT_PR_CI 缺封存 commit 或 Runner 使用不同树 | Final 阻断，不能在批准/发布阶段补造身份。 |
| 更换 EnvironmentInstance 或 fixture reset 后重试 | 新 CheckRun 及 retry_of 关联；原物理尝试和失败证据保留。 |
| FINAL_ONLY 运行中改成 DRAFT_PR_CI | 拒绝变更；另建明确授权的新任务。 |

拟建导出 `reports/final/<run_id>/<candidate_id>/` 包含 coverage、VerificationBundle、GateDecision、人读报告及完整性检查；每个条目链接真实受控对象，不产生可绕过 ACL 的公开 URL。

完成检查：

- [ ] 状态三轴、适用性、GateDecision 和 Run 状态分开。
- [ ] 适用SUP规则具备完整断言、可信观察、健康/变异/坏观察器校准；缺能力不放行。
- [ ] Release Profile覆盖新政策/Tool Schema v2及历史兼容；语义质量与自动对客未验证范围单列。
- [ ] 结果绑定当前候选，缺项/未知/篡改会阻断。
- [ ] 判定确定性、可复算，模型无签发权。
- [ ] Final、平台 qualification、发布后 CI 时序明确。
- [ ] 两种 completion_mode 的出口明确，发布模式在 Final 前固定可发布 Git 对象。
- [ ] NO_CHANGE 先于 completion_mode 分流，以相对任务 base 的完整输入相等、需求满足和独立 Final 为依据；平台无修复不误判 NO_CHANGE。
- [ ] Final readiness 是送验条件，FINAL_DRAIN_READY 不预设 PASS，也不能恢复 STOPPED。
- [ ] 生成报告没有把 fake、规划、历史结果或局部自测当真实验收。

下一会话可复制：

> 请按第 14 章先实现 evidence-binding、coverage 和纯 Final Gate。核对第 08 章适用SUP断言及政策/工具/权限/草稿提交证据是否完整，未批准的规则不要激活。用固定测试 fixture 验证缺 mandatory、错误 candidate、证据替换、合同撤销和 INCONCLUSIVE 会阻断；然后接真实 CheckResult 做一个可复算报告。不要依赖尚未发生的本 Run PR/CI，不调用发布接口。保留首次失败和全部物理尝试，记录实际证据与尚缺的平台资格。

