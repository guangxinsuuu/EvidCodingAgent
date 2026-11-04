# 92 · 实现进度与会话记录

建立日期：2026-11-02。**已有控制面骨架与实验账本；主业务于2026-11-04切换ML，真实ML基线与独立Oracle尚未通过。** 此表记录新目录的事实，不继承旧仓库的测试状态。文档校验与业务验收分开登记。

2026-11-03用户要求本轮最后微调后，docs修改须先获明确允许。本日志后续也受此规则约束；未获许可的实现会话把真实结果存reports/artifacts并在会话交付，不能自动改本表。续作需同时核对实际代码与最新报告，避免因冻结日志滞后而误判进度。

## 1. 状态约定

- `NOT_STARTED`：未开始该章实现。
- `IN_PROGRESS`：已有实现或实验，退出条件未全部满足。
- `BLOCKED`：明确前置未满足；记录责任方和解除条件。
- `VERIFIED`：指定版本/环境下全部本章适用退出条件有真实证据。
- `NEEDS_REVALIDATION`：代码、依赖、Profile或环境变化后资格失效。
- `DEFERRED`：当前范围明确延期，保留理由；不能计作通过。

手册00–02、22及90–94属于设计/导航/验收附件，不填写业务PASS。Gate只聚合实际证据。

## 2. 实施清单

| 章 | 实施单元 | 状态 | 证据 / 阻塞 |
| --- | --- | --- | --- |
| 03 | ML环境与来源预检 | IN_PROGRESS | 旧Eventuate构建预检完成；新ML源码已固定并二次重验，工具链/模型/依赖运行预检另记，旧结果不迁移 |
| 04 | 项目骨架 | IN_PROGRESS | 已有pnpm/TypeScript、contracts/domain/artifacts、Fastify健康入口；ML adapter与默认来源切换已完成本轮代码测试，非完整业务平台 |
| 05 | ML真实基线与实验账本 | IN_PROGRESS | 05.0文件账本/锁/恢复已有；05.1旧190文件来源历史保留，ML27文件来源已冻结并重验；Docker适配器已有，真实ML启动/四smoke/清理尚待证据 |
| 06 | Contract Registry | NOT_STARTED | 输入草案仍DRAFT |
| 07 | ML Observer / Experiment Barrier | NOT_STARTED | 待实际Redis/Job/model/result观察与可信屏障spike |
| 08 | ML-01…06业务Oracle | NOT_STARTED | 待完整断言与健康/缺陷/坏观察器校准；ML-07单独后续准入 |
| 09 | 单缺陷校准、增量发布任务与评测隔离 | NOT_STARTED | 新功能/回归候选及对应检查尚未实现 |
| 10 | 持久化Runtime | NOT_STARTED | 待真实PG事务与迁移 |
| 11 | 恢复、预算与并发 | NOT_STARTED | 待故障矩阵实测 |
| 12 | Codex Coding Worker | NOT_STARTED | 尚未进行模型调用 |
| 13 | LangGraph Verifier | NOT_STARTED | 尚未实现Graph |
| 14 | Final Gate与证据闭包 | NOT_STARTED | 尚无可信最终候选 |
| 15 | 安全与平台资格 | NOT_STARTED | 待执行域和否定测试 |
| 16 | Console与精确审批 | NOT_STARTED | 尚无真实批准记录 |
| 17 | Draft PR与CI | NOT_STARTED | 尚无远端目标/发布记录 |
| 18 | 公平评估与试点 | NOT_STARTED | 尚无真实实验数据 |
| 19 | 部署、身份与运营 | NOT_STARTED | 后续范围逐项准入 |
| 20 | 企业Connector | NOT_STARTED | 未连接真实账号 |
| 21 | 业务与平台扩展 | NOT_STARTED | 按需求单独选择 |

## 3. 当前Gate状态

G0为 `IN_PROGRESS`：通用基础已实现，ML真实运行条件待验证。G1、G2、G3、G4、G5仍为 `NOT_STARTED`。第19–21章的扩展资格也未建立。业务与平台版本资格不得从文档勾选生成。

## 4. 每次会话记录模板

```text
会话ID / 日期：
目标章节与检查点：
开始时源码commit / 工作树状态：
输入来源、Profile / 环境revision：
本轮实际完成：
新增/修改文件：
实际执行命令及退出码：
测试范围 / 首次失败 / 重试attempt：
证据目录与摘要：
是否使用真实模型 / 远端 / 费用：
尚未完成 / 阻塞 / 未知：
设计变化与ADR：
清理与残留资源：
本章退出条件是否满足：
下一次从哪个检查点继续：
```

会话总结可以短，但证据引用必须可打开。原始日志含秘密或业务数据时，只把脱敏摘要写这里，原始对象按访问策略保存。

## 5. 文档建立会话

- 日期：2026-11-02。
- 请求：建立独立后续实现目录，将最新PDF、合同材料和AAA参考仓库整合成逐章实现手册。
- 已完成范围：文档编写、来源对照、步骤/接口/验收/取舍设计。
- 实施范围：未安装项目依赖，未创建业务服务，未调用模型，未启动Eventuate，未执行远端发布或部署。
- 下一步：[03 环境预检](03-environment-preflight.md)；先读00–02掌握目标和边界。

## 6. 首次文档检查记录

检查日期：2026-11-02。检查范围仅为文档与参考身份，不据此更新上面的任何实现状态。

- 文件完整性：28个Markdown文件，包括两个入口和00–22、90–92共26篇编号文档。
- 导航与格式：215处本地文件链接有效；全部编号文档已进入总目录；代码围栏闭合。
- 实施入口：03–21共19项仍为NOT_STARTED，每篇包含完成清单与可复制的续作请求。
- 参考身份：第90章记录的8份AAA源码/配置SHA-256与本次读取的文件一致。
- 交叉审阅：已核对早期文件账本与后期数据库的衔接、DRAFT校准再激活、Final前精确候选冻结、统计重复与技术重试的区别、两种完成模式，以及首次远端资格演练的前置顺序。
- 未执行范围：应用编译、业务测试、真实模型调用、远端发布和部署均未开始；不把文档检查计作这些项目通过。

## 7. 跨章节审查修订记录

日期：2026-11-02。依据用户的五项规格审查和两份验收附件建议，保留架构与实施顺序，只修订实施手册。

| 审查项 | 本次落地的规格 | 主要章节 |
| --- | --- | --- |
| 批准消费后撤销窗口 | 消费与撤销独立；每步发送准入复核、事务竞争边界、在途对账；新增负例 | 01、11、15–17 |
| PASS与UNKNOWN冲突 | COMPLETED+PASS+NONE；按执行/断言/归因路由，null有限用途及非法组合拒绝 | 01、08、13–14、18 |
| NO_CHANGE闭环 | 无快照仅提案，可信base Candidate与完整输入摘要、独立Discovery/Final、先于mode分流；修复与正确不改分别计分 | 09–14、16–18 |
| 预算分池 | 五池、FINAL用途子额、总额/池/用途同事务；探索耗尽先送验或停止，终态保留有界对账清理 | 01、06、10–11、13–14、18 |
| Connector ACL拒绝条件 | 超新鲜度预算或ACL不可用任一成立拒绝；新鲜ALLOW缓存不能掩盖服务故障 | 20、94 |
| 完整验收追溯 | 56个原L1/L2/L3条目及外部接入要求，逐项映射阶段、范围、证据与延期 | [93](93-v3-acceptance-coverage.md) |
| 数据流向与保留 | 20类设计流向、实际地区/期限/访问者必填、12项数据治理验收；真实未知项阻断接入 | [94](94-data-flow-and-retention.md) |

全部03–21实现状态仍为NOT_STARTED。未构建、未启动系统、未调用模型、未修改旧参考项目、未执行外部操作。文档修订不表示上述协议已被代码验证，也不构成企业生产资格。

本次文档核验通过：30个Markdown文件、28篇编号文档、269处本地链接；56个原L1/L2/L3检查的ID、名称和顺序逐项一致，无缺漏或重复；20条数据流向、12项数据治理验收编号完整；8份AAA参考文件摘要仍匹配。03–21共19项均保持NOT_STARTED且保留续作入口，代码围栏闭合。

交叉审阅另外收紧了受保护下载的逐次鉴权、墓碑期限覆盖可恢复备份窗口，以及DataFlowRecord生命周期与准入决定的命名空间。以上只证明文档结构、来源追溯和本次设计对照；不计作系统或业务测试通过。

## 8. 增量版本发布验证定位微调

日期：2026-11-03。用户明确要求围绕稳定版本之后的新功能/大改动进行针对性测试，并授权本轮最后调整MD；后续docs变更须先获得允许。

- 主入口改为RELEASE_VERIFICATION，输入Stable、已提交候选、真实ChangeSet、批准需求变化及历史覆盖；INCIDENT_REPAIR保留。
- 单缺陷F-SEED-01用于开发校准。主Demo采用自有V3.4→V4.0标签，包含可选client_reference功能、Saga组织重构和低风险对照；标签不代表真实上游版本或线上稳定史。
- 新候选先独立验证，可信Finding且获准才Coding。无修复的新功能候选仍为CHANGED；NO_CHANGE相对冻结任务比较base判断，不能通过回退Stable删除新需求。
- Stable历史覆盖用于风险先验/资格核验；必要时做有理由的对照复核，不按每个候选全量重跑。新候选仍执行批准的mandatory、变更与兼容检查；REL-C01/C02需回06/08补真实校准和批准。
- 主评估三组固定同一候选、同能力/总预算/Final，repair禁用且CODING=0；测试规划/发现与后续修复分账，核对漏报、发现时间、测试成本及人工分钟。
- 文档许可规则写入工作区和本项目AGENTS.md；手册自动更新91/92的旧步骤均受新许可条件约束。
- 本轮仅修订文档和项目指令，没有创建应用代码、安装依赖、构建/启动业务、调用模型或外部发布。全部实现仍NOT_STARTED。

本轮文档核验通过：30份手册（28篇编号文档）、另有2份协作指令；275处本地链接有效，围栏闭合；56个原L1/L2/L3条目的ID、名称和顺序保持一致，20条数据流向及12项数据治理验收完整，8份AAA参考文件摘要仍匹配。03–21共19项均为NOT_STARTED并保留检查清单和续作入口。模式、任务基线/修复输入、预算、无修改终态与发布父链已交叉审阅，未发现剩余阻断性矛盾。结构校验初次遗漏4种既有续作标题，核对后修正校验器，未为通过检查修改正文。校验结果保存于`tmp/egap-release-doc-validation.json`；这些结果不代表业务测试通过。

## 9. 后续继续工作的固定入口

> 先只读EGAP-Implementation/docs/92-progress-and-session-log.md、当前章节和最新reports/artifacts，核对实际代码及证据，继续第一个未完成检查点。实现后运行对应验证并交付真实结果。未获我明确允许不得修改docs；需要更新进度/ADR时先给具体拟改内容，运行记录先存reports/artifacts。

## 10. 2026-11-04 实现补记与ML业务迁移

本轮用户明确授权修改对应docs与现有实现；以后文档仍按许可规则维护。上面05章节名、业务目录和当前状态按这次迁移更新，旧会话叙述保留其当时事实。

- 已有公共实现：TypeScript/Fastify控制面骨架、基础契约/ID/ArtifactStore、file-lab追加日志与快照、单写锁/死进程恢复、环境epoch与动作/resource登记、固定本机DockerHost及隔离/归属检查。这些能力继续复用，不重写为ML专用账本。
- 已完成历史工作：[旧构建预检报告](../reports/preflight/20261104T020503Z-build-preflight/report.md)与[05.0＋05.1报告](../reports/05-baseline/20261104T044024Z-05ab/report.md)。前者证明固定Eventuate源码在记录的工具链上编译/导出JAR；后者证明190文件基线与账本流程。两者均不证明ML业务成功，也没有订单四场景真实smoke PASS。
- 迁移目标：默认来源/配置/CLI切换ML，新增ML API适配层；旧Eventuate源码、JAR、运行计划及证据保留历史并退出默认主线。新来源是固定commit `c200bcb4721c4428a9bfe808ca8a61648a2b16f6` 的27文件图片分类模板。
- 真实接口为 `POST /predictions` / `GET /predictions/{job_id}`，wire状态 `queued/processing/done/failed`；没有 `/scoring-jobs`、逐Job模型版本或HTTP幂等键。health只测Redis，不能作为端到端就绪证明。
- 本轮未完成的资格：ML镜像/依赖/模型制品完整锁、真实启动/端口发现/四条API烟测、独立模型身份/持久结果观察、ML-01…06 Oracle、Python LangGraph、Codex Runner、Final/审批/发布和ML-07质量评估。对应真实测试结果应以本轮reports/artifacts为准，单元测试不提升成G0/G1。
- 下一检查点：依03/05完成新ML来源和模型/依赖预检，再让账本登记的真实环境运行“图片完成、非法图片失败、不支持类型、未知ID”四smoke；随后07确认可信观察与模型/结果屏障。不要直接启动旧订单runtime，也不要跳到模型生成补丁。

本轮可核对结果（[迁移报告](../reports/migration/20261104T074012Z-ml-migration/report.md)，证据 `artifacts/evidence/migration/20261104T074012Z-ml-migration/`）：

- `pnpm typecheck`、`pnpm build`成功；`pnpm test` **77/77**，包含ML mock HTTP 14项、source7项、跨业务账本来源拒绝与公共基础；控制面集成测试 **3/3**。这些是代码/协议测试，不是真实Redis/ONNX端到端验收。
- ML来源27文件按固定Git blob、SHA-256和mode校验后原子落位，二次执行返回 `VERIFIED_EXISTING`。source lock SHA-256为 `8ac054f6a0ed27d842c04a5f55a551dc65d99f8a5cc8ccd7a457ed15b388c91f`。
- 默认 `egap:lab` 已切换ML；独立账本 `artifacts/lab/ml-inference`，experiment `experiment_66f037a9-bc35-4f96-acf6-ce1a6e60a159`，2个attempt、journal sequence 3、**0环境/0动作**；不复用旧实验授权/身份。
- 旧业务实现移入 `legacy/eventuate/packages/eventuate-adapter` 与 `legacy/eventuate/environment-host`，退出默认构建。旧 `sut/upstream.lock.json`、`sut/workspaces/baseline`、Eventuate配置与 `artifacts/lab/local` 原位保留历史。
- 本轮未下载模型、未启动真实Redis/ONNX服务、未运行真实ML四条smoke，也没有把控制面health的ready改称业务验证通过。下一步仍是03/05的ML运行条件与真实基线。

迁移收尾还核对了339个旧来源、证据及AGENTS文件的SHA-256，全部保持原样；旧实验/结果没有被本次ML迁移改写。
