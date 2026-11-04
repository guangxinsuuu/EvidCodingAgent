# 17｜把批准的候选发布为自有 Fork 的 Draft PR

> 实现状态：**NOT_STARTED**。本章没有创建 Fork、分支、PR、评论或 CI 运行。远端地址、账户权限和真实发布资格仍需在实施时确定；阅读或批准本手册不等于授权任何业务合同或外部动作。

本章只处理准入时已冻结 `completion_mode=DRAFT_PR_CI`、可信 `change_kind=CHANGED` 且已通过独立 Final 的 Run：经用户对具体对象批准后，发布到明确指定的自有测试仓库/Fork，取得与同一候选 head 对应的可信 CI 结果。本模式的 `SUCCEEDED` 表示发布与检查完成，不表示合并、部署或线上恢复。CHANGED 的 `FINAL_ONLY` 完成 Final 并解决相关 UNKNOWN/清理阻断后直接结束，publication/CI=`NOT_REQUESTED`；UNCHANGED 且独立完整 Final 确认任务满足时，任一 mode 都进入 `NO_CHANGE`，publication/CI=`NOT_APPLICABLE`。本章入口拒绝 FINAL_ONLY、UNCHANGED/NO_CHANGE，不能新增审批、空 commit/PR 或暗改 mode。

依赖 [11 对账](11-recovery-and-budget.md)、[14 Final Gate](14-final-gate-and-evidence.md)、[15 安全资格](15-security-and-qualification.md) 和 [16 控制台与审批](16-console-and-approval.md)。先完成本地 dry-run 和模拟远端故障测试，再使用真实凭据。企业 Jira/Slack/文档写回不属于本章默认副作用链。

RELEASE_VERIFICATION与INCIDENT_REPAIR共享本章发布链。Release候选相对Stable有真实功能变更，即使未经Codex修复也可以通过验收并请求精确人审；是否运行Coding不作为发布资格。change_kind按冻结任务base判断，不能误把“无修复”当UNCHANGED，且全部批准新需求必须通过。

本章发布对象为 ML Release 的冻结交付闭包。审批 payload 和 CI 除准确 Git head 外，还须绑定模型制品摘要、预处理/输入 schema、模型加载与部署配置；模型文件走外部制品库时绑定不可变对象及来源回执。任何字节或引用变化都使旧候选批准失效。Draft PR/可信 CI 不等于模型已推广到生产；alias promotion、流量切换与回滚是另行授权的部署动作。

## 17.1 批准的是具体对象

发布请求至少绑定：

```text
tenant_id, run_id, task_mode, completion_mode = DRAFT_PR_CI, change_kind = CHANGED,
frozen_context_digest, requirement_delta_digest, current_changeset_digest,
candidate_digest, candidate_commit_sha, candidate_tree_sha,
base_commit_sha, frozen_bundle_digest, verification_bundle_digest,
target_repository_id, target_owner/repository, base_ref, publication_branch,
action = CREATE_DRAFT_PR, title/body_digest,
allowed_changed_paths, policy_version, expires_at, approval_id
```

仓库 ID 与 owner/name 一起登记，避免名称变化或同名目标被混用。候选 commit/tree 必须在 Final Gate 前冻结；验证读取的源码树与将要发布的 commit 必须一致。如果当前实现只冻结文件包，应先实现可信封存器生成精确 Git commit，再进入 Final Gate。

Release的base_commit_sha是Stable比较/发布基线，submitted candidate可能已有多个功能提交；后续修复commit的直接parent通常是修复输入commit，而非Stable。必须核验真实父链/批准祖先关系、完整对象闭包与base→最终candidate全部变更，不能硬要求parent=base，也不能在发布时squash/rebase生成未验收的新commit。候选谱系parent_candidate_id与Git parents分别记录；来源含未授权提交或父链不可核验时阻断。

审批后的候选、base、合同、验证包、目的仓库或动作内容变化，均不能沿用原批准。目标base移动时，首版阻断旧Run的发布；若采用新base，必须重新准入WorkOrder/Run、重新冻结比较基线和候选、验收并取得新的精确批准，保留旧Run关联。不能原地修改FrozenRunContext或在发布时偷偷把补丁应用到新的main。

## 17.2 拟建文件

| 路径 | 职责 |
|---|---|
| `packages/contracts/src/publication.ts` | 发布意图、批准绑定、远端收据、CI Policy |
| `packages/policy/src/publication-admission.ts` | 授权、资格、撤销、预算和精确摘要检查 |
| `apps/action-gateway/src/publication-service.ts` | 仅接受有限发布动作，不接受任意 shell |
| `apps/action-gateway/src/providers/github.ts` | 真实远端适配，execute/observe 分离 |
| `apps/action-gateway/src/providers/simulator.ts` | 响应丢失、延迟可见、冲突等受控故障 |
| `apps/control-plane/src/publication-coordinator.ts` | 操作链推进与对账 |
| `apps/control-plane/src/ci-observer.ts` | 检查精确 head、可信来源与必需检查集合 |
| `tests/integration/publication.spec.ts` | 批准竞争、故障对账与身份验证 |
| `tests/qualification/fork-publication.spec.ts` | 自有测试仓库的受控真实资格验证 |
| `scripts/publication-preview.ts` | 生成不发送的具体发布预览 |

Action Gateway 单独持有短期、最小权限发布凭据。Coding Worker、Verifier、被测代码和 PR CI 都不能取得这份凭据。

## 17.3 小步实现顺序

### 步骤 A：实现完全离线的发布预览

从冻结候选和 VerificationBundle 生成预览：目标仓库、base/head、完整 diff、title/body、需要的权限、CI Policy、所有待完成 Gate。payload 必须引用第12章规范化 ML release manifest 的同一摘要，以及相应 Candidate input digest、模型/labels/预处理/schema不可变对象；预览、批准、派发和CI复核同一闭包，不能各自生成字段较少的“发布摘要”。解析归一化后的目标由白名单配置决定，不使用模型给出的任意 URL。

预览必须展示真实已存在的候选文件与证据。没有 Candidate/验证结果时显示缺少什么，不生成假的全绿报告。预览生成属于可逆工作，不需要额外发布批准。

检查点：篡改任何绑定字段后，预览摘要改变；Approval 只接受当前完整摘要。`dry_run=true` 的适配器在代码路径上不能访问写 API。

### 步骤 B：原子预留批准与副作用意图

在一个 PostgreSQL 事务里核验主体、当前租约、Run 状态、冻结的 `completion_mode=DRAFT_PR_CI`、可信 `change_kind=CHANGED`、批准未过期/未撤销、全部摘要、适用资格和预算；将批准的 `consumed_by_operation_id` 绑定到唯一发布 operation；写 EffectOperation、Run→`PUBLISHING`、journal 和 outbox。常规发布要求已有匹配的 `fork-publication` 资格；首次 provider qualification 仅按 17.4 的具体测试任务规则检查前置资格与 RepoOwner 批准，不能要求它先提供尚未生成的远端结果。

同一个批准被两个并发请求消费时，最多产生一个有效逻辑发布。重放相同 operation 返回原状态；相同幂等键但不同请求摘要必须冲突。消费只固定用途和唯一 operation，**不冻结永久执行权、不消除撤销权，也不表示已发送**。`revoked_at/revocation_epoch` 与历史批准、消费绑定分开；已消费但尚未派发的批准仍可撤销，UI 仍显示待派发/已阻断/对账中的真实状态。

“消费批准”只授权所列动作，不自动附带合并、部署、评论、Issue 状态更新或企业文档写回。后续扩展必须新增明确 action 与独立绑定。

#### 每一步发送前的撤销与并发边界

Outbox 记录只是待处理意图；relay 发送队列通知、Worker 领取消息、批准已消费，都不能替代最新授权检查。每个外部写入步骤以及每次真正重发，在持有发布凭据的 Gateway 内按下列协议执行：

1. **短事务作发送准入。** 共享锁顺序为 `Run/相关租约 → ApprovalAuthorization → EffectOperation/Step → BudgetAccount/Reservation`。撤销事务不要求用户持有 Worker 租约，但锁定涉及的行时遵守相同顺序，不能先锁批准再反向锁 Run。发送准入检查最新 `revocation_epoch`/未撤销状态、有效期、候选/目标/action、Run 非终止、当前租约、步骤前置事实和预算。登记唯一 `dispatch_attempt_id`、`step_id`、request digest、observed revocation epoch、nonce、短有效期；将本步置 `DISPATCHING` 并提交 journal。撤销先提交时，本步不得取得发送资格。
2. **事务外执行网络。** 提交成功后由同一受信 Gateway 尽快发送；凭据不得再被排入一个可长时间等待的通用队列。若发送前已观察到撤销/过期，应放弃并记录确定未发送回执；但无法证明未发送时保持可能在途。网络请求不在 PostgreSQL 事务中，数据库 rollback 不能撤回远端现实。
3. **明确线性化点。** 撤销与发送准入的先后，以两者对授权行的事务提交顺序决定。发送准入先提交，本步已跨过“可能在途”边界，即使实际 socket 调用发生在稍后，也不能向用户保证撤销一定阻止本步；撤销仍立即禁止任何后续尚未准入步骤。此边界必须写入 API/UI 和故障证据，不宣称端到端原子网络事务。
4. **记录结果并为下一步重新检查。** 响应成功要读回核对；响应丢失进入 UNKNOWN。下一步、恢复后的重发都重新核验当前撤销状态，不能使用第一个步骤或消费时缓存的批准。SDK/HTTP 客户端不得绕过 Gateway 自动重试写请求；每次物理重发均有独立登记与新的准入检查。

撤销事务即使发现 `consumed_by_operation_id` 非空也必须接受有权撤销的请求，增加 revocation_epoch、追加撤销记录并标记未准入步骤被阻断。不要删除历史 operation 或假装 outbox 没有存在过；已经发出的队列通知也会在 Gateway 被拒绝。已在途步骤只做有界对账，确认未发出的预留才释放到原预算池；未知费用继续占用预留。撤销导致剩余必要发布步骤未完成时，本 Run 不能报交付成功。

这个协议保证“撤销先于发送准入提交，步骤不发送”；它不提供远端 API 未参与的零窗口撤回保证。若未来需要比这个边界更强的语义，必须验证远端条件版本/取消能力或专门出口机制，不能仅增加一次本地读取就声称解决网络竞态。

### 步骤 C：发布精确 commit，而不是逐文件改分支

由可信发布器从封存 Candidate 获取完整 commit/tree，验证 parent/base 和允许路径，然后按远端适配器能力发布。推荐语义为：准备精确不可变对象 → 创建该操作专属 branch/ref → 创建 Draft PR → 重新读取并核对。

Branch 名称由服务生成且唯一绑定 Run/operation；不能让模型选择覆盖已有工作分支。若分支已存在，读取其实际 head：等于期望可继续核对，不同则冲突，首版禁止强推覆盖。

每个会产生可见远端变化的边界都有独立、稳定的 EffectOperation 或明确子步骤，分别能 observe。准备 Git 对象和创建 PR 不是一项跨远端事务；部分完成必须能够解释和接续。

例如 branch 已创建但 PR 尚未创建时收到撤销：保留 branch 收据，PR 步骤不得取得新的发送资格。继续 observe 已在途步骤不授权补做剩余发布，也不默认授权删除 branch；任何远端补偿需要其本来就具备的独立范围与批准。

逐文件 Contents API 可能产生多个中间 commit，并在可变 base 上形成未经验证的树；不能直接照搬旧演示的写法。无论使用 Git 传输还是 provider API，必须证明远端 head/tree 与批准 Candidate 相同。

检查点：多文件新增/修改/删除、重命名、mode 变化在 CandidateManifest 中明确表示；不支持的类型在 Admission 阻断，不能静默丢文件。不得只比较 PR 标题或文件数量。

### 步骤 D：响应未知时先观察

发生超时或连接中断后，按 operation 查询：分支/head 是否已存在，PR 是否关联相同 repo/head/base，Draft 状态、正文摘要和文件树是否相符。查到多个对象、身份不匹配、远端已有冲突，均停止自动写入。

远端未查到可能是延迟可见、权限不足、分页遗漏或查询失败。先返回 `INCONCLUSIVE`；只有 adapter 能给出可靠的缺席证据且重试策略允许时，才使用原逻辑 operation 继续。不得换 branch/幂等键绕过未知状态。

即使观察确认缺席且技术上可以安全重发，已撤销/过期批准或 STOPPED/CANCELLED Run 也不能重新发布；此时只归档缺席与残留。对账本身从第 11 章 RECONCILIATION 池取得有限额度，不能借“对账”新发业务写入。审批等待、常规发布/CI 成本按 `budget_pool=FINAL, final_purpose=DELIVERY` 的独立用途账受控，不能挤占 `final_purpose=MANDATORY`、恢复或清理额度；采用第 11 章 `final_purpose_limits/reserved/settled` 的同事务守卫。

首次接真实 provider 时核验官方文档与实际测试结果，记录 API 版本、权限、分页、rate limit、幂等和观察限制。本手册中的抽象协议不是服务端能力保证。

检查点：模拟服务创建 PR 后丢响应，恢复得到同一 PR；创建 branch 后进程退出，恢复不新建第二个 branch；observe 返回 403 时不会重复发布。

### 步骤 E：CI 验收必须核对身份与来源

CI Policy 冻结以下字段：目标仓库、`expected_head_sha`、必需 check 集合、可信 App/workflow 身份、workflow 定义来源与摘要、允许触发方式、总等待期限和允许的重跑策略。

只接受精确 head 的必需检查。零 check、少 check、pending、skipped/neutral 等结果是否允许，由已批准 policy 明确定义；首版必需业务检查要求实际执行并成功，不把“没有失败”当成功。分页必须完成，重复/同名 check 按受信来源和执行身份消歧。

候选里的 workflow 不能拥有裁决权。使用受保护来源的可信 workflow/runner 验证候选；不把 PR 可编辑的脚本与发布凭据放在同一执行域。CI 内也要保持候选构建和可信 Oracle/Collector 隔离，防止仓库测试输出伪造通过。

PR head 在等待期间变化即阻断当前发布验证；不得把旧 head 的绿色结果套到新 head。重新运行不能抹去原失败，必须记录原因、执行 ID 与每次结果，禁止“重跑到绿”为默认策略。

检查点：另一个 head 的成功检查、伪造同名 check、缺少必需项、候选修改 workflow、CI 失败后人工点重跑，都会得到符合 policy 的明确结果。

### 步骤 F：归档和关闭

生成 `PublicationReceipt`，包含批准/operation ID、目标仓库 ID、base/head/tree、branch、PR ID/URL、Draft 状态、读取时间、远端核对证据。生成 `CiReceipt`，包含每项 check 的运行 ID、head、可信来源、结论和原始响应摘要。

确认相关 UNKNOWN、完整性和清理阻断已解决，残留事实已登记，再将本范围 `DRAFT_PR_CI` Run 从 `WAITING_CI` 标为 `SUCCEEDED`。保留字段 `merged=false, deployed=false, incident_resolved=not_assessed` 或等价清晰语义。用户后续要求合并/部署时，进入独立 Gate，不复用 Draft PR 批准。

## 17.4 真实凭据与具体授权

先完成所有本地模拟、dry-run 和预览工作。实施真实发布时，必须已有用户指定或确认的自有仓库/Fork、动作范围和可审查候选，且第 15 章发布前平台安全/恢复资格有效。已有明确授权且对象未改变时，不反复询问；缺少目标或权限时只请求缺失的具体范围。

第 15 章较早的记录可能只覆盖当时已有的 Runtime/Worker。完成本章 Action Gateway 和真实 provider adapter 后，先回第 15 章补做**本次实际代码与部署版本**的隔离、权限和恢复资格，覆盖其身份、凭据路径、挂载、网络与策略；再进行首次真实远端演练。未实现组件的旧 `NOT_RUN` 不能被视为通过，新增 adapter 也不能继承 simulator 或旧 Gateway 的资格。只复核新增及受影响的范围，并保留仍有效的其他记录。

单人试点可使用 `verified-local-owner` 身份，不必先实现第 19 章的企业 SSO。它要求：受认证的本地会话；服务端登记的稳定主体；该主体与目标 Git 账户的可核验关联及目标仓库权限核验；真实用户对精确发布对象和资格计划的批准记录。服务端从认证上下文解析 actor，重新检查当前目标权限与撤销状态，不能接受任意 `body.actor`、自报 RepoOwner 或 mock operator。

`verified-local-owner` 与 `local-development` fixture 身份不同：后者只用于本地协议/UI 测试，不得授权真实发布。前者的身份验证方式、会话/主体记录、目标账户/仓库权限核验证据都进入试点审计链；它只满足这个单人限定试点的主体要求，不代表企业 SSO、跨组织职责分离或多租户权限已完成。使用它不放宽 target/action、候选 Final、平台隔离、预算或精确批准要求。

**首次 provider qualification 与常规发布采用不同的准入证据。** 首次演练尚无 `fork-publication` 远端结果，不能要求“先创建真实 PR 证明资格，再允许第一次创建真实 PR”。允许它启动的依据是 RepoOwner 明确批准的隔离 Fork 测试 WorkOrder、冻结的资格计划摘要、既有平台前置资格，以及当前候选 Final 和精确发布批准。

具体实施顺序为：

1. 准备有真实 diff 的合成测试候选，按正常冻结/Final 流程完成验证；资格任务固定 `completion_mode=DRAFT_PR_CI`。
2. RepoOwner 审阅具体仓库 ID、候选/操作清单、`CREATE_DRAFT_PR` 动作、次数/预算/期限、允许故障模拟和清理范围，记录资格计划批准；这不替代该候选的精确发布批准。
3. Admission 校验平台安全/恢复资格、批准的计划和当前对象完全匹配；仅免除“事先已有本次拟产出的远端资格”这一自依赖，其余 Gate 全部保持。
4. 在指定隔离 Fork 执行有限真实演练，采集权限、远端 commit/tree/PR、UNKNOWN 对账、CI 和清理证据。Simulator 的成绩仍单列。
5. 完成后审查结果并签发绑定 provider/目标范围/代码/策略版本的 `fork-publication` 资格；失败不签发。后续 routine 发布必须引用有效且匹配的这份资格，版本或权限范围改变时复核。

首次任务不是通用 bootstrap 豁免。普通修复任务不能在请求体设置一个标志就获得首次测试身份；不能换仓库、增加评论/合并/部署、无限延长测试，或复用该计划为其他 Run 自动授予权限。需要额外范围时重新形成具体计划与准入记录。

凭据由服务端安全配置注入；不要求用户把 token 粘进聊天、文档、shell 历史或候选目录。启动检查应验证权限最小化与目标白名单，不打印 token。真实请求会创建远端资源与触发 CI，不能把它伪装成“连通性检查”。

## 17.5 验收矩阵和证据

| 场景 | 期望结果 |
|---|---|
| 正常单一候选发布 | 唯一 Draft PR，远端 head/tree 与批准一致 |
| Approval 重放/并发消费 | 唯一 operation，完全相同请求可查询原结果 |
| 消费提交后、步骤发送准入前撤销 | 撤销成功登记，待派发 outbox 不赋予执行权，远端零请求 |
| branch 完成、PR 步骤前撤销 | 已有 branch 事实保留，PR 与后续步骤阻断，不自动补偿 |
| 撤销与发送准入事务竞争 | 用确定性屏障分别验证两种提交顺序：撤销赢则零发送；准入赢则最多本步可能在途并对账，后续阻断 |
| 准入提交后进程退出/网络响应丢失 | 保持可能在途/UNKNOWN；不因已撤销而假称远端不存在，不盲目重发 |
| 批准后 base/候选/合同变化 | 阻断，原审批不可使用 |
| 发布成功但响应丢失 | RECONCILING，observe 找到原对象 |
| 已存在冲突 branch 或不同 PR 内容 | CONFLICT，不覆盖远端工作 |
| 发布目标不在白名单 | 本地拒绝，无远端请求 |
| Final-only 请求发布或运行中改 mode | 本地拒绝，原 Run 的完成范围不变 |
| UNCHANGED/NO_CHANGE 请求审批或空 PR | 本地拒绝；publication/CI=NOT_APPLICABLE，不制造新 commit |
| 首次限定资格任务/普通任务伪装 | 前者凭完整前置资格与 RepoOwner 计划准入；后者拒绝，不产生远端请求 |
| 常规发布缺 fork-publication 资格 | 阻断，不能复用首次演练例外 |
| CI 绿色但 head/来源错误 | 不通过，保存身份不匹配证据 |
| 缺少必需 check/等待超时 | WAITING_CI 或明确失败/未决，不置成功 |
| 取消发生在远端提交后 | 停止后续动作，保留远端事实与残留 |

拟建目录：`artifacts/runs/<run_id>/publication/` 保存预览、批准引用、effect 时间线、发布收据、CI 收据和清理状态；`artifacts/qualification/publication/<qualification_id>/` 保存 simulator 与真实 fork 分开的资格结果。公开 PR 正文只引用允许公开的摘要，不泄漏内部合同、凭据或隐藏验收答案。

未来命令：本章实现脚本后才可执行；此处只列不会发送远端写入的预览命令和本地测试。

```sh
pnpm exec tsx scripts/publication-preview.ts --run-id '从运行记录复制实际UUID' --dry-run
pnpm exec vitest run tests/integration/publication.spec.ts
```

真实发布通过有范围的服务接口/控制台触发；不要提供一个默认就 push 的开发脚本。

## 17.6 参考取舍与排障

旧 [repository.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/repository.ts:335>) 的 `reservePublication` 把批准消费、Run 状态和 external transaction 放进同一事务，值得保留这个不变量。新项目还需要 journal/outbox 原子性及完整 Candidate/VerificationBundle/目标绑定。

旧 [providers.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/tool-gateway/src/providers.ts:203>) 通用路径逐文件写入，Kafka 专用分支才进一步核验指定文件与远端 head；[finishCi](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/durable-workflow.ts:294>) 也有 Kafka 专属的 head 校验。新项目把精确候选和可信 CI 的规则提升为所有发布的共同边界，不能依赖 scenario 名触发。

发布停在 UNKNOWN 时先观察，不能删 operation 重来。CI 红灯时区分代码失败、环境失败和访问失败；没有新事实不自动改代码。发现远端与批准不符时冻结后续副作用，保存差异；关闭 PR、删 branch、回退 commit 都是需要明确目标的外部操作，不能作为未经范围约束的自动补偿。

## 17.7 完成清单与下一次请求

- [ ] Dry-run 可展示完整可审查对象，且技术上禁止外部写入。
- [ ] 批准消费、operation、Run、journal/outbox 原子提交；消费与撤销独立保存。
- [ ] 消费后待派发、步骤间、撤销/发送准入竞争均有测试；网络事务外和可能在途边界明确。
- [ ] 精确 commit/tree 发布，远端核对不依赖标题或分支名。
- [ ] 未知结果先 observe；权限/分页问题不会变成“远端不存在”。
- [ ] CI 校验精确 head、可信来源、必需集合及失败历史。
- [ ] 发布前平台资格、首次受限 provider 演练、后续 fork-publication 资格按时序分开登记。
- [ ] Final-only 与 UNCHANGED/NO_CHANGE 不进入审批发布；首次资格任务不能成为绕过其他 Gate 的开关。
- [ ] 完成语义仅限 Draft PR+CI，合并/部署没有隐式授权。

可复制的下一次请求：

> 按第 17 章先实现 publication-preview、审批绑定与本地 simulator 测试，把响应丢失和 CI head 不匹配跑通。只生成具体可审查预览，不写真实远端。之后核对既有平台安全/恢复资格与 RepoOwner 批准的具体隔离 Fork 资格计划，完成首次受限真实演练；不要要求预先已有本次将产出的远端资格，也不要跳过其他 Gate。单独报告外部操作、真实结果及后续常规发布的资格范围。
