# 91 · 决策记录与待决问题

建立日期：2026-11-02。本次于2026-11-04按用户授权切换ML主业务。下表逐项标状态；`ADOPTED`仅表示采用该技术方向，不构成Owner业务批准、执行权限或生产资格。已有代码/证据状态见92。

文档修改规则：2026-11-03本轮最后微调交付后，本文件及其他docs修改须先获用户明确允许。实现中可在会话/reports提出决策建议，不自动更新本日志；这不妨碍继续已授权且不依赖新业务决策的代码工作。

## 1. 如何维护

技术决定记录 `PROPOSED / ADOPTED / SUPERSEDED`，并保留原记录；业务规范的 `DRAFT / APPROVED / ACTIVE / REVOKED` 由Registry负责，两者不要混用。一次实验成功也不自动把技术方案升为生产资格。

批准字段必须是真实主体和实际时间；未知填“待定”，不能填占位签名。影响合同、Oracle、环境或运行器的决定要说明哪些旧证据失效。

## 2. 当前技术建议

| ID | 决定 | 理由与代价 | 状态 | 验证章节 |
| --- | --- | --- | --- | --- |
| ADR-001 | 新目录独立实现，旧项目只读参考 | 减少旧场景与权限耦合；逐段移植有额外工作 | PROPOSED | 03–04、90 |
| ADR-002 | TypeScript平台 + Java Eventuate SUT | 复用平台经验，同时保留真实业务 | SUPERSEDED（见ADR-024–028） | 03–05 |
| ADR-003 | PostgreSQL权威 + transactional outbox +通知队列 | 对崩溃与重复提供可审查事务边界 | PROPOSED | 10–11 |
| ADR-004 | 五类Profile以JSON Schema为公共结构协议 | 复用草案，防多份规范漂移 | PROPOSED | 06 |
| ADR-005 | 单Run专属测试环境，候选无Docker socket | 降低串扰，隔离强权限引导 | PROPOSED | 05、07、15 |
| ADR-006 | 独立Observer和真实回复屏障 | 同时验证提前批准安全性与释放后活性 | SUPERSEDED（见ADR-024–028） | 07–08 |
| ADR-007 | CodingProvider首期Codex非交互adapter | 易控制一次Attempt的输入、输出与退出 | PROPOSED | 12 |
| ADR-008 | LangGraph JS仅负责实验规划 | 不让checkpoint成为第二权威账本 | SUPERSEDED（见ADR-024–028） | 13 |
| ADR-009 | Final确定性，初期五项检查明确范围 | 控制首期工作量，保留完整强化路线 | SUPERSEDED（见ADR-024–028） | 08、14 |
| ADR-010 | 发布凭据在Action Gateway，默认自有Fork Draft PR | 精确审批、对账与CI范围清晰 | PROPOSED | 15–17 |
| ADR-011 | 先本地ArtifactStore，接口支持后续对象存储 | 快速落地；需单独做ACL、恢复与容量治理 | PROPOSED | 04、10、19 |
| ADR-012 | 首期不接企业SaaS/RAG作为硬依赖 | 先验证修复链，随后逐来源接入 | PROPOSED | 20、22 |
| ADR-013 | 批准消费与撤销分离，每个远端步骤发送前重新准入 | 覆盖消费后尚未发送窗口；准入后可能在途需对账，不能宣称撤回网络请求 | PROPOSED | 11、16–17 |
| ADR-014 | 正常PASS使用NONE，按执行→断言→归因路由 | 防健康检查落入UNKNOWN；null仅未评估未归因，状态组合须验证 | PROPOSED | 01、08、13–14 |
| ADR-015 | 无修改也冻结完整base Candidate并独立Final | 防Coder自报通过；UNCHANGED成功进入NO_CHANGE，修复指标单列 | PROPOSED | 09–10、12–14、18 |
| ADR-016 | 五预算池与受保护Final/恢复/清理额度 | 探索耗尽先判断送验而非立即停止；禁止挪池，终态不恢复业务 | PROPOSED | 10–11、13–14、18 |
| ADR-017 | ACL服务不可用或权限超龄任一成立就拒绝 | 与v3一致，避免新鲜缓存掩盖服务故障；降低故障期可用性 | PROPOSED | 20、94 |
| ADR-018 | 完整v3目录逐项映射并保留延期状态 | 防首期五项冒充所有企业要求；适用范围变化需重新评审 | PROPOSED | 00、19、22、93 |
| ADR-019 | 各数据流登记实际区域、期限、访问者及删除能力 | 外部读取不隐含外发权；缺项只阻断相关真实数据范围，合成协议可先开发 | PROPOSED | 12、15、19–20、94 |
| ADR-020 | 主业务定位为变更感知的版本发布验证，Incident Repair保留 | 企业收益聚焦新增功能/回归验证，可信Finding之后才按授权修复 | PROPOSED | 00–01、09–14、18 |
| ADR-021 | 冻结task_mode、repair_policy，分开Stable比较base与修复输入candidate | 外部新功能候选无需Coding即可验收；防回退Stable删功能及无修复误记NO_CHANGE | PROPOSED | 01、09–14、16–18 |
| ADR-022 | 首个主Demo为client_reference+Saga重构及seed/no-seed对照 | 增加真实可验证功能，控制首轮范围；完整重试/消息升级/额度调整仍后续 | SUPERSEDED（见ADR-024–028） | 06、08–09、13–14、18、21–22 |
| ADR-023 | 历史Stable覆盖作先验，当前候选Final执行批准适用集 | 减少无理由的Stable重复全测，未知影响扩大检查/阻断，不偷换历史PASS | PROPOSED | 05、10、13–14、18 |
| ADR-024 | 主业务切换Async ML Inference Release Verification，保留Eventuate历史资产 | 用户2026-11-04明确要求；通用账本/治理复用，业务适配与资格重新建立 | ADOPTED（方向） | 03–09、90–94 |
| ADR-025 | 固定公开FastAPI/Redis Streams/ONNX图片分类源码，先测实际API | 不把模板称企业生产基线；无逐Job模型身份/HTTP幂等时明确能力缺口 | ADOPTED（来源） | 03、05 |
| ADR-026 | ML-01…06逐项校准，ML-07质量/性能独立后续准入 | 版本正确不等于模型质量好；无批准数据/门槛不宣称质量通过 | ADOPTED（范围） | 06–09、14、21 |
| ADR-027 | TypeScript控制面＋独立Python LangGraph，JSON Schema跨语言 | 方便ML工具生态，增加协议/部署成本；图不拥有权威事实与PASS权限 | ADOPTED（设计，未实现） | 01–04、13 |
| ADR-028 | Stable先固定模型/预处理，V4增加模型B、schema v2、v1兼容与Worker重构 | 真正有变化的候选；late-alias-lookup仅自有synthetic seed，不归咎上游 | ADOPTED（实验路线） | 06–09、13–14、18、21 |
| ADR-029 | Candidate/Gate/批准绑定模型字节及完整推理输入闭包 | Git SHA相同也可能变行为；artifact或引用变化使旧证据/批准失效 | ADOPTED（设计，未实现） | 10、12、14、16–17 |
| ADR-030 | 首期本地不可变manifest，MLflow/Feast/生产推广后续接入 | 控制前置成本；已有队列机制不能替代持久性、版本与数据治理实测 | ADOPTED（范围） | 05–09、20–22 |

## 3. 不能由实现者暗中补完的问题

| 问题 | 决策人 / 证据 | 关闭条件 | 阻塞范围 |
| --- | --- | --- | --- |
| 输入图片大小/尺寸、预处理及模型输入域 | Business/ML Owner + 源码与运行观察 | 版本化schema/预处理/模型manifest与边界测试 | 超出已明确输入域的验收 |
| 重复Job/HTTP key与不同payload或model冲突 | Business/ML Owner | 队列重投与HTTP幂等分别定义复用/拒绝、保留策略和真实测试 | ML-04/HTTP幂等扩展 |
| 收敛窗口与健康前提 | QA/SRE + 基线分布 | 起算事件、阈值、误差、超时归因获批 | 活性Final |
| 实际Redis key/Stream载荷/结果与模型身份关联 | 固定依赖 + 运行观察 | 可追溯schema、claim/fencing及结果写入清单 | Observer/Barrier |
| 屏障可否独立于候选 | QA/Security + spike | 无干扰、不能绕过、失败可检测 | ML-01成功时序与ML-02版本切换检查 |
| Python构建、模型预取与容器引导分离 | 平台工程 + 上游插件核查 | 候选无daemon权限仍可装载运行 | 任意候选执行 |
| 工具链、依赖和镜像组合 | 环境预检/构建实测 | 可解析可重建的锁定清单 | G0及之后 |
| 摘要规范化与签名信任根 | 平台/QA/Security | 黄金向量、内容/审批分离、轮换测试 | 不可变Bundle及证据接受 |
| Git目标与真实CI身份 | Repo Owner | 明确repo/base/action及可信checks | 真实发布 |
| 模型配置和计费范围 | 项目负责人 | provider/model/版本/预算明确，真实使用日志 | live模型演练 |
| 企业身份、地区与数据范围 | 企业Owner/Security | 账号scope、数据路径、保留与撤权 | 企业连接/部署 |
| 各系统处理/存储/备份/支持访问区域 | Data Owner/Security + 实际部署及provider配置 | 第94章适用流向地区明确、配置可核验、无未知接收者 | 对应模型通道/企业数据流 |
| 各副本保留期限、删除延迟及例外hold | Data Owner/SRE + 存储/provider实际能力 | 明确数值与起算点，删除及备份恢复测试，不用虚构统一天数 | 企业数据接入/导出/备份 |
| Final mandatory/delivery及对账清理保护额度 | 项目负责人/QA/SRE + 实测资源上界 | 总额、池限额、预留策略与截止时间可执行，竞争/耗尽测试通过 | 真实运行准入与批量评测 |
| 模型B、preprocessing/schema v2及v1兼容范围 | ML/Business Owner/QA + 正确与变异校准 | 模型摘要、输入/输出/队列矩阵、新需求明确；冻结提交身份与实际执行可核对 | 最小ML Release Demo正式验收 |
| 结果持久性、TTL与失败模型 | Business Owner/SRE + Redis/存储实测 | 受理与成功承诺、宿主故障丢失窗口、保留和到期行为获批 | ML-01/05及生产声明 |
| 质量指标/切片、标注集与性能阈值 | ML/Data Owner/QA | 无泄漏的冻结数据、统计计划、硬件环境和批准阈值 | ML-07，不阻塞限定协议校准 |
| Stable实际资格、历史覆盖与发布层级 | Repo Owner/QA + 版本/制品/环境证据 | 实验室基线与真实生产史分开，当前适用mandatory/扩大回归规则可执行 | RELEASE_VERIFICATION准入与价值结论 |
| 试点验收与推广阈值 | 项目负责人/QA | 预注册指标和公平对照计划 | G5推广结论 |

## 4. 单条决策模板

```text
ID / 标题：
状态：PROPOSED
日期 / 发起人：
问题与触发证据：
可选方案：
选定方案及原因：
代价、风险与缓解：
影响的代码 / Schema / Profile / 环境：
需要失效或重跑的检查：
验收与回退方式：
真实采纳人 / 记录引用：待定
替代的旧决定：无或具体ID
```

## 5. 首次设计建立记录

2026-11-02：根据当前Eventuate v3方案、合同草案和AAA参考工作树建立这组实施手册。新增分阶段实现路径、具体代码位置、校验与恢复任务。文档写入已完成；尚未运行新系统或把任何业务合同设为ACTIVE。源码来源与复用边界见 [90](90-reference-and-reuse-map.md)。

同日审查修订：接受用户指出的五项跨章规格缺口，按ADR-013–017补齐协议，并新增93/94两份验收附件。以上仍是待实施/待验证的技术方案，不构成业务Owner、数据Owner或发布授权；保留原架构与阶段顺序。

2026-11-03最后定位微调：用户要求把测试价值聚焦稳定版本之后新增功能和较大改动引入的风险，参考其附带讨论，选择Release Verification为主入口、针对性Discovery与批准Final并存；单缺陷保留作校准。新增ADR-020–023及文档修改许可规则；没有实施功能、建立真实线上历史或运行评测。后续docs改动需用户新授权。

2026-11-04：用户明确授权把主业务、对应docs和当前实现迁移到Async ML Inference Release Verification。ADR-002/006/008/009/022保留历史并被替代；新增ADR-024–030。旧订单构建/source证据保留但不转为ML PASS，未批准的业务语义和质量门槛仍为DRAFT；后续docs仍须新的明确授权。
