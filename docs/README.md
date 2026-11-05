# 实施手册：按章节搭建企业 GenAI 发布验证平台

本手册主业务为 **Enterprise GenAI Release Verification & Governed Coding Harness**；被测对象是B2B SaaS续费退款争议的客服处理建议与草稿。TS控制面骨架、file-lab、Docker原语与来源准备机制已有；客服SUT、真实LLM/工具、SUP Oracle、Verifier及发布闭环尚待实现。当前迁移事实以92和实际报告为准，旧ML/Eventuate证据不转换为SUP资格。

先读00–02及13的详细设计，再从03/05的GenAI来源、运行条件和真实SUT切片继续；04骨架与通用账本不需重做。一次实现一个有证据的检查点。

主场景已调整为**稳定版本→新功能候选→变更影响与针对性验证→可信Finding后可选修复→Final与按需交付**。03–08先建立可信实验能力，09的单缺陷用于开发校准；09的最小新增功能发布切片才是G3/G5主要展示，历史稳定结果不充当当前候选PASS。

**文档修改约定，自2026-11-03本轮微调交付后执行：修改docs及项目README必须先获用户明确允许。** 本手册任何“更新91/92、同步文档”的步骤均受此条件约束；“继续实现某章”不自动授权文档修改。可继续已授权代码/测试工作，将真实记录存reports/artifacts，文档差异在会话中提出后等待允许。规则见[项目AGENTS.md](../AGENTS.md)。

## 1. 导航与每章交付

| 编号 | 文档 | 你会实现或理解什么 |
| --- | --- | --- |
| 00 | [总体计划](00-overall-plan.md) | 项目终点、阶段Gate和完成标准 |
| 01 | [系统架构](01-system-architecture.md) | 模块、进程、身份、数据和完整执行链 |
| 02 | [工程取舍](02-engineering-decisions.md) | 技术选择、替代方案、代价和改变条件 |
| 03 | [环境预检](03-environment-preflight.md) | 源码身份、工具链、构建/运行条件与阻断报告 |
| 04 | [项目骨架](04-project-skeleton.md) | TS monorepo、配置、编译、测试和健康入口 |
| 05 | [GenAI客服基线](05-genai-support-baseline.md) | 来源与自有SUT分离、持久Job、真实LLM/工具、草稿、smoke与清理 |
| 06 | [Contract Registry](06-contract-registry.md) | 草案校验、引用闭包、摘要、批准/激活/撤销 |
| 07 | [Observer与实验屏障](07-observer-and-experiment-barrier.md) | 独立工具/模型调用记录、草稿/Job提交与revision屏障 |
| 08 | [Oracle与发布切片检查](08-native-oracles.md) | SUP-01…08，确定性业务/trace裁决与独立语义评价 |
| 09 | [校准与增量发布任务集](09-seeded-defects.md) | 单缺陷校准、Stable→新功能候选、回归对照及答案隔离 |
| 10 | [持久化Runtime](10-durable-runtime.md) | Run/Check/Attempt、数据库事务、Outbox和租约 |
| 11 | [恢复与预算](11-recovery-and-budget.md) | 对账、取消、恢复转换、预算和无进展停止 |
| 12 | [Codex Coding Worker](12-codex-coding-worker.md) | 受控工作区、provider协议、精确候选冻结 |
| 13 | [LangGraph Verifier](13-langgraph-verifier.md) | Context/Impact/Hypothesis/Proposal/Decision、成本/无进展、恢复与提案策略 |
| 14 | [Final Gate与证据](14-final-gate-and-evidence.md) | 当前候选的证据闭包和确定性验收 |
| 15 | [安全与平台资格](15-security-and-qualification.md) | 实际隔离、凭据/权限否定测试和资格版本 |
| 16 | [控制台与精确审批](16-console-and-approval.md) | 查看事实、审阅diff、服务端审批与失效处理 |
| 17 | [发布与CI](17-publication-and-ci.md) | 自有Fork Draft PR、UNKNOWN对账、精确head CI |
| 18 | [公平评估与试点](18-evaluation-and-pilot.md) | 固定候选三组发布QA、漏报/测试成本/人工投入，修复收益另计 |
| 19 | [部署与运营](19-deployment-and-operations.md) | 企业身份、staging、备份恢复、灰度及值班 |
| 20 | [企业Connector](20-enterprise-connectors.md) | 只读来源、ACL、增量、删除、撤权和知识引用 |
| 21 | [业务与平台扩展](21-business-and-platform-extensions.md) | 幂等/并发、取消补偿、第二系统、多Run与新引擎 |
| 22 | [后续路线](22-future-roadmap.md) | 按证据选下一项扩展、维护与退出计划 |
| 90 | [参考与复用地图](90-reference-and-reuse-map.md) | 旧代码定位、可借鉴内容、必须重做的边界 |
| 91 | [决策日志](91-decision-log.md) | 技术建议、待决业务语义和变更模板 |
| 92 | [进度与会话记录](92-progress-and-session-log.md) | 实际完成状态、证据和下一检查点 |
| 93 | [v3完整验收映射](93-v3-acceptance-coverage.md) | L1/L2/L3原目录逐项映射、阶段、缺口及延期理由 |
| 94 | [数据流转与保留验收](94-data-flow-and-retention.md) | 各系统实际区域、访问者、保留/删除及企业数据准入 |

## 2. 顺序中的三个小回环

**环境预检分层完成。** 03先形成只读来源/工具报告，再在受控临时副本做必要构建诊断；04不要求业务系统已经启动。05才建立正式实验副本与真实业务smoke。

**契约与Oracle相互校准。** 06先实现DRAFT校验、冻结和引用；07–08建立观察和断言，09前半做健康/缺陷对照；再回06完成真实Owner决策与激活，09后半输出正式受限WorkOrder。不能把“06尚未ACTIVE”误解为无法做已获隔离实验授权的校准，也不能为了进入下一章伪造批准。

09的发布切片需回06/08批准十一月政策、tool schema v2与历史事件兼容，并校准正确V4、latest-only和wrong-date变异。运行时先验证已有candidate；只有可信候选资产Finding和修复授权才进入Coding。

**平台资格先本地，远端资格通过受限演练建立。** 14的候选Final不要求尚未发生的PR/CI；15先验证已经实现的组件。17开发出Action Gateway/实际Adapter后，先回15补齐该版本的隔离、权限与恢复资格，再用明确授权的首次受限fork资格任务建立真实provider证据，供后续常规发布使用。该任务不要求自己尚未产生的远端资格；它也不是跳过已有安全门槛的通用例外。

## 3. 每章怎么用

每章都包含目标演示、前置、具体拟建文件、步骤与取舍、正反测试、证据、失败处理和下一次会话提示词。

1. 先读目标和前置，核对92记录及实际文件。
2. 选择一个检查点，把“本轮完成什么”说清楚。
3. 写代码并运行该切片需要的验证，保存真实结果。
4. 遇到环境/权限/语义缺口，按原因处理；不要让Agent盲目改业务。
5. 输出真实结果及下一检查点到会话与reports/artifacts；获准修改docs后才更新92、91和受影响协议。

可以直接说：

> 按第XX章继续第一个未完成检查点。先解释本轮架构与取舍，再实现、验证和记录，不把未运行项算通过。

所有未来命令必须先在对应章实现并注册。文中 `<run_id>` 等是要替换的参数，不能原样当shell命令复制执行。具体库API和版本在实施时核对并锁定；伪代码用于表达协议。

## 4. 共用目录和证据约定

所有相对计划路径均以 `EGAP-Implementation/` 为根。

- `artifacts/`：受控原始对象、执行证据、候选及私有输入，不整体提交Git或挂载给Coder。
- `artifacts/evidence/`：03–09早期实验的证据命名空间；后续通过ArtifactStore继续管理，不创建另一个无权限治理的根目录。
- `artifacts/qualification/`：平台、恢复、安全、发布资格；按实际版本/范围保存。
- `artifacts/runs/`：Run级受控证据索引；不可变大对象由ArtifactStore寻址。
- `reports/`：脱敏的人读摘要、评估汇总和受控原始对象引用；可按审查策略提交，不能包含隐藏答案或秘密。
- `sut/`、`config/`、`datasets/`：分别保存来源锁/实验副本、已核验配置、任务目录；按后续章节创建。

05–09采用单机文件型实验账本，明确标注 `authority_mode=file-lab`、`evidence_scope=calibration-only`。这允许先建立真实实验，不提前依赖第10章数据库；具体ID、锁和迁移语义见01。

## 5. 共用名词

| 名词 | 含义 |
| --- | --- |
| SUT | 自有B2B SaaS客服应用；参考航空Demo不代表已经拥有账单、历史政策、持久队列或企业隔离 |
| Oracle | 按批准规则从独立观察判定结果的可信程序 |
| Experiment Barrier | 精确持有任务派发或结果提交的独立测试能力，各能力分别校准 |
| Candidate | 冻结代码/依赖、Prompt、corpus/index/retrieval、tool/output schema、provider/model/config、workflow身份的候选 |
| CheckRun / ExecutionAttempt | 一项逻辑检查 / 一次实际执行，重试历史保留 |
| Mandatory | 当前批准范围必须完成的检查，模型不能删减 |
| Qualification | 某平台/工具/环境版本在指定范围内的资格证据 |
| UNKNOWN | 外部结果尚不确定，需要观察对账 |
| completion_mode | FINAL_ONLY止于验收；DRAFT_PR_CI包含批准发布和准确CI |
| task_mode / repair_policy | RELEASE_VERIFICATION先验证外部候选，INCIDENT_REPAIR处理已知缺陷；DISABLED或ON_VALIDATED_FINDING单独限定修复 |
| change_kind / NO_CHANGE | 相对任务冻结比较base判定；Release没调用Coder不等于UNCHANGED，新功能合格仍可成功 |
| attribution=NONE | 仅COMPLETED+PASS没有故障归因；UNKNOWN表示原因未知，null仅未评估未归因 |
| budget_pool | CODING、DISCOVERY、FINAL、RECONCILIATION、CLEANUP；探索不能借用受保护额度 |
| execution_mode | scripted-fake或live，防止模拟结果冒充模型能力 |

Graph实验模式A/B/C、18章EVAL_A/B/C与CONTEXT_ABLATION是三种不同分类；报告写全名称。新归因为CANDIDATE_DEFECT + candidate_asset_kind，旧CODE_DEFECT仅作历史版本映射。Job DONE、Ticket RESOLVED、EGAP PASS及两套业务/发布批准不得合并。

## 6. 从这里开始

先读[总体计划](00-overall-plan.md)、[架构](01-system-architecture.md)、[取舍](02-engineering-decisions.md)。续作核对[92进度](92-progress-and-session-log.md)和实际报告，进入[03 GenAI预检](03-environment-preflight.md)与[05客服基线](05-genai-support-baseline.md)。[93验收映射](93-v3-acceptance-coverage.md)保留历史目录与当前适用性；SUT模型、Verifier、Grader和企业接入前逐通道核对[94数据准入](94-data-flow-and-retention.md)。

## 7. 本次迁移后的入口

默认业务由 `config/active-business.json` 选择新的GenAI参考来源与独立 `artifacts/lab/genai-support/` 账本；准确锁与工作副本路径以该配置及来源回执为准。`prepare-source`只核验来源，不创建健康SaaS资格。历史ML和Eventuate代码、锁、账本及证据保持原义，不计SUP覆盖。

当前下一步是03/05的Python/SDK/镜像/模型通道预检和自有客服SUT：可信身份下提交ticket/revision、队列执行、真实LLM与政策/账单工具、持久草稿及独立记录。之后07/08完成SUP政策/参数/补证与最小权限、持久性校准，再进入09真实增量候选。
