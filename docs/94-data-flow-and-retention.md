# 94 · 数据流转、区域、保留与访问验收登记

状态：`DESIGN_READY / IMPLEMENTATION_NOT_STARTED`。本附件是待填写、待实测的准入规格，**不是企业数据已获准接入、供应商已满足区域要求或保留策略已生效的证明**。当前没有登记真实企业账号、部署区域或获批保留天数；流向清单均为DRAFT设计，企业准入决定应为 `BLOCKED`，原因 `PENDING_REGISTRATION`，不是第五种记录生命周期状态。

依据：[v3 接入01–04、06](../../EGAP-Order-Approval/docs/external-integration-v3.md)关于数据分类、用途、ACL、保留、脱敏和血缘的要求，以及[15 安全资格](15-security-and-qualification.md)、[19 部署运营](19-deployment-and-operations.md)、[20 企业Connector](20-enterprise-connectors.md)。本附件集中补充逐系统登记与验收，不改变来源内容不能自行授予权限的原则。

## 1. 怎样使用这份附件

先用公开固定源码和合成数据实现登记、过滤、权限与删除测试。准备让实际企业源码、文档、日志、工单、账单、政策或回复草稿进入一个系统时，为每条流向填实际配置和证据，并由有权的Data Owner/Security确认该范围。系统需在启用Connector、构建模型输入、写日志/trace、导出证据及备份前检查对应有效记录。

两个独立门槛：

- `MODEL_DATA_EGRESS_READY`：第05/12/13/18章任何真实SUT、Coding、Verifier或Grader模型通道启用前，核对该通道实际接收的字段、账号/endpoint、数据用途、区域和服务端保留能力。公开源码/合成任务可用限定该数据等级的记录，不提前依赖完整企业部署；凭据和隐藏答案仍禁止外发。
- `ENTERPRISE_DATA_READY`：第19/20章真实企业内容进入任何新系统前，适用流向全部登记、策略通过正反测试、Owner确认，且没有未解决的地区/保留/访问缺口。单个来源能读取成功不能替代这项门槛。

`DataFlowRecord.status=DRAFT|ACTIVE|SUSPENDED|REVOKED` 表示记录生命周期；`DataAdmissionDecision.decision=ALLOW|BLOCKED` 与reason_codes表示某次操作的准入判断；两个READY名称表示资格门槛，不新增Run状态。ACTIVE记录也必须经过当前ACL、用途和配置复核，不能等同每次访问自动ALLOW。

批准仅对已登记的 `tenant/project + data_class + purpose + destination_revision + flow_set_digest` 生效，不是平台通用许可。新增模型、日志平台、region、备份桶或子处理服务改变流向集合，需要复核；不可自动继承上次批准。

## 2. 每条流向必须登记的字段

以下是拟建 `DataFlowRecord` 的字段要求，不是当前已有Schema。缺失必填字段时不得把状态改为 `ACTIVE`。

| 字段组 | 必填内容与验收条件 |
| --- | --- |
| 身份与范围 | flow_id、revision、tenant/project、Data Owner、运行/来源scope、用途、数据等级、允许/禁止字段；主体取自实际身份系统 |
| 上游与血缘 | source_system/object_kind、source_revision、原ACL与用途、上游flow_id；源码片段、摘要、embedding、trace等派生物都保留关联 |
| 下游身份 | 接收服务/产品、账号/项目、endpoint、服务身份、配置revision；不能只写“云端”或“模型” |
| 数据路径 | 发送字段、转换/脱敏规则版本、传输通道、代理/网关、缓存、重试队列、失败回收位置和最大payload；每个新增持久副本单独登记或明确子记录 |
| 实际区域 | `processing_regions`、`storage_regions`、`backup_regions`、`support_access_regions`、已识别的中转服务及其区域；各字段填写具体批准地域或有证据的“不适用”，未知不填成用户所在时区 |
| 区域证据 | 账号实际设置、部署配置、供应商适用条款/能力说明及版本、核查日期、网络/存储盘点；endpoint名字、IP定位或单次请求不能单独证明全部数据驻留 |
| 保留策略 | 每个副本的策略ID/revision、起算事件、明确数值/单位、最大删除延迟、过期处理、备份到期、例外hold规则；不能只有“短期”“按需”或无期限的null |
| 访问者 | 人员/服务主体、角色与目的、read/write/export/admin分别允许什么；支持人员、CI/供应商服务端权限也列明，不能只登记本地用户 |
| 权限检查 | 当前ACL来源、最大陈旧时间、权限服务故障拒绝方式、撤权传播及派生副本失效；读取权限与跨系统发送许可分别校验 |
| 删除与恢复 | 负责删除的组件/人、对象定位、墓碑、删除回执、备份恢复时重放删除/撤权记录；无法逐对象删除的限制及批准处置 |
| 模型特有项 | 实际产品/账号的数据使用和训练设置、provider可见输入/输出、服务端日志/滥用监测保留及例外、删除能力；不可从其他产品的设置推断 |
| 资格与状态 | 测试报告、真实批准记录、有效期、变更触发器、已知缺口；`DRAFT / ACTIVE / SUSPENDED / REVOKED`，均与实际数据流配置摘要绑定 |

不适用必须说明理由，例如“本流向无持久化”需要实际配置与磁盘/遥测观察支持；未知和不适用是不同状态。摘要和对象ID也可能泄露来源关系，仍按分类处理。

## 3. 首版需要登记的完整流向清单

下表是**设计清单**。区域列的“待登记”包含处理、存储、备份与支持访问；不能据此开放企业数据。保留列引用第4节待批准策略，所有具体期限当前均未确定。

| ID | 数据与流向 / 下游系统 | 必须去除或限定 | 区域 / 期限 | 允许访问者与边界 | 实施章 |
| --- | --- | --- | --- | --- | --- |
| DF-01 | 固定Git/企业仓库 → Source cache、受控工作区 | 限定repo/ref/path；排除凭据、无关历史和未授权子模块/LFS对象 | 待登记；R-WORK、R-CACHE | Source reader、指定Run的Coder；Coder不得读其他Run | 03、05、12、20 |
| DF-02 | 工单/文档 → Connector对象存储与检索索引 | 限项目/对象/用途；原ACL与删除血缘随内容走 | 待登记；R-SOURCE、R-CACHE | Connector写；Knowledge reader按当前主体与ACL读 | 20 |
| DF-03 | 源码/WorkOrder/规则/Finding → Prompt builder → 模型服务 | 字段白名单、最小片段；无密钥、隐藏答案、未经许可原始业务数据 | 待登记；R-MODEL（输入及服务端副本） | Prompt服务、限定provider服务；人员支持权限单列 | 12、13、15 |
| DF-04 | 模型输出/工具调用事件 → Worker、响应归档 | 输出可能复述输入；按输入最高敏感度，工具参数脱敏 | 待登记；R-MODEL、R-WORK | 本Attempt的Worker、授权审阅者；输出不能赋权 | 12、13、16 |
| DF-05 | Prompt与Graph state → checkpoint/store | 默认只存引用及必要状态；若框架保存全文，增加实际正文流记录 | 待登记；R-WORK | Verifier服务、受控恢复角色；Coder无权读隐藏状态 | 10、13 |
| DF-06 | 候选源/构建上下文 → 隔离构建器、依赖代理/cache | 候选无平台凭据；代理日志/缓存不能额外收集源码，意外发送算违规 | 待登记；R-WORK、R-CACHE | 构建域只读冻结输入；预取器写限定依赖cache | 04、05、12、15 |
| DF-07 | 合成工单/账单/政策fixture → SUT API、PostgreSQL Job/草稿、Redis、Worker | 首期合成；真实tenant/customer/invoice等另登记字段、范围与用途 | 待登记；R-SUT | SUT按主体读写，Observer只读；屏障控制另授权 | 05、07、19、21 |
| DF-08 | SUT Job/工具/模型调用/草稿/日志trace → Observer → 原始证据 | 限定ticket/revision/job/check/attempt；完整原件受控，脱敏副本不冒充原件 | 待登记；R-EVIDENCE | 候选外网关/Observer采集、Collector登记；QA按scope读 | 07、08、14 |
| DF-09 | Candidate/Git对象/制品 → ArtifactStore | 精确文件/对象闭包，拒绝工作区外文件；保留保密分类 | 待登记；R-EVIDENCE | 可信Collector写；Runner/Gateway按精确candidate读 | 12、14、17 |
| DF-10 | 事件、批准、预算、身份与证据引用 → EGAP PostgreSQL | 载荷最小化，不在journal存原文/完整token；审计ID仍受控 | 待登记；R-AUDIT | Control Plane按角色写；审阅/运营有限读 | 10、11、16 |
| DF-11 | 工作通知 → outbox、Redis/BullMQ、失败队列 | 只传对象引用；不夹带prompt、源码、密钥或授权快照作为权威 | 待登记；R-QUEUE | Relay/指定Worker；队列运维无业务内容默认访问权 | 10、11 |
| DF-12 | 平台与SUT日志/错误栈 → 本地或外部日志系统 | 禁token、默认完整工单/账单/Prompt/客户回复正文；路径、工具参数及引用也检查 | 待登记；R-LOG | 按tenant/scope的SRE/审计；供应商支持另登记 | 15、19 |
| DF-13 | trace/span/metrics → 采集器/APM | 属性白名单；不自动记录prompt、SQL参数和HTTP正文；采样不是脱敏 | 待登记；R-TRACE | 观测服务写、授权SRE读；不默认向全员开放 | 19、20 |
| DF-14 | 原始证据/源码 → Console、报告、下载/导出 | 默认脱敏摘要；受保护下载经当前ACL鉴权代理，不能用直达对象的签名URL绕过；导出是新副本 | 待登记；R-EXPORT | 当前授权审阅者；导出目的地及二次分享需在scope内 | 14、16、18 |
| DF-15 | 冻结Git对象/PR描述 → 自有Fork/托管Git服务 | 仅批准candidate和目标；PR正文不附敏感日志/隐藏答案 | 待登记；R-REMOTE | Gateway指定动作；Repo当前权限用户、服务端管理员另登记 | 17 |
| DF-16 | 仓库/制品/测试输出 → CI runner、cache、日志、artifact | CI身份与工作流可信；不得将内部证据或密钥自动上传公开artifact | 待登记；R-CI | 指定CI服务与repo角色；权限不能因Draft PR而默认安全 | 17、19 |
| DF-17 | DB/证据/Registry配置 → 备份、快照、复制区域、恢复环境 | 凭据独立恢复；恢复环境不能默认开放旧数据或旧批准 | 待登记；R-BACKUP及源策略 | Backup服务写、受限恢复角色读；恢复后重放撤权/删除 | 19 |
| DF-18 | Hidden ground truth/Seed答案 → 私有评估存储 | 与候选/普通报告/模型通道隔离；不进入通常构建上下文 | 待登记；R-EVAL | 数据集维护者、授权裁决器；Coder/Planner/provider禁止读取 | 09、18 |
| DF-19 | 实验统计/失败案例 → 评测报告/支持诊断包 | 默认只导出聚合与脱敏案例；支持工单附件也是独立外发 | 待登记；R-EXPORT、R-AUDIT | 项目负责人/QA；供应商支持须指定实际接收者与用途 | 18、19 |
| DF-20 | 密钥管理系统 → 各可信执行器的短期内存 | 只通过引用解析；禁止进入prompt、代码、证据、日志、备份正文 | 待登记；R-SECRET | 指定service identity；轮换/吊销由独立密钥管理员 | 12、15、17、19、20 |
| DF-21 | 历史ML：模型来源/注册库 → 预取器、模型cache、Worker加载目录 | 限来源、许可、不可变模型字节与摘要；不随意下载外部custom operator/可执行反序列化对象 | 待登记；R-ML-ARTIFACT | 可信预取器写；Worker只读批准模型；Coder无权改当前验收模型 | 03、05、12、15、21 |
| DF-22 | 历史ML：图片/特征snapshot/预处理 → 推理内存、临时文件、队列/结果 | 首期合成或明确获准图像；身份信息、EXIF、原始特征和embedding分别分类；摘要不自动匿名 | 待登记；R-SUT、R-ML-ARTIFACT | 当前Job的Worker、受限Observer；禁止默认为Agent prompt输入 | 05–08、15、21 |
| DF-23 | 历史ML：标注集/切片/holdout → 独立ML评估器与指标存储 | 数据许可/用途、标签隔离、样本去重/泄漏与派生统计；不流向Coder/Planner或普通报告 | 待登记；R-EVAL | Data/ML评估Owner、可信Evaluator；模型只能读获准汇总 | 09、18、21 |
| DF-24 | 历史ML：模型/feature注册元数据与alias → Connector、发布预览与审计 | 只读范围与promotion写权分离；记录版本/摘要/当前ACL，不把alias字符串当不可变身份 | 待登记；R-SOURCE、R-AUDIT、R-REMOTE | 只读Connector、独立批准推广Gateway；当前首期不接MLflow/Feast | 16–17、20–21 |
| DF-25 | 工单、适用政策片段、工具结果 → SUT模型代理 → 实际provider | 限合成/批准字段，隐藏真值与密钥禁止；逐次请求/响应、配置和用途绑定 | 待登记；R-MODEL、R-EVIDENCE | 可信模型代理、限定provider；候选无原始key | 03、05、07、15 |
| DF-26 | 获准回复/证据摘要 → Semantic Grader/人工复核 | rubric、输入最小化；独立holdout标签不默认给grader；输出可能复述敏感字段 | 待登记；R-MODEL、R-EVAL | 独立Evaluator与复核人；Coder/Planner仅获准报告 | 08、18、21 |
| DF-27 | 政策正文/ACL/生效区间 → corpus snapshot、索引、可选embedding/reranker | 内容与权限版本分开，历史政策保留用途明确；删除/撤权覆盖派生索引 | 待登记；R-GENAI-ASSET、R-SOURCE | 受控索引器写，SUT检索按当前Tenant/用途读 | 05–08、20–21 |
| DF-28 | 认证主体/工具参数/返回/授权拒绝/实际效果 → 可信工具网关证据 | 候选外记录，原始受保护；schema引用/摘要不等于匿名；不能只存UI截断 | 待登记；R-EVIDENCE、R-AUDIT | 网关/Collector写；Oracle只读，候选无覆盖权 | 07–08、14–15 |
| DF-29 | 业务建议/客户回复/退款批准 → SUT业务Action Gateway/人工桌面 | 首期仅草稿/沙盒；真实发送/支付另批准对象/目的地；与EGAP发布审批分域 | 待登记；R-SUT、R-AUDIT、R-REMOTE | 业务授权人员/服务；EGAP发布token无业务动作权 | 16、19、21 |

DF-21–24保留2026-11-04 ML历史设计，不是当前活动业务流向或数据许可。以后重新启用另行登记和激活，不能因为历史表存在就读取图像/特征/模型。当前GenAI新增流向见DF-25–29；派生摘要、embedding和模型输出仍按来源敏感度治理。

DF-03/04拆开登记Coding与Verifier，DF-25登记SUT模型，DF-26登记Grader；即使同品牌也不继承账号、区域、保留或费用设置。日志/APM、备份、embedding与人工支持各实际接收系统展开子记录，不藏进“其他”。

受保护内容的下载、Range续传及导出取回应走可信代理，每次请求重新核验主体、当前ACL、用途和flow状态。短TTL直达签名URL本身不能实现即时撤权，不能作为用户可复用的绕过通道；如底层存储使用签名能力，只在代理内侧短期取回，或采用可验证撤销且仍强制上述检查的等价机制。在途流的停止边界需测试并记录，已合法取回的离线字节无法由服务端撤销；没有获准离线副本治理策略时拒绝该导出用途。

## 4. 保留期限登记：每种副本必须能算出到期时间

所有下列策略当前 `retention_value=null`、`approval_ref=null`，表示未决定而非永久保留或零保留。企业接入前，实际策略必须填写 `trigger_event + retention_value + unit + deletion_grace + backup_expiry + owner + policy_revision`，并能据此计算 `expires_at`。不在本手册编造通用的30天/90天要求。

| 策略 | 对象与起算事件 | 到期及例外规则 |
| --- | --- | --- |
| R-WORK | 工作区、临时构建、恢复上下文；Attempt/Run结束或取消 | 及时撤销访问，按批准期限清理；需要复现时转受限证据并登记新用途，不能无限留临时目录 |
| R-SOURCE / R-CACHE | 来源正文、索引、摘要、依赖cache；取回/更新，ACL时效另算 | 内容TTL不等于权限TTL；撤权先禁读，删除按源规则和保留策略处理 |
| R-MODEL | 输入、输出、本地调用归档及provider副本；实际发送/完成 | 分别记录本地和服务端期限、训练/日志例外；provider不可逐条删时明确最长保留与批准范围 |
| R-ML-ARTIFACT | 历史ML范围的模型、预处理/feature snapshot及模型cache；撤销、版本退役或实验结束 | 不可变身份与存续期限分别管理；运行中的授权Job按批准策略续用或停止，删除缓存不伪称撤回已加载内存；备份和证据副本另登记 |
| R-GENAI-ASSET | Prompt、corpus/index/retrieval/tool schema快照；版本退役或来源撤权 | 不可变内容与使用权限独立；索引/embedding/缓存/导出分别删除，provider副本按R-MODEL处理 |
| R-SUT | 测试输入/结果、Redis/消息与快照；实验完成/环境租约结束 | 清理对应所有权资源；需保留反例时导出受限最小证据，不保留整个实例代替证据 |
| R-EVIDENCE / R-EVAL | 原始观察、候选、结果、私有答案；任务/实验结束 | 完整性与可重验期限明确；到期删除原件后保留允许的墓碑/摘要，历史结果标明证据已不可复算，不能继续作新Gate依据 |
| R-AUDIT | 批准/撤销、账本、身份与决策摘要；事件或Run终结 | 只保留获准审计字段；不可用“审计需要”把原始正文无限复制进账本 |
| R-DELETE | 删除/撤权墓碑与必要恢复屏障；删除或撤权事件 | 最长保留必须覆盖所有可能恢复旧副本的备份/副本存活期及批准的恢复处理窗口；无法证明屏障完整时恢复环境保持禁读，不能先到期清除墓碑 |
| R-QUEUE | outbox投递与失败通知；确认投递/终态 | 删除前确认无需恢复的逻辑记录；队列保留不影响PG事实与审计期限 |
| R-LOG / R-TRACE | 日志、span、metrics；采集时间 | 分层定义期限和访问；脱敏/聚合后另有策略，源撤权仍做血缘处理 |
| R-EXPORT | 人读报告、下载、诊断包；导出时间 | 登记实际目的地、收件角色与可撤销能力；不承诺不可控离线副本即时消失，超出治理能力时拒绝该导出 |
| R-REMOTE / R-CI | Git对象、PR、CI日志/cache/artifact；发布或运行结束 | 核查服务实际删除/保留能力；关闭PR不等于清除Git对象/日志/镜像副本 |
| R-BACKUP | 数据备份、复制、快照；备份创建时间 | 每份备份有确定最长存活期；恢复先重放墓碑/撤权，不能因恢复使过期数据重新可读 |
| R-SECRET | 临时凭据/缓存；签发时间 | 到期、失租、取消或撤销失效；不能把凭据写入长期归档，密钥轮换另有记录 |

必要的保留例外用单独 `RetentionHold` 登记：限定对象、原因、实际批准者、开始/复核/最长截止时间、读权限和解除流程。未决UNKNOWN可以触发受限保留申请，不能由Agent自行改成无限保留；到期仍无法解决则升级负责人并保持动作阻断。删除与组织要求冲突由有权Owner作具体决定，记录受限存储与剩余期限；本附件不预设法律保留结论。

策略校验同时检查R-DELETE与R-BACKUP的关系，包含离线/异地副本及有效hold；延长备份或hold必须同步延长必要恢复屏障或使该备份不可恢复。墓碑只保存实现阻断所需的最小受控标识，不保留已删除正文；主库回退时从独立、未随该回退丢失的屏障记录重放。无法确定屏障是否完整，禁止重新开放读取或外发。

## 5. 拟建实现与小检查点

| 拟建路径 | 工作 |
| --- | --- |
| `packages/contracts/src/data-governance.ts` | DataFlowRecord、RetentionPolicy、RetentionHold、DataAdmissionDecision字段与Schema |
| `config/data-flows/` | 不含秘密的流向声明、实际配置摘要与审批引用；DRAFT不能直接启用 |
| `packages/policy/src/data-admission.ts` | 用途/字段/区域/当前权限/有效策略判断，拒绝未知目的地 |
| `packages/artifacts/src/retention.ts` | 到期索引、血缘、墓碑、删除回执与受控hold |
| `apps/control-plane/src/data-governance/` | 当前记录查询、复核、失效和准入事件；审批来自认证主体 |
| `tests/qualification/data-governance/` | 合成敏感字段、外发拒绝、TTL、ACL故障、备份恢复测试 |
| `reports/data-governance/<review_id>/` | 脱敏流向图、实际配置引用、正反测试、缺口和批准记录 |

1. **合成阶段。** 为DF-01/03/04/10/12及当前SUT的DF-25/27/28建立最小登记与拒绝函数，用合成敏感标记检查模型载荷、日志和磁盘副本；不发送真实企业内容。
2. **第12/15章通道资格。** 盘点真实CLI/SDK、工具遥测、checkpoint、shell输出与异常路径的实际副本；核对供应商当前账号设置和证据，仅启用已获准数据等级。
3. **第19章落地。** 对每个实际存储/观测/备份目的地填写区域、身份、期限，完成恢复和删除演练；只填一张架构图不算完成。
4. **第20章来源准入。** 加入源ACL、数据分类与传播血缘，满足全部门槛后才小范围回填。扩大来源、字段或目的地重新计算flow_set_digest。
5. **持续复核。** 配置或provider行为变化使相关记录 `SUSPENDED`，停止新的受影响读取/外发；保留必要有界对账，复核后显式恢复。停用要撤销凭据并按保留策略处理已有副本。

## 6. 必须通过的验收矩阵

| ID | 实验 | 通过条件 / 必留证据 |
| --- | --- | --- |
| DG-01 | 缺region、期限、接收账号或Owner，仍尝试企业取回/外发 | 服务端拒绝；缺项与flow revision可定位，不允许UI绕过 |
| DG-02 | 有来源读取权但无模型/遥测外发许可 | 可在获准系统读取，不向未准入目的地发payload；网关捕获与拒绝记录 |
| DG-03 | Prompt、异常日志、trace中植入合成秘密/私有答案标记 | 捕获真实出站payload与落盘内容，证明禁字段未进入各下游；不以模型回答声称没有泄露 |
| DG-04 | 缓存新鲜但ACL服务不可用；或ACL在线但权限超龄 | 两种情况都拒绝检索、摘要、原文和模型输入；与第20章同一策略 |
| DG-05 | 候选/来源被撤权，但已有摘要、checkpoint或尚未过期下载链接 | 新的读取/Range续传按当前ACL拒绝；签名URL不能直达绕过，测在途停止边界和批准SLO，不宣称撤回已下载字节 |
| DG-06 | 到期清理与有效hold并存 | 只删除适用对象，有删除回执；hold限定对象/截止时间，非hold对象不被拖延 |
| DG-07 | 从过期/撤权前备份恢复；墓碑已到期或屏障源不可用 | 先重放完整墓碑、撤权、当前TTL；策略拒绝墓碑早于可恢复备份到期，屏障不全保持禁读；旧批准/凭据不复活 |
| DG-08 | provider区域/保留配置、日志目的地或CI上传范围改变 | 配置漂移使对应资格失效并阻断受影响流；不能继续引用旧批准 |
| DG-09 | 跨租户查询、导出、运维及支持账户读取 | 按当前角色/scope拒绝；仅有存储admin能力不自动成为业务浏览许可 |
| DG-10 | provider不支持请求的区域限制/删除期限 | 记录真实限制并拒绝该数据等级/流向；改为适配的目的地或明确缩小数据范围后重新评审 |
| DG-11 | 证据原件到期或删除后，尝试新Final/发布引用 | 明确缺少可核验原件并阻断，不能因历史hash还在而当作证据完整 |
| DG-12 | 在原文ACL收紧同时队列中有待发送模型/导出任务 | 发送前重读授权与flow状态；已发出的副本登记后续处置，不谎称已撤回 |
| DG-13 | 工单/账单/客户标识、合同或holdout答案意外进入Prompt/trace | 捕获真实出站与落盘字段，按来源用途拒绝/脱敏；模型输出和聚合不得泄漏隐藏答案 |
| DG-14 | Prompt/corpus/tool/provider配置变化仍沿用旧批准 | 请求与冻结manifest不符时拒绝；记录远端模型实际请求/响应身份与时间，不要求不可取得的权重摘要。历史ML/未来本地模型另核字节与来源 |
| DG-15 | 工单/草稿/政策缓存到期或撤权，但Worker内存/重试队列/证据仍有副本 | 逐副本应用保留/停止策略，provider副本按实际能力单列；有限证据保留单独hold，不用清Redis key冒充全删。本地模型cache属于历史或后续子范围 |

DG编号为本附件新增的数据治理测试，**不是v3原检查ID**；与[93 v3检查映射](93-v3-acceptance-coverage.md)相关L2-S/运营条目建立引用，不能替换其余安全要求。

## 7. 企业数据准入签收与完成清单

签收对象至少包括 `flow_set_digest, policy_set_digest, account/config_revisions, qualification_refs, allowed_data_classes, approved_scope, reviewer_identity, valid_until`。任何实际期限或区域仍未知、适用DG测试未运行、权限链断裂或删除能力不满足批准策略，都保持阻断；不能以“后续补齐”自动激活。

- [ ] DF-01–29已逐项标记（历史DF-21–24默认非活动）适用/不适用；适用项按真实系统展开，没有未登记接收者。
- [ ] 处理、存储、备份、支持访问区域有配置与服务证据，未从用户所在地猜测。
- [ ] 每种副本有明确期限、起算点、删除延迟、备份到期和负责人，例外可到期复核。
- [ ] 访问者与用途明确，源ACL、派生访问、外发许可分别检查；服务不可用即拒绝。
- [ ] 必需DG测试及相关安全/恢复测试有实际报告，已知限制阻断相应流向。
- [ ] 有权主体签收指定范围；合成/公开数据试点不冒充企业数据资格。

下一次可复制：

> 按第94章先建立合成DataFlowRecord与RetentionPolicy校验，覆盖缺区域/期限拒绝、未授权模型外发、日志秘密过滤、ACL不可用和过期备份恢复。只使用公开源码与合成标记，不连接真实企业来源。列出需要实际账号设置或Owner决策的字段，将结果写reports/artifacts；仅另获文档许可后更新91/92，不填虚构地区、保留天数或批准人。

## 8. GenAI 新增的准入与删除验收

| ID | 反例 | 必须观察到的结果 |
| --- | --- | --- |
| DG-16 | SUT模型已准入，Verifier/Grader使用未登记账号或endpoint | 各通道独立拒绝，不继承同品牌批准 |
| DG-17 | 工单正文包含假key/跨租户账单引用 | 实际出站、工具返回、trace、日志与公开报告逐出口核验，不能仅测UI |
| DG-18 | 政策撤权/删除后索引、embedding和缓存仍命中 | 当前授权入口阻断所有片段/引用；派生副本按血缘删除并留回执 |
| DG-19 | 候选SDK关闭trace或伪造调用结果 | 可信代理/Observer记录不被候选控制；证据缺失为INCONCLUSIVE，不生成PASS |
| DG-20 | 备份恢复使旧工单/Prompt/政策或业务批准复活 | 重放当前墓碑/撤权，旧能力无效；真实通信/退款与EGAP发布分别对账 |

本轮仅定义和迁移这些数据范围，没有批准实际供应商区域、保留天数或企业数据，也没有运行SUT模型/Grader。当前HTTP mock不证明任何provider侧删除或驻留能力。正式激活依旧逐记录、逐真实账号完成本章验收。
