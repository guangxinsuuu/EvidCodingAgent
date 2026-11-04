# 10｜把执行进度放进 PostgreSQL

> 实现状态：**NOT_STARTED**。本章只定义待实现的表、接口、脚本与验收。文中的路径是拟建目录；SQL 是设计片段，没有在任何数据库执行。

本章要做出的最小演示是：提交一个合成 WorkOrder，数据库产生唯一 Run 和待派发任务；即使 Redis 暂时不可用，恢复后任务仍能继续；两名 Worker 竞争同一执行资格时，只有一个能提交有效结果。暂时使用 scripted-fake Worker，演示运行协议，不声称完成模型修复。

先完成 [04 工程骨架](04-project-skeleton.md)、[06 合同注册](06-contract-registry.md) 和 [09 缺陷样本](09-seeded-defects.md)。本章先把执行载体做可靠；[12 Coding Worker](12-codex-coding-worker.md) 和 [13 Verifier](13-langgraph-verifier.md) 再把真实工作接入这些接口。

ML Release 的完整候选身份须包含 `source/tree + dependency/build revision + model artifact digest + model identity + input/feature schema + preprocessing revision + runtime configuration` 的不可变 manifest 引用。模型或特征处理变化即使 Git diff 为空也产生新 Candidate/CheckKey；业务队列的 Job ID 与 EGAP 的 Run/CheckRun/ExecutionAttempt 分开。早期实现若暂未支持完整 ML manifest，保持校准范围，不能签发正式发布验收。

## 10.1 为什么数据库、队列和 Graph 各管一件事

PostgreSQL 保存“什么工作已经承诺、谁当前有资格提交、什么证据已经接受”。BullMQ/Redis 负责唤醒 Worker；收到任务不代表获得执行权。LangGraph checkpoint 保存推理节点的引用和恢复位置，不保存第二份可独立修改的 Run/CheckRun 真相。

例如，数据库已经写入 `FINAL_VERIFYING`，进程在发队列前崩溃。如果业务提交和队列发送是两个独立动作，任务可能永久停住。新实现必须在一个 PostgreSQL 事务里写入状态、journal 和 outbox；独立 relay 再把 outbox 通知送到 Redis。发送成功而确认未落库时允许再次发送，消费者靠数据库唯一键和领取事务去重。

这意味着我们承诺的是“可恢复的至少一次派发与受约束提交”，不能声称端到端 exactly-once。外部 API 的未知结果由 [11 恢复与预算](11-recovery-and-budget.md) 处理。

## 10.2 需要新建的文件

以下全部是规划路径，当前不表示存在实现。

| 拟建文件 | 职责 |
|---|---|
| `packages/contracts/src/runtime.ts` | Run、CheckIntent、CheckRun、ExecutionAttempt 的边界 DTO |
| `packages/domain/src/run-machine.ts` | 允许转换、事件和失败原因，拒绝任意状态跳转 |
| `packages/persistence/migrations/001_runtime.sql` | Run、journal、outbox、租约与检查身份表 |
| `packages/persistence/src/runtime-repository.ts` | 显式事务、CAS、唯一键和租约操作 |
| `apps/control-plane/src/outbox-relay.ts` | 领取待派发 outbox、发送通知、确认派发 |
| `apps/control-plane/src/run-coordinator.ts` | 依照数据库状态决定下一步工作 |
| `apps/control-plane/src/execution-admission.ts` | 为 Worker 分配有限执行资格 |
| `tests/integration/runtime-persistence.spec.ts` | 真实 PostgreSQL、多个连接及进程退出测试 |
| `scripts/runtime-demo.ts` | 只创建合成 Run 的演示入口 |

不要直接搬来旧项目的整个 Repository 接口。按事务不变量拆分方法，避免 `updateRun(id, arbitraryPatch)` 成为绕过状态机的后门。

## 10.3 先定身份，再建表

每个对象都有 `tenant_id`，值来自认证上下文。初期只有一个本地租户，也不接受请求体自报租户。所有查询、唯一约束和产物授权都带相同租户边界。

| 对象/表 | 最小字段与不变量 |
|---|---|
| `work_orders` | `id, tenant_id, request_key, source_digest, scope, task_mode, repair_policy, completion_mode, created_by`；Release 另含下述发布输入引用；同一请求键相同内容返回原对象，不同内容报冲突 |
| `runs` | `id, work_order_id, status, state_version, task_mode, repair_policy, completion_mode, frozen_context_digest, stop_reason, exploration_closed`；三项模式/策略入场冻结，状态只能经状态机转换；Final收尾不能重新打开探索 |
| `run_events` | `run_id, seq, event_id, kind, payload_digest, recorded_at`；每 Run 序号唯一，事件追加不覆盖 |
| `outbox` | `id, aggregate_id, aggregate_version, event_type, payload, available_at, delivery_state`；与业务状态同事务创建 |
| `run_leases` | `run_id, holder_id, epoch, expires_at`；协调者接管时 epoch 单调递增 |
| `check_runs` | `id, check_key, intent_digest, execution_set_id, phase, repetition_index, retry_of_check_run_id, status`；逻辑检查唯一，关联重建检查但不覆盖旧事实 |
| `check_execution_attempts` | `id, check_run_id, ordinal, lease_epoch, execution_status, environment_instance_id, fixture_reset_id, evidence_ref`；同一完整身份下的安全物理重试另建记录 |
| `worker_registrations` | `worker_id, role, version_digest, health, last_seen_at`；健康记录不是授权 |

`task_mode=RELEASE_VERIFICATION|INCIDENT_REPAIR` 区分任务入口；`repair_policy=DISABLED|ON_VALIDATED_FINDING` 决定是否允许修复，独立于 `completion_mode=FINAL_ONLY|DRAFT_PR_CI`。RELEASE_VERIFICATION 的 WorkOrder 必须冻结 `stable_release_manifest_ref, submitted_candidate_ref, release_changeset_ref, requirement_delta_ref, historical_coverage_refs`。引用闭包包含稳定版本、提交候选、批准需求变化和历史证据的精确身份；没有需求变化时也用明确的空 delta，不能把缺失解释为已批准。历史覆盖是规划材料，不给当前候选写入 PASS。

Candidate 的 `base_commit_sha/base_input_digest` 始终指任务比较基线：Release 为稳定版本，Incident 为原故障/输入版本。每个 CodingAttempt 另绑 `repair_input_candidate_ref`，不能用修复输入覆盖比较基线。当前候选、父候选和真实 Git parent 分别记录；细节按第 12 章可信接收/冻结器执行。

`CheckKey` 初期定义为版本化规范 JSON 的摘要，输入至少包括：

```text
tenant_id + run_id + candidate_digest + frozen_bundle_digest
+ execution_set_id + phase + repetition_index
+ check_id/check_version + parameter_digest + fixture_digest + seed
+ harness_digest + evaluator_digest + environment_profile_digest
+ environment_instance_id + fixture_reset_id + evidence_policy_version
```

`execution_set_id` 由可信服务为一次已批准的执行集合生成；`phase` 固定为 `BASELINE / DISCOVERY / FINAL`；`repetition_index` 区分统计重复，即使 seed 相同也不能合并。Final 默认使用自己的执行集合，不能因参数相同就读取 Discovery 的旧绿色结果。

将环境实例与 fixture reset 放入键意味着不同干净环境或新 reset 不偷偷复用结果。更换 `environment_instance_id` 或 `fixture_reset_id` 必须建立新的 CheckRun，并通过 `retry_of_check_run_id` 关联原检查；旧失败、UNKNOWN 和证据继续保留。同一 CheckRun 只有在完整键身份未变、已经证实重试安全时才增加物理 CheckExecutionAttempt；UNKNOWN 先对账，不能靠新 attempt、reset 或新键跳过未知效果。

将 `run_id` 放入键意味着首版不做跨 Run 缓存。将来需要复用时，另写可审核的等价条件及缓存策略，不能删除几个字段来“提高命中率”。具体规范化格式沿用 [06](06-contract-registry.md) 冻结的规则，不自己另造一个 JSON 序列化顺序。

## 10.4 小步实现顺序

### 步骤 A：只实现迁移与空库往返

先把迁移文件做成有版本、有 checksum、有执行记录的升级。开发库也要防止并发迁移；新进程发现数据库版本不兼容时退出并解释原因。测试必须覆盖空库安装、重复启动、旧版本升级和中途失败回滚。

先写最小 `createWorkOrderAndRun`，同一事务创建 WorkOrder、`ADMITTED` Run、首条 journal、`RUN_PREPARE_REQUESTED` outbox。不要先接队列。

检查点：两个独立数据库连接同时提交相同 request_key，最终只出现一个 Run；不同 request_digest 必须返回明确冲突，不能用 `ON CONFLICT DO NOTHING` 吞掉差异。

### 步骤 B：把状态转换变成单一事务入口

接口草案：

```ts
// 设计伪代码，非现有导出。
transitionRun({
  tenantId, runId, expectedState, expectedVersion,
  coordinatorLease: { holderId, epoch },
  event, nextState, nextWork
}): Promise<{ stateVersion: number; journalSeq: number }>;
```

事务按固定顺序锁定 Run 行、租约行、相关逻辑工作行，然后检查状态、版本、当前租约及权限；更新 Run；分配 journal 序号；插入 journal 和 outbox；一起提交。所有接管和提交路径遵守同一锁顺序，避免一个路径先锁租约、另一个路径先锁 Run。

设计约束示例：

```sql
-- SQL 设计片段：省略具体字段和参数绑定，不可直接执行。
BEGIN;
SELECT ... FROM runs WHERE tenant_id = $1 AND id = $2 FOR UPDATE;
SELECT ... FROM run_leases WHERE tenant_id = $1 AND run_id = $2 FOR UPDATE;
-- 检查 expected_version、合法转换、holder/epoch 和当前数据库时间。
UPDATE runs SET status = ..., state_version = state_version + 1 WHERE ...;
INSERT INTO run_events (...) VALUES (...);
INSERT INTO outbox (...) VALUES (...);
COMMIT;
```

检查租约使用当前数据库时间；不要用 Worker 的墙钟决定归属。长事务中要理解事务起始时间与当前时间的区别，并在实现中选择一致的检查方式。事务内不进行网络调用，不等待模型或执行测试。

### 步骤 C：实现 outbox relay

Relay 用短事务领取一批到期记录，为该批设置投递租约，然后在事务外发送。发送后另一个短事务记录确认。失败保留原 outbox ID、指数退避和可观察错误；不能通过新建 Run 解决派发失败。

队列载荷只放 `tenant_id, outbox_id, work_kind, work_id` 等引用；消费者从数据库读取当前状态并核验权限。队列中的 Candidate、合同或审批内容不能覆盖数据库已冻结内容。

幂等要落在逻辑工作键和领取事务，不能只依赖 BullMQ 的 `jobId`：队列保留策略、job 删除和重复通知都可能改变队列侧行为。

检查点：分别在事务提交后发送前、发送成功后确认前结束 relay 进程，重启后工作可达、逻辑检查没有重复、journal 可以解释重复通知。

### 步骤 D：实现协调租约与执行租约

协调租约保证一个 Run 的状态推进只有一个当前协调者。耗时 Coding/Check 使用自己的 ExecutionAttempt 租约，不把整个模型执行包在数据库事务里。

协调者失租后停止创建新工作；执行者失租后请求取消本次进程组/环境操作。**停止信号只是尽力而为，最终仍由数据库 fencing 拒绝过期提交。** 旧进程即使继续存活，也不能修改新的 Run 状态、签发副作用资格或覆盖结果。

Worker 不持有万能数据库账户。Coding Worker 通过窄 API 领取 Coding Attempt、上传候选并提交引用；不能直接写 `CheckResult`。Verifier 只能提出 CheckIntent；可信 Runner/Collector 提交检查证据。每个接口检查主体角色与被分配的 attempt。

检查点：让 Worker A 停止续租，B 接管，再恢复 A。A 的心跳、状态提交、结果替换和新副作用请求全部被拒绝。已生成的不可变证据可以保留，是否重新采纳由当前协调者的明确对账流程决定。

### 步骤 E：接 scripted-fake，再接真实 Worker

Fake Worker 只实现 `start / observe / cancel / collect` 协议，输出带 `execution_mode: scripted-fake` 的合成产物。共同 DTO 统一采用 `execution_mode: scripted-fake | live`；它与 provider 名称、环境类型分别记录。Fake 不能生成被标记为真实修复或真实业务检查的 PASS。

先展示 `ADMITTED → PREPARING → BASELINING` 的状态推进和恢复。Release 的 BASELINING 核对 StableReleaseManifest 的资格、来源和轻量环境/Observer 校准，不默认重跑稳定版全部历史检查；缺失或过期的必要资格再按批准范围补验。可信接收外部 Candidate 后直接派发 Discovery，不为通过入口而创建空 CodingAttempt。Incident 入口则核对原故障输入和已验证反例，满足修复授权才派发 Coding。

## 10.5 状态转换的首版边界

WorkOrder 与 Run 在准入时冻结 `task_mode`、`repair_policy` 和 `completion_mode`，并纳入请求摘要与 FrozenRunContext。用户没有要求发布的验证任务使用 `FINAL_ONLY`；Release 可选 DISABLED 且 CODING 预算为零。运行中不能为省略失败门禁、启用修复或增加远端动作而暗改这些值。若完成目标改变，重新准入新 WorkOrder/Run，保留与旧 Run 的关联。

主场景 RELEASE_VERIFICATION 的入口与后续链是：

```text
ADMITTED → PREPARING → BASELINING
  -- RELEASE_CANDIDATE_ACCEPTED --> DISCOVERY_VERIFYING → FINAL_VERIFYING
DISCOVERY_VERIFYING / FINAL_VERIFYING
  -- VALIDATED_FINDING_REPAIR_ADMITTED --> CODING → DISCOVERY_VERIFYING
合格 Final → 按 change_kind + completion_mode 分流
```

| 事件 | 来源 → 目标 | 必要守卫 |
|---|---|---|
| RELEASE_CANDIDATE_ACCEPTED | BASELINING → DISCOVERY_VERIFYING | RELEASE_VERIFICATION；Stable 资格及环境核对完成；外部提交候选经可信封存；ChangeSet/RequirementDelta 及适用合同冻结；Discovery/Final 保护额有效 |
| KNOWN_FINDING_REPAIR_ADMITTED | BASELINING → CODING | INCIDENT_REPAIR；当前输入上的 ValidatedFinding 为可信 CODE_DEFECT；repair_policy=ON_VALIDATED_FINDING；修复 scope、CODING 及后续 Final 预算有效 |
| VALIDATED_FINDING_REPAIR_ADMITTED | DISCOVERY_VERIFYING / FINAL_VERIFYING → CODING | 当前候选有未解决可信 CODE_DEFECT；repair_policy=ON_VALIDATED_FINDING；exploration_closed=false；当前候选绑定为 repair_input_candidate_ref；修复与后续 Final 预算有效 |

Incident 的已知缺陷入口保留如下；无可信缺陷或未授权修复时不派发 Coding，而是记录证据/授权缺口，依事实进入 BLOCKED/INCONCLUSIVE 等状态：

```text
ADMITTED → PREPARING → BASELINING
  -- KNOWN_FINDING_REPAIR_ADMITTED --> CODING → DISCOVERY_VERIFYING → FINAL_VERIFYING
有变更 DRAFT_PR_CI 的合格 Final
  → WAITING_APPROVAL → PUBLISHING → WAITING_CI → SUCCEEDED
```

Final通过、任务满足且相关UNKNOWN、完整性与清理阻断已解决后，必须先按可信Candidate的 `change_kind` 再按completion_mode分流，不能把NO_CHANGE落入一般成功分支：

| 当前状态 / 事件 | 守卫 | 目标与副作用 |
| --- | --- | --- |
| FINAL_VERIFYING / NO_CHANGE_CONFIRMED | UNCHANGED，base_input_digest=candidate_input_digest且可信base commit/tree封存；当前独立Final与任务完成证据齐全 | NO_CHANGE；两种mode均publication/CI=NOT_APPLICABLE，不创建审批或空PR |
| FINAL_VERIFYING / FINAL_ONLY_COMPLETED | CHANGED，mode=FINAL_ONLY，满足全部Final与完成守卫 | SUCCEEDED；publication/CI=NOT_REQUESTED |
| FINAL_VERIFYING / PUBLICATION_REVIEW_READY | CHANGED，mode=DRAFT_PR_CI，满足Final及发布前资格 | WAITING_APPROVAL；后续按16/17精确审批、撤销、发布与CI |

外部提交候选与修复候选都经第 12 章可信接收器进入同一 Candidate Registry。Coder 返回“无修改”且没有快照时，保留本次 `repair_input_candidate_ref` 指向的候选及完整输入，不回退稳定版；按需追加接收/交接记录，再继续当前候选的独立验证。`change_kind` 始终相对任务比较基线计算：新增功能候选即使没有 CodingAttempt 或 Agent 未修改，仍可为 CHANGED。只有相对 base 完整输入相等且任务满足，才可能在当前 Final 后 NO_CHANGE，不能从 CODING/BASELINING 直接终结。历史基线与本次最终执行集合不同，不默认复用旧 PASS。上述无发布路径均不生成发布 Approval、EffectOperation 或 PR；正常内部审查和业务合同批准仍按各自规则执行。

Discovery/Final 发现可信代码缺陷且满足上表修复守卫时才可进入 CODING，必须新建 Attempt，并在产生实际新内容时建立后继 Candidate，保留原候选与证据。预期的新功能差异、环境/访问错误、合同歧义和模型怀疑都不能直接授权修复。repair_policy=DISABLED 时保留有效 Finding 和未达发布要求的结论，不为修复而修改策略。无修改提案但 Final 失败仍是失败/待处理，不是 NO_CHANGE；无修复通过与有效修复分别计量，见第 18 章。

预算协议采用第11章五池：`CODING / DISCOVERY / FINAL / RECONCILIATION / CLEANUP`。执行准入根据可信工作类型决定 `budget_pool`，调用者不能自选FINAL池支付探索；FINAL另绑定 `final_purpose=MANDATORY | DELIVERY` 及用途子额度，发布/CI和审批等待不得侵占强制验收。每次消费在同一事务锁定Run预算总额、对应池及适用用途，核对授权、pool_limits/pool_reserved/pool_settled及final_purpose_limits/reserved/settled后登记reservation和执行意图；未知用量仍占用原池/用途。第11章增加预算表，不能在本章队列层另存可消费余额。

只有当前阶段所需 CODING 或 DISCOVERY 池耗尽、或探索时限到达时，才先关闭新增探索并执行 drain；未请求修复的 Release 任务 CODING=0 不影响 Discovery/Final。协调者在原 CODING/DISCOVERY_VERIFYING 状态检查第 14 章送验条件；如需有界确认未知事实，可经过专门标记为本次 drain 的 RECONCILING，不能以任意来源进入 Final。满足条件且受保护 Final 额度可用，原子写入 `FINAL_DRAIN_READY → FINAL_VERIFYING`、`exploration_closed=true` 和 Final 工作意图；不满足则 `FINAL_DRAIN_NOT_READY → STOPPED` 并保留未完成项。已经在 Final 时不因探索池为零中止适用 Final。已停止、已取消或已失败的 Run 不能使用该事件重新执行 Final；只有限定的对账/清理继续。Final 自身额度/硬期限不足时使用 `FINAL_BUDGET_OR_DEADLINE_EXHAUSTED → STOPPED`；收尾发现缺陷或证据缺口按事实进入相应非成功状态，不借 Final 池重新 Coding 或减少 mandatory。

合同/权限前置条件不满足进入 `BLOCKED`；观测不足进入 `INCONCLUSIVE`；外部副作用未知进入 `RECONCILING`；不可恢复错误进入 `FAILED`；预算或无进展停止进入 `STOPPED`；用户取消进入 `CANCELLED`。每个特殊状态记录 `reason_code` 与可恢复点，不允许统一的任意 `resumeTo` 字段。具体恢复转换按 [11](11-recovery-and-budget.md) 枚举。

`SUCCEEDED` 只表示冻结任务及 completion_mode 对应范围完成：Release 的 Final-only 可证明无需平台修复的提交候选通过验收；DRAFT_PR_CI 还包含指定 Draft PR 与 CI。成功不等于发生了 Codex 修复，也不等于已合并、已部署或线上事故已解决。状态与报告同时显示 task_mode、repair_policy 和 completion_mode。

## 10.6 验收与证据

所有下列测试都必须基于本项目新实现执行；旧项目的测试数量不迁移为成绩。

| 测试 | 预期结果 | 必留证据 |
|---|---|---|
| 正常提交合成 WorkOrder | 单一 Run，journal 与 outbox 同时出现 | 请求摘要、事务后查询、事件序列 |
| Release 外部候选，repair_policy=DISABLED、CODING=0 | BASELINING 后直接 Discovery/Final，无 CodingAttempt | 模式、封存引用、RELEASE_CANDIDATE_ACCEPTED、预算记录 |
| Release 预期变化 / 可信缺陷 / 环境错误 | 仅可信 CODE_DEFECT 且有修复授权时进入 CODING | RequirementDelta、Finding、归因与准入守卫 |
| Redis 不可用后恢复 | Run 不丢失，outbox 最终派发 | 故障时间线、relay 日志、数据库行 |
| 重复通知/重放提交 | 不新建逻辑 CheckRun，不覆盖历史尝试 | 唯一键冲突分类、attempt 列表 |
| 两进程竞争租约 | 一方持有有效执行权 | 两连接记录、epoch、失租拒绝事件 |
| 修改 Candidate 后沿用原 CheckKey | 入口拒绝身份不匹配 | 原/新摘要、拒绝原因 |
| 同 seed 新统计重复或换环境/reset | 新 CheckRun；重建检查关联 retry_of_check_run_id | execution_set/phase/repetition/reset 与旧事实 |
| Final-only 完成及运行中改 mode | 合法路径直接完成、发布/CI 未请求；改 mode 拒绝 | 冻结 mode、Gate 与状态事件 |
| 修复无修改提案、或只有历史 baseline PASS | 保留 repair input Candidate，不能回退 Stable 或直接 NO_CHANGE | 比较基线/修复输入摘要、独立 Final 集合、拒绝事件 |
| 提交候选相对 Stable 有新功能，平台无需修复 | 仍为 CHANGED；Final 成功按 completion_mode 继续 | 初始与最终 Candidate、change_kind、零修复记录 |
| UNCHANGED Final通过 / 失败 | 通过且任务满足才NO_CHANGE；失败不能统计正确不改或修复成功 | NO_CHANGE_CONFIRMED与ground truth分类引用 |
| 探索池耗尽但符合送验 / 不符合送验 | 前者FINAL_DRAIN_READY且关闭探索，后者STOPPED；均不挪用恢复/清理池 | 事务前后总额/池余额、readiness结果 |
| STOPPED收到Final任务或旧探索通知 | 拒绝新业务执行；必要对账/清理按独立资格继续 | 准入拒绝与有界清理回执 |
| 状态提交与 journal 中途失败 | 整个事务回滚，无“半条状态” | 受控故障点与回滚后查询 |
| Worker 自报 PASS | Coding 角色无权写入结果 | API 拒绝记录与权限测试 |

拟建证据目录：`artifacts/qualification/runtime/<qualification_id>/`，记录数据库版本、迁移摘要、应用 commit、进程 PID/身份、故障点、事件、预期与实际。时间和 ID 来自实际运行，示例值不得替代记录。

未来命令：本章实现脚本后才可执行。

```sh
pnpm exec tsx scripts/runtime-demo.ts --profile local-isolated --mode scripted-fake
pnpm exec vitest run tests/integration/runtime-persistence.spec.ts
```

运行前由脚本校验环境 Profile 和隔离数据库身份；命令不接受生产连接串，不在输出打印密码。

## 10.7 常见故障与退回方式

重复派发很常见，先查 `work_id/check_key` 和领取结果；不要删队列或创建新 Run 掩盖问题。租约频繁丢失时看进程阻塞、数据库等待和心跳延迟，不能直接无限增大 TTL。数据库提交成功但 HTTP 响应丢失时，用原 request_key 查询，禁止自动换键。

迁移失败时停止接收新任务，保留数据库快照与错误；按已测试的回退/前向修复方案处理，不删库重来。开发环境需要清理时，只清理标记为该 qualification_id 所有的资源，并保存清理回执。

## 10.8 参考取舍

旧项目 [repository.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/repository.ts:229>) 有真实 PostgreSQL 租约、带 epoch 的更新和原子发布预留，适合学习 SQL 边界。但 [durable-workflow.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/durable-workflow.ts:40>) 将 createRun、journal 和 dispatch 分开；已审查 migrations 没有 durable outbox。新项目必须补上状态与待派发意图的原子提交。

[durable-runtime.spec.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/durable-runtime.spec.ts:5>) 使用 MemoryRepository；可移植测试意图，不能把它当 PostgreSQL 竞争、崩溃或锁行为的证明。完整来源边界见 [90 参考与复用矩阵](90-reference-and-reuse-map.md)。

## 10.9 完成清单与下一次请求

- [ ] 版本化迁移、空库/升级/失败回滚已验证。
- [ ] Run、journal、outbox 同一事务；Redis 故障不会丢工作。
- [ ] CheckKey 区分执行集合、阶段、重复、环境/reset；新 CheckRun 与安全物理重试边界清楚。
- [ ] task_mode/repair_policy/completion_mode 入场冻结，Release 外部候选直接 Discovery，Final-only 不生成审批发布或 CI 请求。
- [ ] 只有可信缺陷及修复授权允许 Coding；未用 CODING=0 不提前终止 Release 验证。
- [ ] NO_CHANGE先于mode分流，具有可信base Candidate和独立Final；Final收尾不恢复STOPPED或已关闭探索。
- [ ] 真实 PostgreSQL 两进程租约竞争与失租提交测试通过。
- [ ] 各 Worker 角色不能跨权写入状态或裁决。
- [ ] 证据已归档；只对已执行范围登记 qualification。

可复制的下一次请求：

> 按第 10 章，从迁移与 createWorkOrderAndRun 原子事务开始实现，只完成步骤 A/B。使用隔离本地数据库和合成数据，先给出可复核证据；不要接真实模型或外部发布。完成后记录实际状态、遗留问题与下一步，不把未运行测试标通过。
