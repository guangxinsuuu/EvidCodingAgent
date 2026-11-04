# 06 契约 Registry：把“正确行为”变成可审查的版本

实施状态：`NOT_STARTED`。本章尚未迁移 Schema、创建正式合同、取得 Owner 批准或激活 Bundle。

前置：[05 真实基线](05-async-ml-baseline.md)。本章分两次完成：先做06.1–06.4的 DRAFT 结构、引用和内容冻结，进入 [07 观察与屏障](07-observer-and-experiment-barrier.md)、08 Oracle 与09.1–09.3 seed 校准；取得真实能力和正反证据后，回到06.5完成真正审批/激活，再按06.6的协议准备输入，由09.4导出正式 WorkOrder。实际 Run 上下文冻结在第10章 Runtime 准入时执行。不得要求 ACTIVE 先于 Oracle 校准，也不能用校准代替 Owner 决策。

## 本章目标

实现一个文件型 Registry 与服务接口：读取五类资产，进行结构与语义校验，解析引用，生成不可变的内容摘要和 Bundle；只有审批、适用范围、Oracle 与环境能力都满足时才可激活。首版把模块放入控制面进程，不为五种 Profile 各建微服务。

首批业务是异步图片推理发布验证。上游只提供真实任务/队列/ONNX基础，模型固定、结果持久性及恢复承诺需逐项定义与验证。资料描述了什么、源码做了什么、Owner 要求什么和一次实验观察到什么，分别存储。Agent 可草拟规则，不能自行把实现行为宣布为正确业务。

09的增量发布切片返回本章时，需建立新的需求增量与Acceptance版本：旧不变量继续适用，获准新功能可改变旧行为；不能用“必须与Stable完全相同”否定合法新增功能，也不能把所有差异都当作获准变化。主任务是验证已提交的新版本，Coding修复是Finding之后的可选动作。

## 五类文档与本手册新增内容

| 资产 | 回答的问题 | 首批内容 |
|---|---|---|
| Business Contract | 什么行为正确 | 模型/特征绑定、结果后成功、重复投递与恢复、schema兼容 |
| Service Profile | 如何定位和接入 | 固定源码、API/消息、构建、能力、允许修改范围 |
| Verification Profile | 如何观察和判断 | ML检查目录、适用子集、参数域、Oracle、屏障与证据要求 |
| Acceptance Profile | 当前阶段怎样放行 | ml-release-final 适用强制集合、失败策略、证据等级 |
| Environment Profile | 在什么前提下成立 | 真实镜像、Redis/Worker、模型工件、隔离、fixture、窗口、清理 |

BundleManifest 绑定这些资产，不再复制一套业务规则。Run 的 candidate、环境实例、租约、seed 等随执行变化，放在 `FrozenRunContext`，不塞进业务合同。

旧资产可参考 [contracts-draft](../../EGAP-Order-Approval/contracts-draft/README.md)。现有六份示例是 planning-only / execution_authority=none，结构校验不是生产 Registry。迁移时保留来源与新版本说明，不在旧目录原地改 ACTIVE。

## 计划新增文件

| 路径 | 内容 |
|---|---|
| `registry/schemas/` | 五类 Profile 与 Bundle 的新版本 JSON Schema |
| `registry/drafts/async-ml-inference/` | 迁移后的 DRAFT 与未决问题 |
| `registry/frozen-drafts/` | 内容不可变的校准快照；仍为 DRAFT，不能用于正式放行 |
| `registry/releases/` | 冻结内容，仅由受控发布器写入 |
| `packages/contracts/src/registry/` | 公共 DTO、错误码、生命周期枚举 |
| `packages/domain/src/registry/` | 语义校验、解析、生命周期和激活策略 |
| `packages/persistence/src/registry/` | 第10章接入 DB 时实现；本章先定义协议并用 file-lab 记录 |
| `apps/control-plane/src/registry/` | 受认证 API；主体来自认证上下文 |
| `tests/unit/registry/` | 结构、闭包、摘要与状态转换测试 |
| `tests/integration/registry/` | 本章测单机文件竞争/撤销/读取；DB多Worker测试留第10章 |

JSON Schema Draft 2020-12 用 Ajv2020 校验；内部 TypeScript 类型由同一契约约束。不要把业务字段再独立写成另一套 Zod 规则并让二者各自演化。

## 小步实现

### 06.1 迁移结构，先让错误可解释

盘点旧 Schema 的字段及九类负例，记录哪些保留、哪些改变、为什么。原 `https://egap.invalid/...` 是 Schema 标识，不是需要访问的网络服务；首版 validator 禁止自动抓远端 `$ref`，只解析注册的本地 Schema。

未知字段默认拒绝；扩展放受控命名空间。禁止在合同里放任意 shell、SQL、动态 import 或可执行自然语言。`assertion_ref` 只指向已注册 Oracle 符号和版本。

错误应含 `asset_id/version`、JSON pointer、reason_code 与修正方向。`MISSING_APPROVAL` 与 `INVALID_SCHEMA` 分开；不能所有失败都返回“配置不合法”。

检查点：合法 DRAFT 可读，关键 null 被保留；DRAFT 不需要伪造批准才能保存；未知字段、错误类型、悬空引用被拒绝。

### 06.2 固定 ML 业务域、规则与检查映射

固定上游是图片分类模板，业务状态原文为 queued/processing/done/failed。合同可用“成功”表达语义，但 adapter/观察必须保留 raw `done`；不能把概念性 SUCCEEDED 当实际 API 字段。首期仅合成图片，无真实个人风控数据；输入格式、尺寸、预处理、标签映射和输出容差显式冻结。

| 规则 | 目标契约 | 检查ID与进入条件 |
| --- | --- | --- |
| ML-01 | 结果在批准持久性边界提交后才能宣布成功 | ML-C01；独立结果/状态因果观察与写入屏障可用 |
| ML-02 | 任务提交时固定模型工件，实际执行与结果身份一致 | ML-C02；先补自有模型绑定能力，再用排队期间别名切换校准 |
| ML-03 | 任务使用符合合同的输入/特征快照与预处理 | ML-C03；有不可变输入、feature/schema/preprocess identity |
| ML-04 | 重复投递不产生冲突业务结果 | ML-C04；批准去重键、效果与重试语义，不假定 exactly-once |
| ML-05 | Worker中断后在界限内恢复或明确失败 | ML-C05；真实崩溃/claim/fencing控制与观察 |
| ML-06 | 新旧输入/输出 schema 与批准升级组合兼容 | ML-C06；版本矩阵、fixture与迁移/转换证据 |
| ML-07 | 满足批准质量、切片及性能门槛 | ML-C07；后续独立数据/标签/统计/硬件协议，目前不激活 |

ML-xx 是新业务规则，ML-Cxx 是对应检查族；它们不是旧 EV/v3 ID 的改名，历史覆盖映射在93保留。目录、已实现能力、适用范围和当前 mandatory 集分开存储。当前优先 ML-C02/01；任何已承诺的 MUST 缺检查/Oracle 都阻断相应范围激活，不能因测试尚未实现就标 N/A。

### 06.2a V3.4→V4.0 的需求增量

Stable V3.4 是自有衍生基线，须先实现并校准不可变模型/预处理 manifest 及可信观察。V4.0 引入受控模型B、preprocess/schema v2并保留**明确批准的**v1兼容，以及Worker组织重构。服务release标签、模型revision、preprocess/schema版本互不混用；主展示不是对未修改上游贴两个版本号。

`RequirementDelta` 逐条记录新增、保留、改变、删除需求及 Owner决定、受影响 API/queue/result schema、适用版本和检查引用。批准行为可以与Stable不同；模型B输出不要求等于模型A，但必须绑定正确工件、遵守输入语义和已批准质量范围。禁止回滚整个候选来删除新功能躲避验收。

兼容矩阵明确旧任务/新Worker、新任务/旧Worker是否支持，输入v1/v2如何处理，无法支持的组合如何拒绝或排空；不自动承诺任意混部、零停机和双向回滚。ML-C06与ML-C02/03共同验证，缺必要观察或语义未决则 BLOCKED。ML-C07 在没有批准质量数据和统计门槛时保持延期，不宣称预测质量已验收。

Acceptance 展开 `mandatory smoke/safety + change-specific + applicable compatibility + release-tier regression`。模型只能调整探索顺序和允许参数，不能削减强制项。历史Stable按旧合同留作对照，不要求它实现V4新增功能，也不能用它代替当前Final。

### 06.3 明确持久性、时间、数值与前提

每条规则要说明主体、触发、输入域、禁止/允许结果、证据源、未知处理。模型工件、输入 snapshot、preprocess/schema、数据集与运行时都纳入事实身份；不可只比两个由候选自述的版本字符串。

ML-01 的“持久化”必须明确：存储已确认、AOF刷盘、进程重启存活还是宿主故障后的恢复。上游AOF everysec与TTL3600秒不支持无限期或零丢失推论。先测真实能力，再由Owner决定保留/恢复承诺，不能由实现者把“不丢结果”写成没有边界的MUST。

区分业务deadline、观察窗口、注入hold时长、重试/lease窗口与TTL。活性窗口从批准的释放/恢复事件开始，冷启动和人为hold不混计；观测超时不制造 failed/expired状态。并行存储/队列没有天然全局快照，以事务序列、受保护轨迹和屏障回执形成因果切面。

预测浮点比较、NaN/Inf、shape/type、标签与阈值采用批准容差和语义；模型更新不要求逐项预测等同旧版。统计质量不能由几个示例预测替代。Owner无需读测试实现也能解释正确行为，QA无需猜意图也能写断言；未决项有 `blocking_activation` 标记。

### 06.4 处理摘要与引用闭包

首版将内容对象、审批记录、激活/撤销事件分开。内容摘要排除自身 digest 和后附签名字段；规范化规则、编码、数字限制、排序与 Unicode 处理必须固定版本，并用 golden fixtures 验证不同进程/语言实现一致。不要直接 hash 任意格式的 Markdown 或依赖 JSON 属性插入顺序。

防止循环 hash：跨资产语义引用使用稳定 ID/version；Bundle 绑定每个资产不可变内容摘要及解析闭包。不得让 A 的最终 hash 包含 B 的最终 hash、B 又包含 A 的最终 hash。若因此改变旧示例字段，创建新 Schema 版本和迁移说明，不无声删除旧 digest 字段。

拟议接口，尚未实现：

```ts
interface ContractRegistry {
  validateDraft(ref: AssetRef): Promise<ValidationReport>;
  freezeDraft(ref: AssetRef): Promise<FrozenAssetRef>;
  resolveBundle(ref: BundleRef, scope: ExecutionScope): Promise<ResolvedBundle>;
  checkUsability(ref: BundleRef, at: string, scope: ExecutionScope): Promise<PolicyDecision>;
}
```

`resolveBundle` 不信任模型提交的摘要；从内容对象重新校验。冻结后同 ID/version 的不同内容必须冲突，修改产生新 revision。未知能力与未解析引用不能当 N/A。

本阶段可冻结 DRAFT 内容及其 Bundle：`bundle_digest` 证明校准用内容没有改变，不证明批准身份、ACTIVE 或正式执行权。`resolveBundle` 区分 `calibration-only` 与正式执行 scope；前者在真实隔离实验授权下可解析已冻结草案，后者拒绝未激活资产。第05章真实 `environment_config_digest` 与运行清单作为来源输入；冻结 DRAFT Environment Profile 后追加绑定其摘要。Oracle 未实现、窗口待测等问题继续阻断激活，但不阻断保存诚实的草案快照。

到此先离开本章，完成07–08和09的校准切片。早期运行由第05章 lab-ledger 协调，不提前依赖 DB Runtime，也不生成带虚假批准的正式工单。

### 06.5 校准返回后：批准、激活、撤销是三件事

生命周期为 DRAFT→IN_REVIEW→APPROVED→ACTIVE→DEPRECATED→RETIRED，已发布版本可 REVOKED。内容 APPROVED 之后，激活仍要核查 scope、检查实现、环境能力、证据等级与未决项。

本地开发可以实现 `local-development` 身份适配器来验证协议，所有记录必须注明其身份保证等级；它不等于企业 SSO，也不能支持真实发布资格。批准主体从适配器的认证上下文获取，不接受任意 body.actor。

激活所需实际业务决策：模型绑定时点、输入/预处理身份、成功与结果持久性边界、重复/恢复语义、兼容矩阵、时间/TTL及数值容差。用户同意编写本手册不等于这些业务条款已获批准。实现校验器、DRAFT CRUD 与拒绝路径无需等待所有 Owner 决策，执行激活则保留真实阻断。

合同 ACTIVE 也不授予任意实验/发布权；运行必须另有 task/environment/action scope。DRAFT 的隔离校准可以在明确实验授权下运行，但只能产生实验性证据，不能伪装正式 final 资格。

撤销追加事件并使新准入和未发生发布失效；保留旧内容及历史结果。已经发生的外部动作交给对账，不因撤销合同删除现实证据。

### 06.6 定义正式运行上下文，交第10章执行冻结

正式 `FrozenRunContext` 至少绑定 tenant/run、repo/base、已激活 bundle digest、候选身份（进入候选阶段时）、runner/oracle 版本、environment profile 与实例、fixture reset、工具权限、预算、seed。早期05–09使用第05章文件型 ExperimentManifest/lab-ledger 及冻结 DRAFT 内容，记录 `authority_mode=file-lab`、`evidence_scope=calibration-only`，不能改个类型名称就充当正式上下文。初次 baseline 无修复候选时绑定明确的 baseline subject，不能用空字符串冒充候选。

同时冻结 `task_mode=RELEASE_VERIFICATION|INCIDENT_REPAIR`、`repair_policy=DISABLED|ON_VALIDATED_FINDING` 和completion_mode。Release输入绑定stable_release_manifest_ref、submitted_candidate_ref、release_changeset_ref、requirement_delta_ref、historical_coverage_refs及发布层级；base为Stable比较基线，修复Attempt的输入候选另记。历史覆盖仅辅助分析/基线资格核对，不能改名为当前候选PASS。

冻结预算策略同时包含第11章各 `budget_pool` 的限额、保护额度、消费权限及全局截止时间；不能只冻结总额、运行时任由模型挪池。具体数值来自本次任务授权和实测，不在草案中假填。

恢复、最终验收、发布前重新检查适用性、撤销与授权。普通新版本不偷偷改旧Run的快照；紧急撤销可阻止旧版本继续放行。批准消费后仍检查撤销：每个后续远端步骤在发送准入前重新核对，已准入/可能在途部分按16/17对账，不能宣称撤销已消除现实效果。

未来命令：相应脚本实现后才可执行，当前不存在：

```sh
pnpm egap:registry validate --scope draft
pnpm egap:registry resolve --bundle <bundle_id>@<version>
pnpm egap:registry inspect --bundle <bundle_id>@<version>
```

首版不提供“强制通过”参数。激活是带身份和决策记录的受控命令，不用手改 JSON 的 status 代替。

## 正负验收与证据

| 情况 | 结果 |
|---|---|
| 合法 DRAFT，仍有阻断问题 | 可保存/审查，拒绝激活 |
| 填入看似正确签名但无可信主体 | 拒绝 APPROVED/ACTIVE |
| MUST 无 mandatory check，或 check 无 Oracle | 闭包校验失败 |
| 新模型/预处理/schema有批准需求但只有API smoke | 当前Release Bundle不能激活/放行；补适用ML检查与校准 |
| 候选获准改变某行为，仍强制使用旧期望 | 适用性/版本冲突，不驱动回滚合法功能 |
| 模型认为不受影响而删除强制回归 | 拒绝；只有获准适用规则可决定集合 |
| 相同内容不同键顺序 | 依批准规范得到同一摘要 |
| 内容变化但沿用同版本摘要 | 冻结/读取时拒绝 |
| 合同已撤销或 scope 不匹配 | 新 Run 与后续放行拒绝 |
| 旧结果属于另一 environment/oracle revision | 不能计入本次覆盖 |

计划保存 `artifacts/evidence/registry/<review_id>/validation.json`、`closure.json`、`digest-fixtures.json`、`approval-decision.json`、`activation-decision.json`。拒绝也是有效验收结果，不能只保存成功样本。

## 排障、回退与完成清单

Schema 校验通过但闭包失败时修引用和适用性，不删强制检查。摘要不一致时检查规范化版本与排除字段，不直接信任其中一个结果。旧草案与新结构不兼容时保留旧版，只在新目录提供迁移输出和差异。撤销误操作通过明确的新授权事件处理，不覆盖历史事件。

- [ ] 五层资产和 Bundle 职责清晰，无第二套业务真理。
- [ ] DRAFT 能正常保存，ACTIVE 不能靠改字符串获得。
- [ ] 当前ML适用集合的规则/检查/Oracle/证据闭包明确。
- [ ] hash、引用、主体和撤销均有否定测试。
- [ ] 第07–09章未完成的校准能力仍阻断正式激活；草案冻结不受虚假批准驱动。

下一会话可复制：

> 按第06章先实现文件型 Registry 的 DRAFT 校验、引用解析和确定性摘要，复用旧六类资产结构，建立ML语义的新版本但不修改旧目录。先完成悬空引用、缺少 Oracle、伪造批准和内容漂移四类拒绝路径；不要虚构 Owner、镜像摘要、观察窗口或 ACTIVE 记录。将激活所缺的真实能力列入第91/92章，再接第07章。
