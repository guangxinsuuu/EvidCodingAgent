# 11｜恢复、对账、取消与预算

> 实现状态：**NOT_STARTED**。本章接口、脚本、策略和证据目录均为拟建。没有执行故障注入或任何外部操作。

本章的最小演示是：进程在“已经做了事，但还没写结果”的时刻退出，系统能够辨认结果未知、先查事实，再决定继续或停止；同时，两个并发工作不能把同一份预算各花一次。

依赖 [10 持久运行时](10-durable-runtime.md)。这里先用可控 simulator 验证协议，真实 GitHub 语义留到 [17 发布与 CI](17-publication-and-ci.md)，真实模型适配留到 [12](12-codex-coding-worker.md)。Simulator 的行为必须显式标记，不能假定远端也实现相同幂等能力。

## 11.1 把三类失败分开

第一类是确定未开始，例如权限检查在发送前拒绝，可以修复前置条件后重新申请。第二类是已知完成，例如可信 Collector 已保存完整证据，可以在身份一致时恢复引用。第三类是请求已发送、执行者或网络失联，结果未知；不能把它等同于失败，更不能立即再发一遍。

Run 的 `RECONCILING` 表示正在解释未知事实；CheckExecutionAttempt 的 `execution_status=UNKNOWN` 表示该次执行状态未知；断言轴仍为 `NOT_EVALUATED` 或 `INCONCLUSIVE`。这些状态不能被日志中的一句“成功”替代。

技术超时不等于客服 Job 已失败或已取消。长期排队/执行中时保留队列、Worker、模型代理、工具调用和草稿存储的观察；不能凭测试等待超时把 Job 改成 FAILED。LLM 响应或费用未知、工具动作未知、草稿保存未知分别登记，不能用一种 UNKNOWN 覆盖其他事实；重投前先核对已持久 Job/草稿和在途调用。

恢复同时保留准入冻结的 `task_mode=RELEASE_VERIFICATION|INCIDENT_REPAIR`、`repair_policy=DISABLED|ON_VALIDATED_FINDING` 和 completion_mode。Release 先验证外部提交候选，Coding 只是可信缺陷且获准修复后的分支；重启、恢复或预算不足都不能把验证任务改造成先写代码的任务。Stable 历史覆盖可以帮助解释风险，不自动成为当前候选的通过事实。

## 11.2 拟建文件和核心对象

| 拟建路径 | 职责 |
|---|---|
| `packages/contracts/src/effects.ts` | EffectOperation、EffectObservation、重试资格 |
| `packages/contracts/src/budget.ts` | BudgetReservation、用量与结算状态 |
| `packages/persistence/migrations/002_effects_budget.sql` | 副作用、预算、恢复游标、清理回执表 |
| `packages/domain/src/recovery-policy.ts` | 按事实分类恢复，拒绝通用盲重试 |
| `apps/control-plane/src/reconciler.ts` | 扫描到期未知操作，执行 observe |
| `apps/control-plane/src/budget-service.ts` | 原子预留、结算、释放和硬上限 |
| `packages/verification/src/cleanup.ts` | 按资源所有权清理并收集回执 |
| `tests/integration/recovery-budget.spec.ts` | 真数据库竞争、进程退出与预算测试 |

`EffectOperation` 至少包括 `operation_id, tenant_id, run_id, kind, request_digest, target_scope, approval_id, approval_binding, idempotency_key, status, attempt_count, remote_id, last_observation, next_observe_at, owner_epoch`。各实际发送步骤另记 `step_id, dispatch_attempt_id, observed_revocation_epoch, dispatch_committed_at`。状态草案为 `PREPARED / DISPATCHING / UNKNOWN / OBSERVED_SUCCEEDED / CONFIRMED_FAILED / MANUAL_REQUIRED`；这些是副作用状态，不增加到 Run 枚举。

`EffectObservation` 必须能表达：

```ts
// 接口草案；每个 provider 需定义这些状态的具体证据标准。
type EffectObservation =
  | { state: "FOUND"; remoteId: string; identityMatches: true; evidenceRef: string }
  | { state: "ABSENT_CONFIRMED"; evidenceRef: string; retrySafe: boolean }
  | { state: "CONFLICT"; evidenceRef: string; reason: string }
  | { state: "INCONCLUSIVE"; reason: string; nextObserveAfterMs?: number };
```

读权限不足、分页未读完、服务端延迟、网络故障都不能转换为 `ABSENT_CONFIRMED`。`FOUND` 必须核对完整身份，不能仅按标题碰到一个对象就算成功。

## 11.3 小步实现恢复协议

### 步骤 A：在每个不可逆边界前持久化意图

环境创建、模型执行、外部 PR 等操作各自定义逻辑 operation ID。先持久化请求摘要和资格，之后才发送；响应与证据落库后才推进状态。响应丢失时保留同一个 operation ID，物理重试增加 attempt，不能伪装成一项全新操作。

并非所有操作都具备相同幂等能力。环境 Host 可按 `environment_instance_id` 查询容器组；模型 provider 是否可查先前请求依具体适配器而定；外部 PR 可查远端对象但不天然提供端到端 exactly-once。无法观察时保持未知、停止重复调用或进入人工处理。

检查点：simulator 提供“提交成功后丢响应”和“发送前失败”两个独立故障。前者必须先 observe，后者才允许按策略重发。

批准的消费绑定与撤销是两个独立事实。`consumed_by_operation_id` 防止同一批准被拿去建立另一个逻辑操作；`revoked_at / revocation_epoch` 决定该授权是否仍可用于后续发送。批准已消费、EffectOperation/outbox 已写入但尚未取得发送资格时，仍可撤销；outbox 只是意图，不是不可撤销通行证。

每个后续步骤以及每次物理重发，都必须经过第 17 章的发送准入事务：与撤销事务按同一顺序锁住批准授权行/操作步骤行，检查最新撤销、有效期、scope、租约和预算，然后记录一次性 `DISPATCHING` 凭据。撤销先提交则该步骤不得发送；发送准入先提交则该步骤已跨过“可能在途”边界，撤销仍阻止后续步骤，但本步需 observe。网络请求在数据库事务外，不声称数据库回滚能撤销已发请求。若能证实本步从未发出，保存未发送回执；不能把未知当作未发出来释放预算或重试。

### 步骤 B：建立恢复矩阵

| 中断位置 | 恢复读取 | 允许动作 |
|---|---|---|
| Run 事务已提交，尚未派发 | outbox 与 Run | relay 补派同一逻辑工作 |
| 工作已领取，执行未启动 | attempt 租约、启动回执 | 证明未启动后创建新物理尝试 |
| 环境可能已创建 | Host 按实例 ID 查资源 | 找到则核对/接管；未知则继续对账 |
| Coding 产物已上传但引用未登记 | 内容寻址存储、上传主体、Attempt | Collector 校验后登记；不能直接接受 workspace 文件 |
| Release 候选接收后、Discovery 尚未派发 | submitted/current Candidate、Stable/ChangeSet/需求引用、outbox | 沿原候选补派 Discovery；不新建 CodingAttempt、不回退 Stable |
| 检查完成但 Graph checkpoint 落后 | CheckRun、CheckKey、证据完整性 | 引用已接受结果，Graph 不重复裁决 |
| 外部动作可能完成 | EffectOperation、provider observation | 完整匹配才标已完成；否则未知/冲突 |
| 人工审批后代码/合同变化 | 冻结摘要、撤销/到期状态 | 审批失效，重新验证/审批 |
| 批准已消费、派发前被撤销 | 最新 revocation_epoch、步骤发送登记 | 未取得发送资格的步骤阻断；已取得资格的步骤按可能在途对账 |
| 数据库回退后外部世界未回退 | 备份边界、操作日志、远端清单 | 暂停写副作用，按 operation 对账 |

对账用新持有者的资格提交，不让旧 Worker 自己恢复写权限。旧证据可以保留，但采纳需满足相同候选、合同、环境和 evaluator 身份。

检查恢复统一采用第 10 章完整 CheckKey。`execution_set_id / phase / repetition_index / environment_instance_id / fixture_reset_id` 任一变化均是新的逻辑检查；因环境重建或 reset 重试时，以 `retry_of_check_run_id` 连接旧 CheckRun。只有身份完全未变且证实安全的重试才在同一 CheckRun 下增加物理尝试。旧 attempt 为 UNKNOWN 时先 observe/对账；创建新环境不能证明旧故障注入或远端动作不存在。

### 步骤 C：重试策略按类别注册

为每种 work/effect 定义最大尝试数、总截止时间、退避范围、可重试错误、是否需要先 observe、停止升级条件。值由本地实测和 Owner 约束选择；不要把示例的“2 次未找到就重试”“最多 5 次”当通用保证。

采用带抖动的退避，持久化 `next_attempt_at`；进程重启后不能重置累计次数与截止时间。禁止模型自由指定无限重试、创建新幂等键绕过额度，或将认证失败归类为短暂网络错误。

检查点：多个 reconciler 同时扫描一个未知操作，只能一方取得当前观察/派发资格；其他方记录让步，不产生额外副作用。

### 步骤 D：取消与清理分两阶段

收到取消后，先原子记录取消意图并阻止新工作、新模型调用和新外部写入。然后向已运行的本地进程组、环境 Host 和可取消的 provider 发停止请求，最后观察实际资源状态。

已创建的 Draft PR、已提交的远端操作不会因为本地 `CANCELLED` 消失。Run 可以处于已取消状态，同时关联 `cleanup_status=PENDING` 或待对账 EffectOperation；界面必须显示残留。取消不默认授权删除远端对象。

清理器只操作资源清单里具有相同 tenant/run/environment 所有权的资源，不遍历机器删相似名称。保存 `CleanupReceipt`：目标、操作、执行身份、前后观察、残留和结束时间。失败可继续对账，不用“大清理”掩盖。

## 11.4 预算分池，并由同一事务账本执行

Token、金额、运行时间、环境数量、并发检查和修复次数是不同维度。Run 的总硬上限之外，还须有不能相互挪用的五个预算池；Planner/Coder 不能通过在请求中改 pool 名称获得剩余资金。

```text
budget_pool = CODING | DISCOVERY | FINAL | RECONCILIATION | CLEANUP
BudgetAccount:
  run_id, budget_policy_digest, total_limits,
  pool_limits[budget_pool], pool_reserved[budget_pool], pool_settled[budget_pool],
  final_purpose_limits[MANDATORY|DELIVERY],
  final_purpose_reserved[MANDATORY|DELIVERY], final_purpose_settled[MANDATORY|DELIVERY],
  hard_deadline, final_latest_start_at, version
BudgetReservation:
  id, run_id, budget_pool, dimension, logical_operation_id, purpose,
  final_purpose (MANDATORY|DELIVERY; required only when budget_pool=FINAL),
  reserved_units, settled_units, status, price_snapshot_digest, created_at, settled_at
```

金额使用整数最小计价单位或有明确精度的十进制，不用浮点累计。按 dimension 分别保存限额、预留和实耗；墙钟还受绝对 hard_deadline 和各阶段截止时间约束，不能通过多池相加延长 Run。`pool_limits` 和用途权限随准入预算策略冻结，自动重试不得改限额或借池；需要新预算授权时按新准入任务处理。

| 池 | 允许的用途与发起者 | 不允许借此做什么 |
|---|---|---|
| CODING | repair_policy=ON_VALIDATED_FINDING 且当前可信候选资产 Finding 下的 Coding Attempt、模型调用及允许的局部反馈 | 未发现缺陷就改 Release 候选；改成 FINAL/恢复池继续修复，或让 Coder 自己追加额度 |
| DISCOVERY | 可信调度器接受的 Stable 资格/必要轻量核对、变更风险探索、参数化实验与 Planner 调用 | 默认重跑 Stable 全部历史测试；消耗 Final、恢复或清理保留额；模型自选池 |
| FINAL | Final 协调器/可信 Runner 的当前候选强制验收；已批准 DRAFT_PR_CI 的交付动作只用单独受限的 DELIVERY 用途额度 | 新的 Coding/探索；用交付开销挤占 MANDATORY 验收额度 |
| RECONCILIATION | 可信 reconciler 的有限 observe、事实确认、未知用量结算 | 用“恢复”名义新开模型修复、重跑业务 Final、创建未授权发布 |
| CLEANUP | 可信清理器按所有权清单回收本 Run 已分配资源并核验 | 新实验、删他人资源、未经授权关闭 PR/删远端分支 |

FINAL 内部通过 `final_purpose=MANDATORY|DELIVERY` 分别保护强制验收和交付；仅 FINAL reservation 必填此字段，其他池禁止填写。`final_purpose_limits/reserved/settled` 按同样维度记账，用途限额之和不得超过 FINAL 池限额。审批等待、发布与 CI 只能使用 DELIVERY，不能挤占 MANDATORY；实际交付派发还要求 Final 通过且独立发布批准有效。FINAL_ONLY 或 NO_CHANGE 不派发 DELIVERY，确认未发送的用途预留可释放；不能因此释放未知用量、撤销恢复义务或省略清理。终态下的残留观察使用 RECONCILIATION，授权资源清理使用 CLEANUP；它们不是继续业务的通用能力。

在允许任何 CODING/DISCOVERY 派发前，准入协调器根据冻结 mandatory 集、执行/重试上限、环境规格和价格快照计算一个完整 Final 的保守预算，并在 FINAL 池建立保护性预留；同时保护有界 RECONCILIATION/CLEANUP 额度。该预留证明资源可安排，不证明业务一定通过。无法给出足够保留额或缺少计价/资源边界时，不启动探索，记录准入预算缺口。下一轮探索还必须保留完整 Final 所需的余额与墙钟窗口，达到 `final_latest_start_at` 即停止探索并走下面的送验/停止决定。

RELEASE_VERIFICATION 可冻结 `repair_policy=DISABLED`，CODING 限额为零，不要求为未授权修复预留费用。Discovery 和包含新功能/兼容要求的完整 Final 仍须足额。只有当前阶段需要的池不足才阻止该阶段；“CODING 为零”不是 Release 验证提前 drain 的理由。发现有效缺陷但未授权修复，或修复池不足时，保留 Finding 与失败/阻断事实；既不借池，也不回退稳定版来绕过新需求。

预算按实际受控用途归属，不按产品名称归属。第18章EVAL_B的Codex只作为只读发布Planner时，其分析/选测调用记DISCOVERY，并受相同验证工具权限限制；只有获准改候选的Coding Attempt才用CODING池，不能因provider同名混用权限或账目。

保护性 Final 预留与具体检查 reservation 是同一额度的父子分配：Final 开始时原子将父预留的一部分转换为检查子预留，不重复计入总额；子预留只能支付当前执行集合的批准工作。Final 失败后若还要修复/探索，必须先证明仍有下一份完整 Final 的保护额，否则停止该修复循环，不能挪用恢复/清理池。

每次预算操作按固定锁顺序在一个 PostgreSQL 事务里执行：

1. 锁 Run 总账户、目标池账户及所需父 reservation；读取当前 `pool_reserved/pool_settled`，包含未知用量仍占用的预留。
2. 根据认证角色、工作类型、phase 和用途选择 pool，拒绝调用者伪造池；逐维度同时检查 `Σ(pool_settled + pool_reserved) + 新增净预留 <= total_limits` 与 `目标池 settled + reserved + 新增净预留 <= pool_limits[目标池]`。FINAL 同时锁用途子账并检查 `final_purpose_settled + final_purpose_reserved + 新增净预留 <= final_purpose_limits`；用途不能相互借用。父转子使用净变化，不能重复扣两次。
3. 同一事务创建唯一 reservation、更新总/池账本、登记逻辑工作与 outbox；任何失败一起回滚，不出现“任务已发而预算没占”或双重扣款。
4. 执行前验证 reservation 的 pool/purpose/operation 绑定；执行后用可信 usage/receipt 原子结算总额和池内余额，剩余金额仅返回原池。
5. 确定未执行的预留可释放到原池；已派发但用量未知的预留保持占用，不能把它填零或借给其他池。若实际费用可能超出预留，冻结新的可计费派发并用保留的恢复额度对账。派发必须有可强制的支出/资源边界；若远端最终账单仍超出预留，如实记录 `BUDGET_OVERRUN` 和实际费用，不修改历史金额来伪造合规。总额守卫约束新派发，不声称能撤回已产生的远端费用。

准入时要求各可加总维度的池限额之和不超过 Run 总上限，仍在每次操作中双重校验。额度不足时只有拒绝、进入受限 Final drain 或停止，没有“自动从清理池补足”的后门。`RECONCILIATION/CLEANUP` 也有操作次数、资源、费用与截止时间上限；不足时保存残留并升级人工处理，不无限运行。

模型侧没有真实账单时写 `cost_status=UNKNOWN/ESTIMATED`，保留估算依据；未知预留仍计入 `pool_reserved`。实时价格核验在真实 provider 接入时完成，并冻结价格快照；本手册不给未经验证的价格。

检查点：预算只够一项任务时，两个独立连接跨池/同池并发预留，都不能突破总额或池内限额。恢复同一 operation 返回同一预留并核对用途；新的物理调用若可能再次计费，必须在原用途池重新取得足额保障，不能拿“重试”绕开硬上限。

## 11.5 无进展与停止条件

记录候选摘要、已验证 Finding 集合、失败原因和每次尝试的增量。相同候选反复提交、反复产生同一反例、连续环境失败，都要触发明确停止策略。模型的“再试一次可能成功”不是预算豁免。

当前阶段所需 CODING 或 DISCOVERY 额度耗尽，或探索时限到达时，首先原子设置 Run 的 `exploration_closed=true`，禁止新的探索/修复派发，**不能立即把所有 Run 都置 STOPPED**。未请求修复的 Release 任务 CODING=0 不触发此决定。此标记在本 Run 内不再改回 false。Runtime 在同一决策快照中核对 [第 14 章](14-final-gate-and-evidence.md) 的 Final readiness：当前可信 Candidate 已冻结、活动写入已停止、身份/合同/环境有效、无已知未解决候选资产 Finding、没有妨碍验收的未决执行/证据事实，并且保护的 Final 及相关恢复/清理预算与时间仍充足。这是允许送验的条件，不是 PASS，也不要求 Final 结果已经存在。

若 readiness 满足，由 `FINAL_DRAIN_READY` 事件在预算和状态事务内将当前 CODING/DISCOVERY_VERIFYING 转为 `FINAL_VERIFYING`，关闭探索权限、转换 Final 预留并派发专门 Final 执行集合。接下来只用 FINAL 池完成批准验收，不因探索池为零而拒绝它。若 readiness 不满足，则记录具体原因与未完成 mandatory，发 `FINAL_DRAIN_NOT_READY → STOPPED`；合同/访问等独立阻断仍如实保留原因，不把它们当通过。

如果候选/调用仍为 UNKNOWN，可在探索关闭后先使用有界 RECONCILIATION 配额确认事实，在已冻结的 drain 决策截止时间前再作 readiness 判定；不得趁对账继续 Coding/Discovery。截止仍无法确认就 STOPPED 并保留残留。Final 已开始后若 FINAL 额度或硬期限不足以完成强制集合，停止并记录未完成项，不再开始修复，不借其他池，不重试到绿。

Final drain 必须在提交 STOPPED 之前完成分流。STOPPED 是终态，不能再恢复到 FINAL_VERIFYING；终态仍允许已授权、有限的 RECONCILIATION/CLEANUP 工作，它们不改变终态、不新增业务实验或发布。完成未知事实与清理不需要把 Run “复活”。

恢复必须重新验证当前授权、合同未撤销、剩余预算和环境，并保持冻结的 task_mode、repair_policy 和 completion_mode。CANCELLED、STOPPED、FAILED、SUCCEEDED、NO_CHANGE 都不自动恢复；取消/停止决定一旦成为终态，只继续必要的对账与清理，不重新开启 Coding/检查/发布。用户提出继续工作时重新准入新 WorkOrder/Run，可用 `related_run_id` 链接旧历史，不能原地清除停止记录或改变模式/修复授权。

### 特殊状态的实际允许转换

进入特殊状态时保存由服务端生成的 `interrupted_phase`、触发事件、对象摘要和原因码；恢复请求只提交事件与证据引用，不接受调用者任意指定目标状态。以下为首版白名单；未列出的转换一律拒绝。每行都需当前认证、有效租约、expected_version、冻结身份一致、取消/撤销检查及预算守卫。

| 当前状态 | 允许事件 | 固定目标 | 额外守卫 |
|---|---|---|---|
| BLOCKED | ADMISSION_PREREQUISITE_RESOLVED | PREPARING | 原阶段 ADMITTED/PREPARING，所缺准入证据齐全，未变更冻结 scope/Bundle/mode |
| BLOCKED | BASELINE_RESOURCE_READY | BASELINING | 原阶段 BASELINING，环境能力和 Observer 自检恢复 |
| BLOCKED | CODING_ACCESS_RESTORED | CODING | exploration_closed=false；repair_policy=ON_VALIDATED_FINDING；原阶段 CODING，可信缺陷及repair_input_candidate_ref仍有效，无未对账的模型/进程副作用；允许新 Attempt 且有预算 |
| BLOCKED | DISCOVERY_CAPABILITY_RESTORED | DISCOVERY_VERIFYING | exploration_closed=false；原阶段 DISCOVERY_VERIFYING，模板/访问能力恢复，仍是当前候选 |
| BLOCKED | FINAL_CAPABILITY_RESTORED | FINAL_VERIFYING | 原阶段 FINAL_VERIFYING，Final 集合未降低，检查身份与环境资格有效 |
| BLOCKED | REVIEW_ELIGIBILITY_RESTORED | WAITING_APPROVAL | 原阶段 WAITING_APPROVAL，mode=DRAFT_PR_CI，Final 与发布前资格有效；过期批准不自动续期 |
| BLOCKED | CI_ACCESS_RESTORED | WAITING_CI | 原阶段 WAITING_CI，已有精确发布收据，head 与 CI Policy 一致，只恢复观察 |
| INCONCLUSIVE | BASELINE_OBSERVATION_READY | BASELINING | 原阶段 BASELINING，观察缺口已修复/校准；新 env/reset 创建关联的新 CheckRun |
| INCONCLUSIVE | DISCOVERY_OBSERVATION_READY | DISCOVERY_VERIFYING | exploration_closed=false；原阶段 DISCOVERY_VERIFYING，观察可判定；旧不确定记录保留 |
| INCONCLUSIVE | FINAL_OBSERVATION_READY | FINAL_VERIFYING | 原阶段 FINAL_VERIFYING，观察可判定，仍需全量适用 Final；不能直接转成功 |
| RECONCILING | PREPARATION_RECONCILED | PREPARING | 原阶段 PREPARING，资源归属/存在性已明确，不重复创建未知环境 |
| RECONCILING | CODING_ATTEMPT_RECONCILED | CODING | exploration_closed=false；repair_policy=ON_VALIDATED_FINDING；原阶段 CODING，可信缺陷及repair_input_candidate_ref仍有效，旧调用/产物/用量已分类；已完成产物经可信冻结再推进，不重新计作模型执行 |
| RECONCILING | BASELINE_EFFECT_RECONCILED | BASELINING | 原阶段 BASELINING，检查/屏障/环境效果已明确，恢复批准的原生实验 |
| RECONCILING | DISCOVERY_EFFECT_RECONCILED | DISCOVERY_VERIFYING | exploration_closed=false；原阶段 DISCOVERY_VERIFYING，未知检查效果已明确，不重复注入故障 |
| RECONCILING | FINAL_EFFECT_RECONCILED | FINAL_VERIFYING | 原阶段 FINAL_VERIFYING，未知检查/清理事实已明确，由 Gate 重新判定 |
| RECONCILING | PUBLICATION_CONTINUATION_AUTHORIZED | PUBLISHING | mode=DRAFT_PR_CI；已有子步骤已核对，每个未完成步骤重新检查最新撤销/到期和发送资格；重发还须可靠缺席及重试安全证据 |
| RECONCILING | PUBLICATION_OBSERVED_SUCCEEDED | WAITING_CI | mode=DRAFT_PR_CI；全部发布动作已读回并匹配精确候选，保存 PublicationReceipt |
| RECONCILING | CI_OBSERVATION_RESTORED | WAITING_CI | 原阶段 WAITING_CI，恢复的是指定 head 的只读 CI 观察，不重新发布 |
| RECONCILING | REMOTE_IDENTITY_CONFLICT | BLOCKED | 保存远端与批准差异，停止所有后续写入；不能自动覆盖远端 |
| BLOCKED / INCONCLUSIVE / RECONCILING | NONRECOVERABLE_FAILURE_CONFIRMED | FAILED | 独立事实证明无法按本 Run 范围继续；未决远端/清理仍留账 |
| CODING / DISCOVERY_VERIFYING / RECONCILING | FINAL_DRAIN_READY | FINAL_VERIFYING | exploration_closed=true、无活动候选写入；RECONCILING 必须来自本次 CODING/DISCOVERY drain 且有界确认已完成；满足第 14 章 readiness，Final/恢复/清理保护额与期限足够；不是 PASS |
| CODING / DISCOVERY_VERIFYING / RECONCILING | FINAL_DRAIN_NOT_READY | STOPPED | exploration_closed=true；RECONCILING 必须来自本次 CODING/DISCOVERY drain；readiness 不满足或有界确认已到期；保存原因、未完成 mandatory 与未知账 |
| FINAL_VERIFYING | FINAL_BUDGET_OR_DEADLINE_EXHAUSTED | STOPPED | Final 剩余额度/期限不足，保留真实结果与缺项；禁止借池或继续修复 |
| 活跃或特殊非终态 | USER_CANCELLED / STOP_POLICY_REACHED | CANCELLED / STOPPED | 用户有权取消，或非“可送验的探索耗尽”停止策略触发；探索耗尽必须先走上述 drain 判定 |
| CANCELLED / STOPPED / FAILED / SUCCEEDED / NO_CHANGE | 无业务恢复事件 | 保持原终态 | 只允许有界 RECONCILIATION/CLEANUP 池动作并追加事实；新 Coding/检查/发布重新准入新 Run |

合同 Bundle、task_mode、repair_policy、completion_mode 或授权 scope 需要改变时，不使用“前置条件恢复”事件原地修改冻结上下文，而是重新准入新 WorkOrder/Run。正常缺陷修复仅在 `exploration_closed=false`、可信候选资产 Finding 与既有修复授权均有效时，可按第 10 章在同一 Run 的 CODING 分支创建新 Attempt/Candidate，并重新建立该候选的检查上下文；不能借恢复事件把旧检查的 candidate 引用替换成新候选。进入 drain 后的有界对账只能补齐事实，再发 FINAL_DRAIN_READY/NOT_READY，不能恢复原 Coding/Discovery 执行。历史结果是否可引用仍受精确身份与导入策略限制。

当前候选 Final 满足、相关 UNKNOWN 和清理/完整性阻断解决后，按第 14 章的可信 `change_kind=UNCHANGED|CHANGED` 分流：UNCHANGED 且任务满足时，任一 completion_mode 都进入 `NO_CHANGE`，publication/CI 为 `NOT_APPLICABLE`，不建空 commit/PR；只有 CHANGED 且 `completion_mode=FINAL_ONLY` 才提交 `FINAL_ONLY_COMPLETED → SUCCEEDED`，publication/CI 为 `NOT_REQUESTED`。CHANGED 的 `DRAFT_PR_CI` 继续沿批准、发布、CI 链完成。不能从 INCONCLUSIVE、RECONCILING 或 STOPPED 直接绕过 Gate 成功。

上述 change_kind 比较冻结任务 base：Release 的稳定版本或 Incident 的原故障/输入版本；它不比较 Agent 有没有动文件。Release 候选新增功能后原样通过仍是 CHANGED。Coder 的 NO_CHANGE_PROPOSED 保留 repair_input_candidate_ref，不回退 Stable，不消除尚未解决的 Finding，也不能靠删除新功能获得 NO_CHANGE。

## 11.6 测试、证据和命令

| 案例 | 预期 | 不能出现 |
|---|---|---|
| 提交成功后丢响应 | UNKNOWN→observe→核对成功 | 立即新键重发 |
| 查询 403/分页缺失 | INCONCLUSIVE | 认定远端不存在 |
| 两个恢复进程竞争 | 单一有效观察/派发资格 | 双重外部写入 |
| 取消时已创建环境 | 阻止新工作、清理有回执 | 只改 UI 状态不追踪残留 |
| 同 operation 预算重放 | 原预留复用并核对摘要 | 重复扣款或不同内容复用 |
| provider usage 丢失 | 保留待结算额度 | cost=0、自动释放全部 |
| 已知反例重复出现 | 到达无进展阈值后停止 | 无限修复循环 |
| DISCOVERY 耗尽但候选 ready、Final 保护额充足 | FINAL_DRAIN_READY→FINAL_VERIFYING，执行完整 Final | 误置 STOPPED，或因探索池为零拒绝 Final |
| Release 修复关闭且 CODING=0，Discovery/Final 有额度 | 正常验证外部候选，零 CodingAttempt | 因未使用的 CODING 池为零提前 drain/STOPPED |
| Release 对账恢复或修复返回无变化 | 保留提交/repair input 候选与新增需求 | 回退 Stable、静默改 repair_policy 或用历史 PASS 放行 |
| 探索耗尽且有未解决候选资产 Finding/候选不可信 | STOPPED，保留缺口 | 把 readiness 当 PASS，或送验掩盖已知失败 |
| FINAL 额度/硬期限不足 | STOPPED，保留未完成 mandatory | 跨池借额、部分通过、STOPPED 后继续 Final |
| 跨池并发与未知费用 | 总额和池内同时受限；未知预留不释放 | CODING 消耗 Final/清理额、总额被并发击穿 |
| 审批等待/发布/CI 与强制验收争用 | FINAL 用途子账隔离；DELIVERY 不足即阻断相应用途 | 占用 MANDATORY 保护额或伪造 final_purpose |
| drain 后收到原 Coding/Discovery 恢复事件 | exploration_closed=true 拒绝恢复探索；确认事实后仅走 drain 分流 | 通过恢复事件清除关闭标记或再开模型修复 |
| 批准消费后/发送准入前撤销 | 所有未派发步骤拒绝，保存撤销事实 | outbox 继续授予发送能力 |
| 撤销与发送准入竞争/步骤间撤销 | DB 线性化顺序可解释；已在途对账，后续步骤阻断 | 宣称网络随 SQL 回滚、自动续发剩余步骤 |
| 已停止/取消 Run 收到迟到结果或 resume | 原终态保留；必要事实追加，继续请求走新准入 | 自动回到 CODING/PUBLISHING |
| 环境/reset 改变后重试 | 新 CheckRun 关联旧 ID，未知先对账 | 覆盖旧失败或复用原逻辑键 |

拟建证据目录：`artifacts/qualification/recovery/<qualification_id>/`，含故障计划、SQL 快照、operation 时间线、模拟服务请求记录、预算流水、恢复前后摘要和清理回执。真实 API 的 qualification 单独存，不与 simulator 成绩合并。

未来命令：本章实现脚本后才可执行。

```sh
pnpm exec vitest run tests/integration/recovery-budget.spec.ts
pnpm exec tsx scripts/recovery-demo.ts --profile local-isolated --provider simulator
```

脚本只结束它创建的测试进程，不接受任意 PID 或任意资源目录作为删除目标。

## 11.7 参考取舍与排障

旧 [durable-workflow.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/durable-workflow.ts:231>) 的 `executing → unknown → observe` 以及 [providers.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/tool-gateway/src/providers.ts:264>) 的远端查找接口值得借鉴。但“两次 absent 后重试”只能是旧演示策略；新实现必须把不完整观察、权限失败和不可证明的缺席单独表达。

旧自定义 `x-egap-idempotency-key` 头是请求元数据，不能证明真实 SaaS 接受它并实现幂等。首次适配真实 provider 时，记录服务实际支持的幂等、查询和一致性语义。

出现长期 UNKNOWN 时，检查最后一次已确认边界、查询权限、请求身份、远端可见性和预算。不要直接把状态改为 FAILED 后重新开始。预算账不平时停止新调用，保留原流水，用补偿记账修正，不能修改历史费用让总数“变好看”。

## 11.8 完成清单与下一次请求

- [ ] 每种副作用都有持久意图、稳定 ID、observe 语义与有限重试策略。
- [ ] “未找到”与“无法确定”分开，响应丢失不会盲目重复执行。
- [ ] 取消停止新动作，现有环境/副作用仍被清理或对账追踪。
- [ ] 五池权限和不可挪用由服务端强制，总额/池内/父子预留同事务；未知用量留账。
- [ ] 探索前保护 Final 与恢复/清理资源；探索耗尽先 readiness 分流，绝不 STOPPED 后再 Final。
- [ ] 消费不消除撤销权，每步发送准入与撤销竞争有真实数据库证据。
- [ ] 终态仅允许有限恢复/清理；UNCHANGED 独立 Final 后按第 14 章进入 NO_CHANGE。
- [ ] 特殊状态只接受白名单事件；取消/停止不自动恢复，task_mode/repair_policy/completion_mode 不变。
- [ ] Release 在 CODING=0 下仍可验证；恢复和无修改提案保留当前候选，不回退 Stable。
- [ ] simulator 与真实 provider 的证据标签分离。

可复制的下一次请求：

> 按第 11 章先实现 EffectOperation、可控 simulator 和“提交后丢响应”的恢复测试，再实现预算预留。全部使用本地隔离资源，不调用真实付费模型或 SaaS。提交实际事件时间线和预算流水，保留未知状态，不用重试掩盖问题。

## GenAI 调用预算与重试的补充验收

每条 usage 除既有 budget_pool/final_purpose 外记录 `cost_role=SUT_LLM|VERIFIER_LLM|SEMANTIC_GRADER|CODER|ENVIRONMENT|TOOL`。SUT/Grader 的模型费用归属触发它的 Discovery 或 Final 检查；同一 reservation 不能由代理和 Runner 重复占用。Coding 模型使用 CODING；不能把所有进程名含 Agent 的调用都算到同一池。价格、token/调用数上限、墙钟和真实计费确认分别保存；估算及 UNKNOWN 不按免费计。

同一实验的重复采样次数和停止规则在计划中冻结，保留首轮失败；不能在失败后追加“再试一次”直到绿。相同候选/参数反复产生相同观察且没有新增覆盖或证据时，按第13章的 no-progress 规则停止探索；强制 Final 的重复策略由 Profile 决定，不能由模型降为单次。

增加故障矩阵：provider 已接受但代理响应丢失；响应已写证据而 CheckResult 未登记；SUT 工具请求未知但 Worker 重启；grader 超时但业务 Oracle 已失败；取消后迟到模型输出；Final 预留不足以支付 SUT+Grader 的完整批准集合。每项保持真实费用/效果与业务终态独立，禁止因重试隐藏首轮结果或绕过关闭的探索权限。
