# 16 · 建立能审查真实候选的控制台与精确审批

实施状态：`NOT_STARTED`。本章只有界面、接口与实施步骤设计；没有网页部署、审批记录或外部写入。

前一章：[15 · 安全与平台资格](15-security-and-qualification.md)。下一章：[17 · 发布与 CI](17-publication-and-ci.md)。本章依赖第 10 章 API、14 章证据与 Gate；真实审批通道在适用资格具备后启用，不能用 UI 提前绕过它们。

## 目标：让人知道自己批准的具体对象

控制台首先是事实阅读器，其次才是动作入口。Reviewer 应在同一处看到工单、候选 diff、业务规则、独立结果、未解决问题、执行模式、完成模式、发布目标及动作。点击批准表示允许在明确条件下创建指定 Draft PR，不意味着允许 Merge、Deploy、工单写回或任何以后出现的候选。

主入口展示task_mode、Stable→submitted/current候选、批准需求变化及repair_policy。Release任务先验证已有新版本，显示测试选择/未选原因、风险与证据缺口；未发现缺陷时无需调用Coder。有新功能的候选相对Stable仍为CHANGED，不能因为“本轮Agent没改代码”显示NO_CHANGE；修复尝试及其相对repair_input_candidate的diff另列。

WorkOrder/Run 的 `completion_mode=FINAL_ONLY|DRAFT_PR_CI` 在准入时冻结。FINAL_ONLY 页面只提供事实审阅：CHANGED 当前候选 Final 通过且相关 UNKNOWN/清理阻断已解决后进入 SUCCEEDED，发布和 CI 显示 `NOT_REQUESTED`；UNCHANGED 且任务满足则在任一模式进入 NO_CHANGE，发布和 CI 显示 `NOT_APPLICABLE`，都没有审批发布入口。DRAFT_PR_CI 且 CHANGED 才显示下文的精确审批流程。运行中的完成模式不可编辑；以后要求发布需创建有明确授权的新任务，而不是为已完成 Run 打开一个发布开关。

旧 AAA 的 [App.tsx](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/console/src/App.tsx>) 已有 Runs、Trace、Approval Inbox、Evaluation 视图与 demo 边界提示，可参考信息组织。旧 [api.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/console/src/api.ts>) 的审批请求带 `actor: console-operator`，这是本地演示身份；新服务必须从认证上下文确定 actor，不能信任浏览器提交的角色或租户。旧 UI 的 simulated Kafka、DSH 和支付术语也不能直接换标题沿用。

## 为什么先做少量事实页

拟采用 TypeScript React 控制台，复用 monorepo DTO；具体 UI 库在第 04 章固定。首期只实现 Run 列表、Run 详情/时间线、候选与证据、审批、评估五类视图。连接器管理和部署面板后续再加，避免在尚无运行事实时制作充满绿色状态的“大屏”。

服务端提供面向页面的只读 projection，但投影不是新的权威数据源。刷新、重连、分页后仍应能回到 PostgreSQL 权威状态。先轮询足以完成单 Run 试点；若引入 SSE，再实现 sequence、断线恢复与补拉，不把连接在线误当任务成功。

## 拟建文件与接口

| 拟建路径 | 职责 |
|---|---|
| `apps/console/src/pages/RunsPage.tsx` | 状态、范围、模式、当前候选和最近进展。 |
| `apps/console/src/pages/RunDetailPage.tsx` | 时间线、检查/尝试、预算、恢复与停止原因。 |
| `apps/console/src/pages/CandidateReviewPage.tsx` | diff、合同、coverage、证据与当前资格。 |
| `apps/console/src/pages/ApprovalPage.tsx` | 精确目标、有效期、批准/拒绝和冲突反馈。 |
| `apps/console/src/pages/EvaluationPage.tsx` | 第 18 章数据集/策略/成本结果，区分 mock 和 live。 |
| `apps/console/src/api/client.ts` | 统一错误码、取消请求、版本和查询缓存。 |
| `apps/control-plane/src/read-models/` | 经授权的页面 DTO，不暴露完整内部表。 |
| `apps/control-plane/src/approval-service.ts` | 认证、精确对象复核、不可变决定、撤销。 |
| `tests/e2e/console-review.spec.ts` | 浏览器事实展示与过期/竞态/越权测试。 |

拟建 API 如下；最终路径与第 10 章统一，不能为 UI 另开不鉴权的“内部快捷接口”：

| 方法/路径 | 输入与输出重点 |
|---|---|
| `GET /api/runs` | 分页、scope、状态和执行模式，tenant 来自认证。 |
| `GET /api/runs/:id` | run version、冻结 completion_mode、当前候选、阻断原因与分段状态。 |
| `GET /api/runs/:id/events` | cursor/sequence，有缺口可补拉。 |
| `GET /api/candidates/:id/review` | diff 摘要、完整 diff 引用、Bundle/Gate/qualification。 |
| `GET /api/artifacts/:id` | 每次鉴权；按类型返回受控内容或短期访问方式。 |
| `POST /api/approvals/:id/decision` | command_id、expected_version、decision、reviewed_object_digest、reason。 |
| `POST /api/approvals/:id/revoke` | 按稳定 approval_id/command_id 撤销当前授权，包括已消费但尚未派发的批准；返回被阻断步骤、可能在途步骤及已发生事实。 |

## 检查点 16.1：先实现只读的真实状态

1. Run 列表显示当前状态、phase、`scripted-fake/live`、冻结 completion_mode、native/enterprise scope、候选 ID、更新时间和停止原因。未知字段显示“未提供/未执行”，不能用零或绿色默认值填补。
2. 详情页分别呈现业务候选验收、平台资格、发布、CI、部署；不要用一个“成功”徽标覆盖全部层次。`SUCCEEDED` 解释为该 Run 声明范围完成。
3. 时间线按服务端 sequence 和因果关联显示，保留 UTC 原始时间并用用户时区展示；跨系统仅按时间戳排序不能证明因果。事件到达乱序时去重并补拉。
4. CheckRun 展开看到 physical attempts、首次失败、重试原因、assertion 与归因，不隐藏不利证据。
5. 所有 loading/empty/error/stale 状态可见。API 失败不显示“0 个问题”；显示无法确认当前结果。
6. 预算展示 Run 总额和 CODING/DISCOVERY/FINAL/RECONCILIATION/CLEANUP 各池的 limits/reserved/settled、未知用量、Final 保护额和剩余期限。探索额度耗尽时显示“停止探索，核对送验条件”或“使用已保留额度执行 Final”，不能先显示 STOPPED 再让任务悄悄继续。终态的有限对账/清理单独展示，不冒充 Run 恢复。

此检查点使用真实持久化记录和明确标注的 UI fixture 两种数据源，分开页面会话或顶栏模式。fixture 演示不生成实际 Approval，截图不作为业务运行验收。

## 检查点 16.2：把 diff 与证据连起来

审阅页按“任务要求→代码变化→独立结果→发布对象”顺序组织：

- 工单：task_mode、repair_policy、目标、允许路径、需求增量、合同范围和未批准事项；合成样本标注来源等级，不暴露私有seed答案。
- 候选：Stable任务比较base、submitted/current commit/tree、真实Git parents和完整diff；另列模型文件摘要、模型/预处理/schema版本及其变化、修复输入/修复diff。无Python diff的模型制品变化也必须可审阅，大diff可折叠但不能静默截断。
- 验证：当前Final全部适用mandatory与实际断言（适用的 ML-01…ML-06 与该 Release 已批准的新功能/兼容检查；ML-07 单列是否准入）、变更→需求→检查映射、选择理由、未决Finding、证据等级及各版本；历史Stable覆盖另栏展示。
- 治理：适用平台 qualification、当前撤销/权限状态、预算与清理缺口。
- 目标动作：仅 DRAFT_PR_CI 且可信 CHANGED 显示自有 Fork、目标 base branch、拟建 head、动作 `CREATE_DRAFT_PR`、允许路径、有效期和 payload/diff digest；CHANGED 的 FINAL_ONLY 显示未请求发布，NO_CHANGE 显示发布不适用。

若可信 `change_kind=UNCHANGED` 且独立完整 Final 已确认任务满足，任一 completion_mode 都显示 `NO_CHANGE`，publication/CI=`NOT_APPLICABLE`，不生成空 PR 或审批请求。CHANGED 的 FINAL_ONLY 成功显示 `SUCCEEDED`、publication/CI=`NOT_REQUESTED`；模式与有无变化不能由前端自行推断。

这里的UNCHANGED是相对任务冻结比较base的完整输入相等，不是相对某次Coding输入无变化。对新功能发布验收，额外显示“候选已接受/仍有阻断”与“是否发生修复”，不要把所有成功Run计成AI修复成绩。repair_policy=DISABLED且CODING池为0是正常验证配置，UI不应因此提前显示预算耗尽。

显示简短摘要不替代原始证据入口。原始日志默认脱敏、分段读取，并保留访问审计；浏览器不直接访问业务 DB，也不接收永久对象存储密钥。源码/日志中的 HTML、Markdown 或链接是数据，使用安全渲染与协议 allowlist，不能允许脚本或任意危险链接执行。

## 检查点 16.3：生成精确的 Approval 请求

Approval请求仅可由completion_mode=DRAFT_PR_CI且可信change_kind=CHANGED、当前Final合格的Run生成；NO_CHANGE/UNCHANGED不进入审批路径。服务端从冻结对象生成请求，至少绑定tenant/project/run、task_mode、completion_mode、change_kind、完整FrozenRunContext/需求增量/当前ChangeSet摘要、candidate tree、commit/真实parents/任务base、完整ML release manifest及模型/预处理制品摘要、ContractBundle、VerificationBundle/GateDecision、qualification、目标repo/branch、CREATE_DRAFT_PR、允许路径、expiry/nonce与请求版本。外部候选即使没有CodingAttempt也可在这些门槛满足后进入人审；actor/role来自认证上下文。

首期本地身份可以使用明确标注、服务端配置的开发 principal；只用于隔离环境协议验证。它不构成企业 SSO/RBAC 已实现的证明。真实远端发布前必须有适用发布主体和权限资格；审批对象作者与审批人按既定职责分离策略校验，例外须显式记录。

UI 先展示服务端返回的完整待批对象，并保存 `reviewed_object_digest`。点击批准时提交这个摘要和 `expected_version`，服务端重新读取当前 candidate/base、合同撤销、Final 与平台资格；若变化，返回冲突并要求刷新审阅。不能静默批准 UI 未展示的新候选。

以下是项目伪代码，不是已有 API：

```text
decideApproval(authenticatedContext, request):
  authorize actor for this tenant/project/action
  read approval request and current candidate qualification
  require frozen completion_mode is DRAFT_PR_CI and action is CREATE_DRAFT_PR
  require trusted change_kind is CHANGED and run is not NO_CHANGE
  require expected_version and reviewed_object_digest match
  require candidate/base/bundle/Final still match and not revoked
  require expiry/scope/separation policy valid
  record immutable decision, actor identity and reason in transaction
  append audit/outbox notification
  return recorded decision and current effect status
```

第 17 章 Gateway 将批准消费绑定到唯一 operation，再对每个步骤做独立发送准入。单击批准、批准已消费、outbox 待派发、步骤已取得发送资格、远端已确认之间必须分别显示状态。双击、浏览器重试、响应丢失使用稳定 command_id，对账已登记决定；不生成两个批准或两个 operation。

批准的三个轴分开保存和展示：历史 `decision_record`；`consumed_by_operation_id/consumed_at` 使用绑定；`revoked_at/revocation_epoch/revoked_by/reason` 当前撤销事实。**已消费不等于不可撤销，也不等于已发送。** 不得以 `status=consumed` 为由隐藏撤销入口或拒绝撤销；历史批准和消费记录仍保留，撤销追加新的事实。

## 检查点 16.4：拒绝、撤销和过期必须同样完整

拒绝保留理由和审阅对象，后续修复产生新候选和新审批请求。过期不自动延期。撤销 API 对稳定的 approval_id 及不可变绑定执行鉴权，以 command_id 幂等记录；消费绑定变化本身不是“不能撤销”的理由。撤销事务与第 17 章步骤发送准入事务按同一锁顺序锁住授权行，更新 revocation_epoch、审计与通知一起提交；UI 不靠删除 outbox 来兑现撤销。

服务端响应至少区分 `blocked_step_ids`、`dispatch_committed_step_ids`、`observed_completed_step_ids` 和 `unknown_step_ids`。撤销先于步骤发送准入提交，该步骤及后续未准入步骤不得发送；步骤发送准入先提交，本步已经进入“可能在途”范围，撤销仍阻止后续步骤，但不能保证本步尚未发生。网络在事务外，这个数据库顺序不等于按钮点击时间或远端接收时间。

界面因此可以显示“授权已撤销；2 步已阻断，1 步可能在途待确认”，不能只显示“全部取消成功”。即使底层请求尚未来得及真正写出，只要无法证明它从未发出，也按在途/UNKNOWN 对账；若 Gateway 能确认未发送，另存可信回执更新展示。已创建 branch/PR 等事实保留，不默认删除远端对象。撤销已消费的批准也会阻止第二步、第三步等尚未发送动作；每步必须重新检查最新撤销状态，不能沿用消费时的缓存决定。

若审批服务响应未知，前端显示“决定状态待确认”，按 command_id 查询；不能乐观更新成批准成功再在后台补失败。服务端重启后从账本恢复，不依赖浏览器 tab 的 React state。

当前资格与历史决定分开显示。例如“昨日曾批准 candidate A，当前候选 B 未批准”。不删除旧批准以营造一致状态，也不能复用旧 nonce。

## 检查点 16.5：浏览器验收与证据

未来命令：本章实现脚本后才可执行。端口取第 03/04 章实际配置，不在本章虚构服务 URL。

```sh
pnpm run console:dev -- --profile local-isolated
pnpm run console:review-check -- --run <Run-ID>
```

| 场景 | 预期结果 |
|---|---|
| 审阅合法当前候选 | 规则、diff、Final、目标清楚；批准绑定同一摘要。 |
| 审阅过程中候选/base 改变 | API 冲突，界面刷新前不可批准。 |
| 用户伪造 body 的 actor/tenant | 服务端忽略或拒绝，按真实认证范围判定。 |
| 跨租户猜测 artifact ID | 每次读取被拒绝，不因知道 hash 获得内容。 |
| 双击批准/响应丢失 | 一个逻辑决定，查询可恢复，不盲目新建。 |
| 批准已消费、outbox 待派发时撤销 | 撤销 API 接受；UI 显示已阻断，Gateway 不发送。 |
| 第一步完成、第二步尚未发送时撤销 | 已发生事实保留，第二步及后续步骤被拒绝。 |
| 撤销与步骤发送准入竞争 | 按 DB 提交顺序展示阻断或可能在途；不虚构远端撤回。 |
| Final 通过但资格缺失 | 明确阻断原因，按钮禁用只是提示；服务端也拒绝。 |
| 日志含脚本/异常 URL | 安全展示，不执行；证据仍可在受控方式读取。 |
| 原始证据缺失或 API 故障 | 展示缺口/无法确认，不能显示 100% 绿色。 |
| 批准后撤销但请求已在途 | 如实显示对账状态，不虚构外部撤回。 |
| CHANGED 的 FINAL_ONLY 已成功或仍在执行 | 发布/CI 标记 NOT_REQUESTED，服务端拒绝创建审批或切换模式。 |
| 任一模式下 UNCHANGED 独立 Final 成功 | NO_CHANGE，发布/CI 标记 NOT_APPLICABLE，服务端拒绝审批/空 PR。 |
| DISCOVERY 耗尽但 Final ready | 显示 FINAL_VERIFYING 与保护预算，不显示 STOPPED 后“恢复”。 |

拟建证据 `reports/console/<review_session_id>/` 保存脱敏截图、浏览器测试结果、对应 Run/Approval/command ID、冲突与拒绝的服务端记录。截图只能证明显示效果，权限结论必须有服务端证据。

完成检查：

- [ ] 页面信息来自已鉴权 projection，错误/过期状态诚实可见。
- [ ] 五类事实分开，fake/live 与本地身份边界清楚。
- [ ] completion_mode 不可变，FINAL_ONLY 不进入审批/发布流程。
- [ ] 用户看到的对象与服务端批准对象摘要一致。
- [ ] 权限、版本、撤销、职责分离由后端强制。
- [ ] 消费与撤销分轴，消费后待派发及步骤间撤销均有效；在途边界诚实显示。
- [ ] 五池预算、Final drain 与终态有限清理分别显示；NO_CHANGE 不生成审批。
- [ ] 重试、双击、拒绝、撤销、响应未知可恢复。
- [ ] 本章没有直接连接或调用真实发布接口。

下一会话可复制：

> 请按第 16 章先实现 Run 详情和候选审阅两页，用第 10/14 章真实只读 API。展示执行模式、三轴结果、首个失败、当前 candidate/bundle、mandatory 覆盖和缺口。不要先做发布按钮或自动审批。检查日志安全渲染、跨 scope artifact 拒绝和候选变化时的 stale 提示；记录截图与对应服务端证据。完成后再实现精确审批协议，actor 必须来自认证上下文。

