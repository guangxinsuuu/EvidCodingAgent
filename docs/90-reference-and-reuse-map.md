# 90｜参考来源、复用矩阵与证据边界

> 2026-11-04已授权切换到 **Async ML Inference Release Verification**。控制面骨架、file-lab账本与Docker适配器已有实现；旧Eventuate来源/构建证据保留历史，新ML业务尚未由真实运行证明。90.1及90.7的旧仓库快照属于2026-11-02历史读取，不能当作当前文件状态或ML资格。

这份附录的用途是：实现每一章前先找准相应参考，知道可以借鉴什么，也知道什么不能因“旧项目有这个名字”就当作新项目已经完成。它与 [02 工程决策](02-engineering-decisions.md)、[91 决策日志](91-decision-log.md) 一起使用。

## 90.1 本次来源快照

历史读取日期：2026-11-02，按 Australia/Sydney 用户日期记录。新增ML来源在90.6单列，旧表不改写成新业务事实。

| 来源 | 本次确认 | 使用边界 |
|---|---|---|
| [AAA Enterprise-Governed-Agent-Platform](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform>) | Git HEAD=`02c76988bae971875316283d803c97e6623fb623`；工作树有 27 个 tracked 变更条目、33 个 untracked 条目 | 条目含目录，不等于文件数；当前工作树与 HEAD 不同，引用不得写成“该 commit 中必然存在” |
| [Eventuate 上游参考 checkout](../../EGAP-Order-Approval/external/eventuate-customers-orders/README.adoc) | HEAD=`894f2dbbe9aba2be6129748b6bbf5f30356deb07`；本次检查 tracked 工作树干净 | 参考源，不是新项目已通过的运行基线 |
| [v3 图解生产方案](../../EGAP-Order-Approval/output/pdf/EGAP_Eventuate_Visual_Production_Plan_CN_v3.pdf) | 95 页设计；[可编辑正文](../../EGAP-Order-Approval/docs/eventuate-production-plan-v3.md) | 本手册的设计输入，图中 PASS/Approved 是设计状态 |
| [合同配套目录](../../EGAP-Order-Approval/contracts-draft/README.md) | 6 类 Schema/示例，全部 DRAFT、execution_authority:none | 可移植为审查输入，不能自动激活或当业务 Owner 批准 |
| [合同校验报告](../../EGAP-Order-Approval/contracts-draft/validation-report.json) | 结构/引用校验记录；本次只读复核 6 Schema、6 示例与引用通过 | 不等于业务执行、可信 Oracle 或真实审批系统 |

AAA 的变更包括 control-plane、executor、providers、Compose、contracts 等核心文件；untracked 中包括 Kafka investigation/validation 代码和文档。因此本次的实际研究对象是 **HEAD 加当前工作树**。此后若参考文件改变，应重新读取与计算摘要，不能继续沿用本表作为最新事实。

Eventuate 目录有晚于 PDF 生成的 Gradle 缓存及 problems 报告，不能断言后来没有任何 Gradle 活动；本次没有发现可证明原生业务验收通过的测试结果。历史文档的 NOT_RUN 与当前目录痕迹应分开陈述。

## 90.2 哪些材料支持哪一层结论

| 证据类别 | 可以支持 | 不能支持 |
|---|---|---|
| 源码静态读取 | 代码分支、接口、配置值和明确缺口 | 编译通过、真实外部权限、端到端业务成功 |
| Schema/引用校验 | 格式合法、引用闭合、缺少字段被拒绝 | 签名真实、规则正确、Owner 有权、环境安全 |
| MemoryRepository 测试 | 协议意图、状态转换、生成样例 | PostgreSQL 锁/隔离、跨进程竞争、崩溃后持久性 |
| simulator/fixture 结果 | 受控故障下的客户端协议 | 真实 GitHub/Jira/Confluence 幂等及一致性语义 |
| 旧项目报告/测试数量 | 历史上下文及后续测试候选 | 新项目当前 commit 的测试成绩 |
| 新项目真实 qualification | 指定代码/环境/策略/用例范围内的观察 | 未测平台、未来版本或所有攻击者模型 |

所有实现报告都应写明统一字段 `execution_mode`、SUT类型、环境、commit、策略/合同/evaluator摘要。Fake模型发出预定补丁，不是模型真实修复能力；模拟PR和模拟CI也不是外部交付。另以冻结的completion_mode说明任务止于Final还是包含Draft PR/CI。

## 90.3 运行时与持久化复用矩阵

| 实际参考 | 已看到的机制 | 本项目处理 | 对应章节 |
|---|---|---|---|
| [repository.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/repository.ts:229>) | pg.Pool、显式事务、Run 更新、journal 序号、租约 epoch | 学习 SQL 不变量；重新定义 tenant/Run/Attempt/CheckKey、严格状态 CAS 与事务入口 | [10](10-durable-runtime.md) |
| [reservePublication](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/repository.ts:335>) | 同事务消费批准、插入 external transaction、Run→publishing | 保留原子预留意图；增加 Candidate/验证包/目标/资格绑定及 journal/outbox | [17](17-publication-and-ci.md) |
| [durable-workflow.create](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/durable-workflow.ts:40>) | createRun 后分别 emit、dispatch | 必须重做为状态+journal+outbox 同事务；不能复制调用序列 | [10](10-durable-runtime.md) |
| [queue.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/queue.ts:1>) | BullMQ 通知、jobId、退避与保留策略 | 作为派发适配参考；唯一工作与恢复资格仍由 PG 决定 | [10](10-durable-runtime.md) |
| [003 migration](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/migrations/003_durable_execution.sql:1>) | worker_leases、run_event_counters、effect_dispatches、reconcile 索引 | 学习租约/nonce 表设计，新增 durable outbox、预算和物理执行尝试表 | [10](10-durable-runtime.md)、[11](11-recovery-and-budget.md) |
| [processTransaction](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/durable-workflow.ts:231>) | executing→unknown→observe、保留幂等键 | 借鉴观察优先；重做 INCONCLUSIVE、可靠缺席证据、operation 特定重试与预算 | [11](11-recovery-and-budget.md) |
| [durable-runtime.spec.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/durable-runtime.spec.ts:5>) | MemoryRepository 上的序号、fencing、批准重放和属性测试 | 迁移测试意图；另写真实 PG 多连接、多进程和崩溃测试 | [10](10-durable-runtime.md) |

已审查的 `001/002/003` migrations 和调用路径没有实现 Run 状态与队列通知同事务的 durable outbox。`external_transactions` 是外部副作用状态表，`effect_dispatches` 是派发资格登记；它们不能自动替代所有内部任务的 outbox。

旧 Repository 有可选 guard 的更新方法，也有先读后改的通用更新；本项目需要对不同写入主体和事务不变量逐一审核，不把“数据库里有 epoch 字段”当所有路径都受 fencing 保护。Fencing 约束平台提交/派发资格，不能撤回远端已经收到的请求。

## 90.4 Agent、验证与安全复用矩阵

| 实际参考 | 已看到的机制 | 本项目处理 | 对应章节 |
|---|---|---|---|
| [executor.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/harness-worker/src/executor.ts:124>) | 独立工作目录、固定 base、移动 .git、调用 DSH runner、生成 proposal | 保留候选冻结与适配器边界；主引擎改为 Codex provider，DSH 只留未来可选适配 | [12](12-codex-coding-worker.md) |
| [executor 环境与测试](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/harness-worker/src/executor.ts:175>) | 模型子进程环境收缩；通用测试继承 process.env；同一 Worker 调用模型与验证 | 必须拆 Coding/Verifier/可信 Runner 与候选构建权限域，取消通用环境继承 | [15](15-security-and-qualification.md) |
| [Kafka validator](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/harness-worker/src/kafka-validation.ts>) | Worker 自有断言、node:vm 内模拟 store、候选源码摘要关联 | 可借鉴独立 Oracle 的方向；实现独立ML Job/模型/预处理/结果及时间序列断言；历史金额Oracle不迁移为ML PASS | [07](07-observer-and-experiment-barrier.md)、[08](08-native-oracles.md)、[14](14-final-gate-and-evidence.md) |
| [contracts/index.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/packages/contracts/src/index.ts:1>) | Zod 边界、类型、canonicalJson、approvalBinding | 选择性移植思路；新 runtime 边界用 JSON Schema Draft2020-12/Ajv2020，业务资产保持单一 Schema 权威 | [04](04-project-skeleton.md)、[06](06-contract-registry.md) |
| [policy.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/policy.ts:1>) 与 [Rego](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/policies/tool-policy.rego:1>) | 工具白名单、保护能力、参数绑定的审批路径 | 学习 allow/ask/deny；补身份、资源、租户、真实路径/链接与上下文权限测试 | [15](15-security-and-qualification.md)、[16](16-console-and-approval.md) |
| [Compose Worker 环境](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/deploy/compose.yml:2>) | Worker 含 DB/内部/签名配置，多服务共享 run-data | 不能当安全部署模板；重建最小凭据、不同挂载与网络、候选构建隔离 | [15](15-security-and-qualification.md)、[19](19-deployment-and-operations.md) |

旧 executor 在同一 Worker 内先调用模型再验证，即使测试代码位于候选工作区外，也不能单凭目录分离证明安全域独立。通用测试会执行候选可影响的配置，并继承 Worker 环境；新设计必须通过 OS/容器、网络、挂载和凭据权限验证，而不是只调整 Prompt。

Kafka validator 的源码注释明确说明：其 store 模拟业务效果与去重的原子操作、幂等 DLT 和单独 offset commit，**不模拟 Kafka broker transactions**。它在同一 Node 进程的 VM context 里执行受限制的候选。因此可学习其断言设计，不能将结果解释为真实 Kafka 故障恢复已通过，也不能用这个 VM context 替代新项目的 OS 执行隔离。

旧 executor 将 `costUsd` 填为零，不能迁移成真实模型成本事实；新预算账本保留 UNKNOWN/ESTIMATED 与实际 usage/账单依据。[11](11-recovery-and-budget.md) 专门处理未知用量与预留。

## 90.5 SaaS、发布与 CI 复用矩阵

| 实际参考 | 已看到的机制 | 本项目处理 |
|---|---|---|
| [FixtureProvider](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/tool-gateway/src/providers.ts:32>) | 内存 effects Map、模拟 PR、直接返回成功 CI | 只作协议测试替身；不计入真实外部交付 |
| [LiveProvider](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/tool-gateway/src/providers.ts:84>) | 同时存在 simulatorOnly、真实 HTTP 入口与凭据读取 | 明确拆模式与能力资格；类名 Live 不足以证明调用真实服务 |
| [effectHeaders](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/tool-gateway/src/providers.ts:109>) | 自定义 x-egap 事务/幂等头 | 保留关联信息概念；不能假设第三方服务兑现幂等承诺 |
| [GitHub 发布路径](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/tool-gateway/src/providers.ts:203>) | 查已有 PR、建 branch、逐文件写 contents、创建 PR | 重做为冻结 commit/tree 发布，操作边界可 observe，禁止可变 base 偷换 |
| [verifyKafkaCandidate](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/tool-gateway/src/providers.ts:238>) | Kafka 专用单文件路径与远端字节/head 验证 | 把精确候选验证泛化为所有发布的共同要求，支持完整变更清单 |
| [finishCi](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/durable-workflow.ts:294>) | Kafka 场景核对 head；通用路径检查汇总结论 | 新实现统一校验 exact head、可信 App/workflow、必需集合和失败历史 |
| [findEffect](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/tool-gateway/src/providers.ts:264>) | 按 branch/title/marker 查 PR、评论或页面 | 加强完整身份、分页、权限失败、延迟可见与 INCONCLUSIVE 语义 |

参考项目的 Jira comment/transition、Confluence change record 是其既有演示链；本项目首个原生闭环只做到自有 Fork Draft PR+CI，不顺带向第三方发消息或更新文档。企业连接在 [20](20-enterprise-connectors.md) 按读权限、数据来源、撤权和写回分别激活。

## 90.6 当前 ML 来源与历史 Eventuate 的边界

当前来源为 [kevin-sparkflow/ml-inference-service 固定提交](https://github.com/kevin-sparkflow/ml-inference-service/tree/c200bcb4721c4428a9bfe808ca8a61648a2b16f6)，commit `c200bcb4721c4428a9bfe808ca8a61648a2b16f6`。固定源码是 FastAPI + Redis Streams + ONNX Worker 的 MobileNetV2 图片分类示例，共27个tracked文件；文件清单与逐文件摘要以 `sut/ml-inference/upstream.lock.json` 为准。源码冻结不等于构建、模型下载或推理已成功。

静态审计确认的边界：`POST /predictions` 接收multipart图片并返回202/Job ID；查询状态为 `queued/processing/done/failed`；health只反映Redis连接。Worker包含XAUTOCLAIM与Lua fencing机制，但实际崩溃恢复仍待测试。Redis AOF everysec和TTL3600不能支持未经限定的零丢失、永久结果保留承诺。没有逐Job模型身份字段、HTTP幂等键或真实信用评分模型；`/scoring-jobs`是附件示意，不是本源码接口。

| 输入 / 已有资产 | 当前落点 | 仍需证明 |
|---|---|---|
| 固定ML源码与完整文件清单 | `sut/ml-inference/`；03/05来源与运行预检 | Python依赖/镜像/模型字节锁、隔离启动、端口发现、四条API smoke、精确清理 |
| TypeScript骨架、LabLedger、锁与DockerHost | 公共治理与可信环境宿主继续复用 | 新业务运行规格、模型制品处理、所有权/故障窗口与真实ML运行资格 |
| ML-01…ML-06规则设计 | 06–09新ML Profile、Observer、Barrier与Oracle | Owner语义、独立观察、健康/seed/坏观察器校准；上游缺能力明确为衍生扩展 |
| ML-07模型质量/切片/性能 | 21后续评估Profile | 批准数据/标签、指标、统计方法、性能环境、门槛；不预填质量PASS |
| 旧Service/Business/Verification/Acceptance/Bundle草案 | 仅借用结构、权限及引用闭包设计 | 新业务正文与摘要、真实批准、适用性、注册检查与独立资格；不能直接激活订单examples |
| Eventuate固定源码、Gradle构建、JAR与实验账本 | 历史对照，退出默认业务主线 | 不转换为ML基线或ML检查PASS；以后恢复作为第二业务需重新准入 |

历史Eventuate业务曾计划EV-C01/C03/C04/C05/C06（回复持有、足额、等额、额度不足、未知客户）。这些检查和金额断言不能重命名为ML检查；93保留原ID并逐项登记新业务适用性。旧文件与证据不覆盖、不删改，也不据此继续启动订单栈。

ML首个主Demo使用自有衍生Stable V3.4→V4.0。Stable先建立模型/预处理manifest和可信观察，V4增加模型B、preprocessing/schema v2及批准v1兼容；late-alias-lookup是自有人工变异。模型加载身份和结果持久化需要真实独立证据，不能仅通过应用日志或Coder声明确定。

## 90.7 本次读取的文件摘要

下面是当前工作树文件的 SHA-256，不是 commit hash，不是安全签名，也不是已归档源码副本。它们用于辨认本次审查对象；后续复用前重新计算，发现变化就复核相应结论。

| AAA 相对路径 | SHA-256 |
|---|---|
| `apps/control-plane/src/repository.ts` | `b655018fc13f8f7bcd468eba1355f11d7a8d293f1cd4da2ba57e8dc9d1278e0c` |
| `apps/control-plane/src/durable-workflow.ts` | `e6790fe898d9b847ed04ac8b6a4ee687a098b7b5518f7636b8f52b3b46ecb036` |
| `apps/control-plane/src/durable-runtime.spec.ts` | `4b33a64db9bc3f495831ffe8a3e39abfabb9a4b78b5809d6fef11c75c1df8cdc` |
| `apps/control-plane/src/queue.ts` | `29d5147fe7d8a5e0bacc2a28a66258e0c3212b75f5565515d4348afcf603ecf5` |
| `apps/harness-worker/src/executor.ts` | `1f3de2fa3e224819c251b216c76edf12c5832b4dd04e3921e2833d782b5bbc8f` |
| `apps/tool-gateway/src/providers.ts` | `8e7be7462d666ac8fab111c18068df82db80f48792c0afd08de70c5c9a87509c` |
| `apps/control-plane/migrations/003_durable_execution.sql` | `53d1a7238bcf33156e8db581b1b40691c7b1b7b84f65a17e92038447609bc90c` |
| `deploy/compose.yml` | `39e809d5dcec91c9aa631b105400b34083dbd6400054c65e416a24c904d437f4` |

现有只读命令：仅用于重新核对参考，不启动应用或修改参考工作树。

```sh
git -C '/Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform' rev-parse HEAD
git -C '/Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform' --no-optional-locks status --short
```

不要通过 reset/clean 让参考仓库与 HEAD 一致；用户的未提交工作是重要来源。需要稳定归档时，另建只读快照，列明包含的工作树差异、来源与许可，而不修改参考。

## 90.8 每次复用的四个小检查点

1. **定位事实。** 读取实际实现及调用者，记 source path、commit、dirty 与文件摘要；不要只读 README。
2. **提取不变量。** 写清复用的是哪个行为或事务保证，以及依赖的环境/身份前提。
3. **适配新边界。** 对齐新对象、状态、JSON Schema、角色与证据接口；只移植必要切片。
4. **重新证明。** 正例、反例、真实依赖和失败注入分别执行；保存新项目自己的结果与缺口。

若复用切片改变了安全域或外部动作，先更新 [91](91-decision-log.md) 的决策与 qualification 范围；若只改可逆本地代码且已在任务范围内，直接实现并验证，不新增无理由的审批环节。

## 90.9 本次核对的官方接入资料

核对日期：2026-11-02。这里只记录实际读取的接入说明；产品行为可能随版本变化，实施时固定CLI/SDK/库版本并重新核对。我们自己的安全域、账本和验收设计不属于这些产品自动提供的保证。

| 官方来源 | 本次用于什么 |
| --- | --- |
| [Codex非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode) | 第12章的非交互进程、JSON事件和受限工作区参数；具体版本再次验证 |
| [Codex TypeScript SDK](https://learn.chatgpt.com/docs/codex-sdk) | 作为未来可替换CodingProvider的程序化入口 |
| [LangGraph JS Persistence](https://docs.langchain.com/oss/javascript/langgraph/persistence) | 区分checkpointer与store，计划持久化与EGAP权威账本分别设计 |
| [Ajv JSON Schema](https://ajv.js.org/json-schema.html) | 第06章使用与草案方言匹配的2020-12校验入口 |
| [BullMQ Idempotent jobs](https://docs.bullmq.io/patterns/idempotent-jobs) | Worker需能应对重试；本项目的DB/outbox/外部对账由自己的协议实现 |

没有在本文指定最新模型、费率或“最新最佳”依赖组合；版本锁和成本证据由03/04/12/18的实际实现建立。

## 90.10 完成清单与下一次请求

2026-11-03定位修订另参考[用户提供的发布验证讨论](</Users/neilsu/.codex/attachments/42051482-4719-46fe-949a-929edccc5e06/Pasted text.txt>)及本轮明确指令：主价值转为稳定版本之后新增功能/大改动的针对性验证，Incident Repair保留。该材料是设计建议，不是当前源码已具备功能或V3.4真实线上历史的证据。最小新功能与检查范围已在00、06、08、09明确；未复制粘贴材料中的CSS，也未将其假设版本号写成上游事实。

本轮交付后，docs修改需用户明确允许；本附录的维护清单不构成自动改文档授权。

- [ ] 已确认参考 HEAD、dirty 状态与引用文件摘要没有被当成同一概念。
- [ ] 每项复用有对应新章节、待建文件与新验收，未直接迁移历史成绩。
- [ ] 内存测试、模拟 SaaS、同 Worker 安全域、无原子 outbox 的限制已进入实现计划。
- [ ] ML原生能力、自有衍生扩展、人工seed与历史Eventuate证据分开。
- [ ] 参考仓库和已有 EGAP-Order-Approval 未被改动。

可复制的下一次请求：

> 开始实现某一章前，先按第 90 章核对对应参考文件与当前工作树摘要。只提取必要接口和不变量，为新项目重新写边界与测试；参考只读，旧报告不算新实现成绩。把本次实际复用、重做和未验证部分记入进度日志。
