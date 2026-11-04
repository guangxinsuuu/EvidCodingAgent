# 93 v3 验收覆盖附件：原目录到实施章节的逐项追溯

验收状态：`NOT_RUN`。公共基础已有部分实现（见92），本附件未据此签发任何完整检查资格；表中的“纳入”“必须”“通过”描述未来计划及验收条件，不表示已经实现、测试、批准或取得生产资格。所有实际 Owner 主体、批准记录、阈值和环境能力仍待确定。

本附件保留 v3 的原始检查 ID 和名称，将其映射到本手册的章节与阶段。**完整覆盖可追溯只是计划完整性，不等于原生系统、平台、企业接入或生产全部通过。** 本附件不修改原 v3，也不自动扩大已批准的任务 scope、mandatory 集合或发布权限。

历史2026-11-03主场景更新：首期展示改为Stable→新增功能候选的Release Verification，单缺陷Incident Repair作为开发校准与另一入口。下面56项原ID/原名保持不变；新功能检查另外登记，不把原目录改名充数。历史Stable结果只供对照/资格与风险分析，不能成为新候选Final。文档后续修改须先获用户明确允许。

2026-11-04经用户明确授权，当前主业务改为 **Async ML Inference Release Verification**。本附件保留原56个ID、名称和旧业务验收方向，作为历史来源追溯；**第3节原“首期”均指旧Eventuate计划，已退出当前ML主线**。当前ML业务适用性以新增3.1及11节为准，不能把旧订单检查改名或把旧构建证据计为ML通过。L2/L3公共治理要求继续适用，但需在新执行栈重新取得对应资格。

## 1. 来源、范围与计数口径

一手本地来源：

- [v3 整合正文](../../EGAP-Order-Approval/docs/eventuate-production-plan-v3.md)：第30节 L1 基本正确性、第31节异步与并发、第32节 L2 可靠性、第33节 L2 安全、第34节 L3、第52节首批五项映射。
- [外部接入 v3](../../EGAP-Order-Approval/docs/external-integration-v3.md)：接入01–06，包含字段检查、变化事件、双路径边界、激活门槛和代表性来源。
- [v3 PDF](../../EGAP-Order-Approval/output/pdf/EGAP_Eventuate_Visual_Production_Plan_CN_v3.pdf)：图示与全文阅读参考；章节编号不等于 PDF 物理页码。
- [当前总体阶段](00-overall-plan.md)、[ML Oracle](08-native-oracles.md)、[任务与 seed](09-seeded-defects.md)、[安全资格](15-security-and-qualification.md)、[真实发布资格](17-publication-and-ci.md)、[后续范围](22-future-roadmap.md)。

| 来源目录 | 原始条目数 | 本附件处理 |
|---|---:|---|
| L1-01…L1-24 | 24 | 原 ID、原实验名逐项保留 |
| L2-R01…L2-R12 | 12 | 原 ID、原故障边界逐项保留 |
| L2-S01…L2-S10 | 10 | 原 ID、原否定测试逐项保留 |
| L3-01…L3-10 | 10 | 原 ID、原完整流程逐项保留 |
| 接入01–06 | 无原始检查 ID | 17个叙述小标题与24个原表行，共41条来源映射；不伪造“原始 check ID” |

56个 L 系列 ID 不重编号。接入部分以“原章节/原名”定位，同一要求可能在叙述、字段表、激活表重复强调；41条映射不是41个独立测试，更不能和56直接相加宣称测试覆盖率。原文另列的8类激活测试在第8节作交叉核对。

## 2. 阶段和纳入标记怎样读

| 阶段 | 当前手册章节 | 资格含义 |
|---|---|---|
| G0 可复现环境 | 03–05 | 固定来源、工具链、实际环境、smoke与清理 |
| G1 可裁决实验 | 06–09 | DRAFT冻结、独立观察、屏障、Oracle、健康/seed校准；返回06.5完成正式批准 |
| G2 可信执行 | 10–11 | 真实PostgreSQL事务、多Worker、预算、故障恢复与对账 |
| G3 发布验证与修复闭环 | 09发布切片、12–14 | 外部新功能候选、变更影响、针对性Discovery、按授权修复、Final及诚实停止 |
| G4 受控交付 | 15–17 | 实际隔离、精确人审、指定Fork的PR与CI资格 |
| G5 试点价值 | 18 | 同候选发布QA对照、隐藏裁决、漏报/测试成本/人工投入，修复收益另计 |
| 后续运营 | 19 | 企业身份、staging、生产准入与恢复演练 |
| 后续接入 | 20 | 指定来源的ACL、同步、删除、撤权与运营资格 |
| 后续强化 | 21 | 并发/业务幂等/恢复强化、扩展业务与多租户 |

第3节历史“首期核心”指旧五项native-final，其余章节当前主线按批准ML Profile解释。当前适用ML-01…06须分别映射到ML-C01…06检查与断言；目录列出不等于全部已准入。ML-07单列后续质量/性能准入。“部分”只覆盖表中明确子域，未覆盖子域保持缺口。“后续”是首期声明不具备的范围，不能因为延期而在企业级验收中自动标 N/A。

业务语义分类：N=原生流程的兼容性或正确性目标，仍需Owner确认和实测；H=需要额外注入、并发、恢复或终态政策的强化验收；X=新增业务语义/接口。N不表示源码已经满足，H不必然要求改业务代码，X不能冒充原生承诺。

早期05–09的 `authority_mode=file-lab / evidence_scope=calibration-only` 结果只证明当次冻结草案和实验条件。正式候选 Final 需ACTIVE Bundle与相应资格；第10章保留 origin 迁移，不能将草案校准 PASS 自动升格。`FINAL_ONLY` 不要求真实发布；可信 `CHANGED` 且 `DRAFT_PR_CI` 才进入G4真实发布链，改变目标须重新准入。两种模式的可信 `UNCHANGED` 候选都需当前独立Final确认任务满足，再进入NO_CHANGE，发布/CI为NOT_APPLICABLE，不制造审批或空PR。

## 3. 历史L1：原Eventuate业务目录24项

以下原 ID、原名逐字沿用 v3 第30–31节。各行保留旧计划上下文，均未有完整验收资格；不再作为当前ML mandatory。具体保留/替换/延期在3.1逐项登记，订单金额场景不作强行一对一改名。

| 原ID | 原实验名 | 语义与首期纳入情况 | 实施章节 / 阶段 | 具体验收证据与前置 |
|---|---|---|---|---|
| L1-01 | 合法足额订单 | N；首期核心 EV-C03 | 05、07、08；G0/G1，14/G3重验 | 隔离客户额度100、订单40；订单APPROVED、同customer/order预留40、关联消息与一致账务切面；真实DDL、Observer、金额域和窗口已明确。 |
| L1-02 | 持有成功回复 | N；首期核心 EV-C01 | 07、08；G1，14/G3重验 | 先ARMED再建单，实际HOLDING命中；持有期无短暂APPROVED的完整轨迹；原消息释放回执、成功预留与释放后活性；hook非干扰与不可绕过范围先校准。 |
| L1-03 | 额度不足 | N；首期核心 EV-C05 | 05、07、08；G0/G1/G3 | 额度100、订单120；REJECTED及INSUFFICIENT_CREDIT、目标无新增成功预留、登记的旁观账本不变；失败回复与同订单因果绑定。 |
| L1-04 | 未知客户 | N；首期核心 EV-C06 | 05、07、08；G0/G1/G3 | 触发前证实客户不存在；REJECTED及UNKNOWN_CUSTOMER；未创建客户/错误预留，限定旁观实体不变；不能用固定大ID猜不存在。 |
| L1-05 | 金额等于可用额度 | N；首期核心 EV-C04 | 05、08；G0/G1/G3 | 等额订单合法APPROVED，精确预留后可用额度0；十进制/单位/scale约定与干净fixture。 |
| L1-06 | 已有订单占用额度 | N；首期五项不完整覆盖，后续补强；新客户足额/等额不能替代 | 05可characterize；21后续强化 | 已有合法预留后再建单，验证按available而非总limit判断；前后同客户完整账本、第二单终态/拒绝原因；需新增固定fixture与Oracle。 |
| L1-07 | 服务不可用 | N安全+H恢复；延期完整故障场景，不能把持有回复当服务停机 | 07能力研究；21后续强化 | 明确暂停Customer/相关依赖，停机期间无误批准，恢复后按批准窗口收敛；故障开始/解除、健康、消息与业务轨迹；EV-C02探索不是首批mandatory。 |
| L1-08 | 迟到但有效回复 | N；C01仅覆盖受控hold/release，超观察窗后的完整迟到语义延期 | 07、08部分G1；21后续强化 | 初始观察超时后仍有效的回复到达，订单不被擅自EXPIRED/拒绝，最终合法收敛；区分业务deadline与测试窗口，记录两次观察及同因果链。 |
| L1-09 | 失败回复/原因映射 | N；C05/C06覆盖两类业务拒绝，技术失败分类为部分覆盖 | 08、09；G1；11/14归因；21补全SUT故障 | 同订单回复类型→拒绝原因证据；另注入技术不可用证明不伪装业务拒绝；已有两种业务失败PASS不能代表全部异常映射。 |
| L1-10 | 错order/customer关联 | N关联目标+H对抗注入；首期正向绑定不替代错误消息测试，延期完整项 | 07、08部分；09 F-SEED-03计划；21后续强化 | 两组目标/旁观实体，受控错误关联回复，证明不误批准/改错预留；实际header、Saga关联和精确注入能力、Owner批准范围先成立。 |
| L1-11 | 响应和持久化核对 | N；首期辅助断言，纳入五项实现而非新增第六核心check | 05、07、08；G0/G1/G3 | HTTP响应、orderId、持久状态、预留金额与消息证据联合核对；“HTTP 200但PENDING”不得报告最终批准。 |
| L1-12 | 无缺陷控制与活性 | N；首期校准/任务控制必需，不只看五项检查列表 | 08、09；G1；12–14/G3；18/G5 | 健康版本五项有效结果、无缺陷WorkOrder的NO_CHANGE及正常订单继续可批准；无隐藏答案泄漏，不能强迫生成补丁。 |
| L1-13 | 相同message_id重复命令 | H传输重投资格；延期，平台队列去重不能代替SUT去重 | 07适配能力；21后续强化 | 真正同message_id重投、原始/重投envelope、消费事务与业务净效果；不是普通barrier重复release协议测试，也不只数Map键。 |
| L1-14 | 新message_id相同业务key | H且业务语义待决；先characterization，首期不强制新增语义 | 06待决；21后续强化 | 新message_id、同order/customer/amount的重放；Owner决定复用/冲突、保留期；账务与结果一致性、冲突记录；不能直接判上游bug。 |
| L1-15 | 同key不同金额 | X/H企业冲突语义；延期单独批准 | 06待决；21后续强化 | 相同业务key不同金额不静默覆盖；原记录、请求摘要、冲突结果和净额度；需明确业务key、单位与冲突规则。 |
| L1-16 | 重复成功/失败回复 | H终态/重复资格；延期，原生成功路径不覆盖 | 07消息注入；21后续强化 | 相同/语义重复成功及失败回复，前后状态转换和账本；明确终态政策、实际去重边界，拒绝非法回退/额外效果。 |
| L1-17 | 回复乱序/已完成Saga回复 | H；延期，需实际生命周期与注入能力 | 07框架调查；21后续强化 | 已完成Saga、乱序/旧回复及另一订单对照；忽略/错误原因、终态不回退、旁观实体不变；不虚构原生request epoch。 |
| L1-18 | 同客户并发60+50，额度100 | H并发额度守恒；延期完整企业强化 | 06输入/冲突决策；21后续强化 | 真Postgres多进程、屏障/seed、所有请求结果、同客户一致账本；净预留≤100，冲突重试有界，不预定哪个先成功。 |
| L1-19 | reserve提交后consumer退出 | H业务崩溃恢复；延期，EGAP Worker恢复不等价 | 07可观测提交点；21后续强化 | 预留已提交证据、consumer精确退出点、恢复后消息/去重/额度与订单净结果；响应丢失不能作为业务失败。 |
| L1-20 | CDC停启/broker短故障 | H业务传输恢复；延期 | 05拓扑锁定、07观察；21后续强化 | CDC/broker停启时间线、位点/消息事实、无提前批准、恢复收敛与清理；实际CDC模式先确认。 |
| L1-21 | approve前后Order进程退出 | H Saga/业务一致恢复；延期 | 07转换观察；21后续强化 | approve前后分别kill，Saga持久状态、订单轨迹、预留与恢复结果一致；需真实故障点控制，不能仅杀EGAP进程。 |
| L1-22 | DB事务回滚/乐观锁冲突 | H事务边界资格；延期 | 05版本/DB；07事务调查；21后续强化 | 真DB回滚与@Version冲突，业务、消息、去重记录全有或全无/可解释；固定隔离与重试策略，内存测试不足。 |
| L1-23 | 重复HTTP建单 | X HTTP幂等；先记录原生行为，延期新增标准 | 05可characterize；06待决；21后续扩展 | 重复POST可能产生两单；Owner另定Idempotency-Key、payload冲突、保留和事务；新Profile下才要求一次逻辑建单。 |
| L1-24 | 观察器/屏障故障 | 实验可信性；首期资格必需，不是可删除的业务check | 07、08；G1；14、15/G3/G4 | 屏障未命中、轨迹缺段、错环境/epoch、观察缺权导致ERROR/INCONCLUSIVE或明确拒绝；不能有可信PASS；含健康/坏观察器正反校准。 |

首批精确映射：`EV-C01→L1-02`、`EV-C03→L1-01`、`EV-C04→L1-05`、`EV-C05→L1-03`、`EV-C06→L1-04`。L1-09/11/12/24等辅助或资格范围须单独报告，不能为宣传“覆盖更多项”把部分断言自动算成整项PASS。

## 3.1 当前ML主线对24项历史业务要求的逐项处置

下表是语义追溯，不是等价证明或Owner批准。`REPLACED`表示当前ML采用另一个领域规则；`HISTORICAL_ONLY`表示没有合理直接对应，旧要求保持历史而不计ML覆盖；`REUSED_PRINCIPLE`表示继续验证公共原则。所有适用检查仍需06/08批准、实现和独立校准；ML-xx为规则，ML-Cxx为检查目录ID。

| 历史ID | 当前处置 / ML映射 | 章节与阶段 | 未完成项、延期理由与必要证据 |
|---|---|---|---|
| L1-01 | REPLACED：ML-01、ML-02、ML-03正常任务 | 05、07–09；G0/G1 | 真实图片Job从受理到结果，提交/实际模型和预处理身份、结果持久化；API done不是全部证明 |
| L1-02 | REPLACED：ML-01结果写入屏障、ML-02排队时模型切换 | 07–09；G1 | 可信hold/release、完整状态/装载轨迹；不能把睡眠或候选自报当屏障 |
| L1-03 | REPLACED：ML-06非法输入失败语义 | 05、06、08；G0/G1 | 非法图片/模型输入不兼容的失败与旁观Job无污染；额度不足本身无ML等价 |
| L1-04 | REPLACED：未知Job/不支持类型的真实API边界 | 05、06、08；G0/G1 | 真实404/415与无意外创建；不存在的model选择接口先作为新需求，不伪造上游能力 |
| L1-05 | HISTORICAL_ONLY；输入大小/尺寸边界另由ML-06定义 | 06、08；G1 | 金额等额不迁移；新边界须按实际API和批准schema正反校准 |
| L1-06 | HISTORICAL_ONLY；资源积压/并发属于独立ML容量范围 | 21；后续 | 没有信用净额等价物；队列积压、配额与背压另定契约/性能环境 |
| L1-07 | REPLACED：ML-05 Worker/Redis不可用 | 07–09、21；G1/强化 | 故障期间无假成功，恢复/明确失败，claim与实际结果事实；阶段取决于批准tier |
| L1-08 | REPLACED：ML-01/05迟到结果 | 07–09；G1 | 实验超时非业务到期；迟到完成按保留/截止合同处理，旧attempt不能覆写 |
| L1-09 | REPLACED：ML-05/06失败归因 | 08–09、11、14；G1/G3 | 非法输入、模型不兼容、依赖不可用分别判定；环境故障不能伪装合理业务拒绝 |
| L1-10 | REPLACED：ML-02/03错Job、模型、输入关联 | 07–09；G1 | 两组Job及旁观对象，可信错误关联注入，提交manifest/执行/结果三方核对 |
| L1-11 | REPLACED：ML-01/02响应与结果存储核对 | 05、07–09；G0/G1 | 202只受理；done与结果、身份/摘要、持久写入证据一致 |
| L1-12 | REUSED_PRINCIPLE：健康/no-seed/NO_CHANGE与活性 | 08–09、12–14、18；G1/G3/G5 | 正确候选不误改、释放后能完成；真正UNCHANGED与零Coding的CHANGED分开 |
| L1-13 | REPLACED：ML-04同Stream entry重复/重领 | 07–09；G1 | 真实重复载荷、pending/claim、fencing和结果净效果；不是普通重复API调用 |
| L1-14 | REPLACED：ML-04新entry同逻辑Job | 06–09、21；G1/强化 | 同逻辑身份与模型/输入摘要的复用/冲突需批准，不能直接判上游bug |
| L1-15 | REPLACED：新HTTP幂等合同 | 06、21；后续 | 同key不同payload/model冲突；上游无HTTP幂等键，新增语义而非金额校验改名 |
| L1-16 | REPLACED：ML-04重复成功/失败结果 | 07–09；G1 | 终态政策、重复结果与旁观Job不变；旧attempt不可覆写合法新结果 |
| L1-17 | REPLACED：ML-04/05旧attempt迟到/乱序 | 07–09；G1 | 实际fencing token/claim与终态轨迹，旧Worker迟到写入拒绝；不虚构request epoch |
| L1-18 | HISTORICAL_ONLY；多Worker竞争另用ML-04/05 | 07–09、21；强化 | 60+50额度守恒不适用；双Worker同Job竞争/并发模型缓存需新Oracle与实际观察 |
| L1-19 | REPLACED：ML-01/05结果写入后确认前Worker退出 | 07–09；G1 | 写入证据、精确退出点、重领及恢复后唯一一致结果；不只杀EGAP进程 |
| L1-20 | REPLACED：ML-05 Redis/队列故障 | 05、07–09、21；G1/强化 | AOF与恢复配置、实际丢失窗口、pending与结果对账；不假设CDC/broker机制存在 |
| L1-21 | REPLACED：ML-01/05完成标记前后Worker退出 | 07–09；G1 | 逐窗口注入，状态/结果/确认一致；结果写入/完成标记原子性实际核验 |
| L1-22 | REPLACED：ML-04/05 Lua/fencing/存储失败边界 | 05、07–09、21；G1/强化 | 真实原子操作、失败/claim竞争证据；不能以旧Postgres乐观锁测试替代 |
| L1-23 | REPLACED：HTTP幂等扩展（非原生承诺） | 05 characterization、06、21；后续 | 重复POST当前可成不同Job；批准key/payload/model绑定、保留期及事务协议后实施 |
| L1-24 | REUSED_PRINCIPLE：坏Observer/屏障不可PASS | 07–09、14–15；G1/G3/G4 | 错模型/环境/epoch、缺轨迹、失权、未命中均拒绝或INCONCLUSIVE；真/坏观察器校准 |

模型特有ML-03预处理/feature一致、ML-06跨模型schema与ML-07质量没有完整旧L1等价物；按新需求验收。ML-04/05可分层实施，但批准某release tier包含的子项缺能力即阻断，不借“后续”缩减已经冻结的mandatory。

## 4. L2-R：平台可靠性12项

这些检查资格针对 EGAP 代码/策略/运行环境，不是每个业务候选都重复全套。版本、环境或权限路径变化时，重新验证受影响范围；最终Run只能引用仍有效且范围匹配的资格。

| 原ID | 原故障边界 | 首期纳入/延期边界 | 实施章节 / 阶段 | 具体验收证据与前置 |
|---|---|---|---|---|
| L2-R01 | 状态提交后队列未发 | 首期必需 | 10、11；G2，15/G4引用 | 真DB状态/journal/outbox同事务提交；kill在提交后通知前；恢复补派同逻辑工作，无静默丢失；SQL快照、事件序号、派发回执。 |
| L2-R02 | 队列重复/乱序 | 首期必需 | 10、11；G2 | 真多Worker重复/乱序通知；CheckKey、唯一键、版本迁移、拒绝记录；单逻辑结果不倒退，不以队列顺序作权威。 |
| L2-R03 | Worker租约过期仍回写 | 首期必需；file-lab锁不代替此资格 | 10、11、15；G2/G4 | 旧epoch继续注册结果/派发的真实SQL条件失败，Gateway同步拒绝过期能力；新持有者合法进展，所有写入点覆盖。 |
| L2-R04 | 启动任务已成功但响应丢失 | 首期本地执行provider必需；云provider后续单独验 | 11、12、15；G2/G3/G4；19云扩展 | 创建外部进程/环境成功后丢响应；operation/launch身份与远端资源对账，UNKNOWN不换token盲启；具体provider幂等窗口另确认。 |
| L2-R05 | 检查完成但checkpoint落后 | 首期必需 | 10、11、13；G2/G3 | 可信结果已登记后kill Planner；恢复查完整CheckKey/产物，合法复用且不重复副作用；换candidate/phase/reset等拒绝命中。 |
| L2-R06 | 产物上传后DB未登记 | 首期必需 | 10、11、14、15；G2/G3/G4 | 上传后中断登记；collector按身份/摘要找回或隔离孤儿对象；DB缺记录时模型不能补写PASS；恢复前后对象与索引清单。 |
| L2-R07 | 同候选但合同/runner升级 | 首期必需 | 06、10、13、14；G1/G2/G3 | 同candidate更换bundle/oracle/runner版本生成新检查身份，旧结果不能计入Final；摘要与引用冲突的否定测试。 |
| L2-R08 | 预算耗尽/无进展 | 首期必需 | 11、13、14；G2/G3 | 五个budget_pool的并发预留与流水；探索耗尽且Final readiness成立时经FINAL_DRAIN_READY关闭探索、使用受保护Final额度，否则STOPPED；均受总上限/截止时间约束，未完成mandatory不放行，终态仅有界对账/清理。 |
| L2-R09 | 人工批准后候选/base变化 | 首期发布模式必需；FINAL_ONLY不经过发布批准 | 14、16、17；G3/G4 | 批准后替换候选/base/目标，预览/摘要与发布前复验拒绝；重新准入或按规则重新冻结验收，不能改旧批准。 |
| L2-R10 | PR创建成功后网络超时 | 首期DRAFT_PR_CI必需；先simulator再真实隔离Fork | 11、15、17；G2/G4 | 同operation对应branch/PR/head读回，失响应后不重复创建；真实API回执/查询与冲突处理；simulator结果不计真实provider资格。 |
| L2-R11 | 数据库恢复至旧点 | 首期恢复协议及本地隔离回退实验应纳入；完整备份/PITR与RPO/RTO在19 | 11恢复矩阵、15/G2/G4；19后续运营 | DB回退但外部操作仍存在，默认禁写、隔离旧Worker、盘点孤儿/外部事实后恢复；本地回退用例尚须具体化，完整备份/跨对象恢复不可由协议单测替代。 |
| L2-R12 | 取消时外部请求在途 | 首期必需；真实provider按已接入范围补验 | 11、15、17；G2/G4 | 取消停止新动作，保留在途operation，读回真实完成/未完成/UNKNOWN；取消请求与确认分开，原终态只追加对账/清理。 |

L2-R11是需要落实的明确工作缺口：11已有恢复策略、19有完整演练，但不能仅因文字存在就在G2/G4勾选实测完成。首次真实发布前，应把当前试点实际故障范围对应的本地数据库回退用例纳入Qualification计划；完整生产PITR、RPO/RTO仍保持后续未验证。

## 5. L2-S：安全、数据与策略10项

首期采用单租户合成范围，但跨Run、伪造其他tenant标识与权限拒绝仍须测试。完整企业SSO、真实多租户/RLS与真实客户数据资格不从本地测试继承。

| 原ID | 原否定测试 | 首期纳入/延期边界 | 实施章节 / 阶段 | 具体验收证据与前置 |
|---|---|---|---|---|
| L2-S01 | 源码/日志指令要求删验收或外传 | 首期必需；企业来源再补测 | 12、13、15；G3/G4；20接入 | 合成注入内容、实际工具提案与授权拒绝；mandatory未减、canary未外传、无外部写入；不能只用关键词过滤或Prompt自报。 |
| L2-S02 | Candidate修改oracle/contract/results | 首期必需 | 06–08基础、12、14、15；G1/G3/G4 | 实际身份/挂载/存储写权拒绝；路径穿越、链接、摘要替换及同名结果伪造反例；可信原资产保留。 |
| L2-S03 | 构建读取模型/发布/DB凭据 | 首期必需 | 03–05隔离、12、15；G0/G3/G4 | 恶意Python build backend/模型加载/test读取测试canary、宿主文件与Docker socket失败；进程env、挂载/网络、模型代理与发布身份独立的证据。 |
| L2-S04 | 任意URL/SQL/shell提案 | 首期必需 | 07受控adapter、13、15；G1/G3/G4 | 未登记模板、越界参数、任意endpoint/SQL/shell被validator和执行侧拒绝；合法参数仍可用；结构化错误及无实际越权效果。 |
| L2-S05 | 跨Run/租户猜测证据引用 | 首期跨Run及合成tenant否定必需；企业多租户后续 | 10、14、15、16；G2/G4；19/21 | 当前主体+对象scope读取校验；猜hash、借RunA令牌读B、过期缓存拒绝；真实RLS/共享缓存隔离在企业资格另验。 |
| L2-S06 | 伪造actor/无权审批 | 首期真实发布需verified-local-owner；企业SSO在19 | 04身份边界、06、15–17；G1/G4；19 | body.actor/自报角色无效；本地认证主体与目标Git账号/仓库权限可核验，精确批准记录；仅mock operator不能支持真实发布。 |
| L2-S07 | 重放/过期capability与批准 | 首期必需 | 10、11、15–17；G2/G4 | nonce并发/重放、expiry、scope、payload hash、epoch和撤销分别拒绝；合法能力仅消费一次逻辑动作；DB条件/网关回执。 |
| L2-S08 | 测试动作指向生产endpoint | 首期必需，使用无生产权限的合成禁止目标 | 03、05、07、13、15；G0/G1/G4 | 环境注册/白名单/实际网络边界阻断非本实例目标；不以真实生产攻击测试，不以提示词提醒代替硬限制。 |
| L2-S09 | 日志/trace含token和客户数据 | 首期合成canary必需；真实数据/保留政策后续 | 04、12、14–16；G3/G4；19/20 | 受控假token/PII进入工具输出后，Prompt、日志、trace、公开摘要和工件引用不泄露；脱敏/截断/读取ACL测试与范围；真实数据资格另批。 |
| L2-S10 | 模型自报PASS或伪造CheckRun | 首期必需 | 07、08、10、14、15；G1/G3/G4 | 候选stdout或伪造JSON无法登记可信结果；collector身份、执行attempt、环境/证据摘要与Oracle版本核对；缺事实阻断。 |

v3第33节另有“发布前的供应链检查”与“威胁范围与残余风险”，没有独立L2-S ID。它们仍是15/17/19的横切前置：依赖/镜像锁、受控解析仓库、SBOM/许可/风险扫描、无秘密CI、攻击面与残余风险。不能因不在10行编号内而省略，也不能用“绝对隔离”代替明确范围。

## 6. L3：组合端到端10项

| 原ID | 原完整流程 | 首期纳入/模式条件 | 实施章节 / 阶段 | 具体验收证据与前置 |
|---|---|---|---|---|
| L3-01 | 真实SUT正常基线→无缺陷任务 | 首期必需；NO_CHANGE合法 | 05–09、12–14、18；G1/G3/G5 | 健康基线与无缺陷任务隔离，独立结果支持无需修改；NO_CHANGE、范围与清理可追溯，无强造补丁/空PR。 |
| L3-02 | seeded defect→Coding→Discovery | 首期Incident校准链必需，live与scripted-fake分报；不是所有Release任务的运行入口 | 09、12、13；G1/G3 | SYNTHETIC_MUTATION校准、fault base、真实模型Attempt、冻结Candidate、有效反例/正确归因与完整谱系；Release先验证已有候选，可信缺陷且获授权才Coding；不泄露正确V4/seed答案。 |
| L3-03 | Finding→再修复→Final | 首期必需；不强迫每次两轮 | 08、12–14；G3 | 真正ValidatedFinding驱动新Candidate；Final execution set覆盖当前候选全部适用mandatory，无旧候选PASS混入；一次修好或诚实停止分别报告。 |
| L3-04 | Run中断→恢复继续 | 首期必需 | 10–14；G2/G3，17/G4补发布阶段 | kill/restart时间线、FrozenContext/CheckKey/预算、已完成证据与UNKNOWN对账；只用服务端允许事件恢复，终态不被偷偷重开。 |
| L3-05 | Final→人审→自有Fork Draft PR | DRAFT_PR_CI首期必需；FINAL_ONLY为未请求，不冒称通过 | 14–17；G3/G4 | 当前候选Final、用户精确批准、repo/base/head/tree/action一致、真实PR收据；平台前置资格先行，首次限定provider资格计划避免自依赖。 |
| L3-06 | Publish timeout→reconcile | DRAFT_PR_CI首期必需 | 11、15、17；G2/G4 | 同operation真实远端事实确认/明确UNKNOWN，分支/PR无重复冲突；本地模拟与真实Fork故障范围分开记录。 |
| L3-07 | CI失败/PR head变化 | DRAFT_PR_CI首期必需 | 14、16、17；G3/G4 | 失败CI/陈旧head/目标漂移拒绝成功，准确head的可信required checks才完成；CI receipt、provider身份与修复/新Run关联。 |
| L3-08 | 权限/合同撤销与预算中止 | 首期必需 | 06、11、13–17；G1/G2/G3/G4 | 准入后撤权/撤合同/预算耗尽的拒绝，新动作停止、在途保留对账；报告STOPPED/BLOCKED等真实原因，不删历史。 |
| L3-09 | Artifact替换/观察缺失 | 首期必需 | 07、08、12、14、15；G1/G3/G4 | 候选、结果、Oracle或观察被换/缺失时Gate拒绝；Agent自报成功无效；验证当前工件完整性、完整身份和原始证据。 |
| L3-10 | 环境清理与证据归档 | 首期必需；真实发布残留另列 | 05、07、11、14、17、18；G0–G5 | 精确环境资源清单、租约回收/残留接管、barrier清理、工件摘要/保留读取；未知资源不算已清理，正式保留政策待Owner批准。 |

G4的顺序不能倒置：实现Gateway/adapter→回15补验实际代码/身份/网络/凭据资格→RepoOwner批准限定Fork qualification任务及精确候选动作→首次真实演练→审查签发fork-publication资格→常规发布引用资格。不能要求首次任务预先持有由自身产生的远端资格，也不能把首次例外变成通用skip开关。

## 7. 外部接入：原章节与原名逐项映射

外部接入原文没有check ID。以下“接入01/…”是来源定位，不是新运行模板ID。正式实现时可另发布版本化Connector case ID，但必须保留此来源映射。首期只接固定源码、合成输入和隔离测试DB/消息；GitHub知识读取、文档库、Slack/工单等企业Connector不成为G0–G3前置。17的Git发布适配器也不等于20的知识Connector已验收。

### 7.1 17个原叙述小标题

| 原来源 / 原小标题 | 实施章节 / 阶段 | 首期是否纳入 / 延期理由 | 具体验收证据与前置 |
|---|---|---|---|
| 接入01 / 先确定用途与范围 | 03–06/G0/G1；20后续接入 | 首期固定源码/合成工单/隔离事实；企业六类来源延期，非native闭环依赖 | 来源类型、用途、Owner、scope清单；规范/工单/Git/遥测/DB/消息分别说明可得结论，复制日志不自动升为运行证据。 |
| 接入01 / Connector必须声明什么 | 20；19身份前置 | 企业Connector延期；首期来源锁复用部分字段 | ConnectorContract包含连接/租户/项目/Owner/对象/revision/ACL/身份/同步/游标/删除/预算，内容与权限摘要、读取/源更新时间、血缘；密钥仅引用。 |
| 接入01 / 读取能力与写回分开 | 15、17/G4；20后续接入 | 首期实现工具/发布边界；Connector动作目录后续 | list/fetch/changes/resolve_acl/可选observe注册；写回默认关闭；SourceEnvelope缺版本/ACL隔离，来源文本不能更改权限；连接成功不等于语义批准。 |
| 接入02 / 持久化游标与对象版本 | 20后续接入 | 延期，需选定来源版本/游标机制 | 初始快照、增量事件、定期对账；对象/ACL/索引任务/游标可恢复提交，崩溃重放不丢；不能只按content hash去重。 |
| 接入02 / 删除、撤权和失败分开 | 20；19政策前置 | 延期真实来源；合成协议先验证 | 删除/移动/ACL收紧/凭据失效/403/缺页分别注入；先撤读再清索引/缓存/摘要；最大撤权延迟与新鲜度预算由测量和Owner确定。 |
| 接入02 / 读取时重新判断权限 | 15/G4基础；19/20后续 | 首期当前Run/对象权限；源ACL与真实主体后续 | 每次检索/取原文验证当前主体/用途/tenant/源ACL；历史引用不绕权；留存与删除冲突政策、来源撤权血缘影响及后续Gate复核。 |
| 接入03 / 知识支持理解与提案 | 06、13/G1/G3；20后续 | 首期固定引用/提案；企业检索延期 | 来源revision/时间/权限、冲突/过期标记、ContractProposal；相关度/来源数量/模型信心不得输出规范批准或业务PASS。 |
| 接入03 / 证据支持本次实验裁决 | 07、08、10、14/G1–G3 | 首期必需；不能等Connector完成才做 | 接受的CheckIntent、独立Observer、collector身份、candidate/bundle/check/参数/seed/环境/attempt绑定、原始事实与Oracle输出；应用日志不是唯一事实。 |
| 接入03 / 同一来源，两种采集身份 | 07、14/G1/G3；20后续 | 首期证据身份必需；知识回流后续 | 历史DB/日志不能换标签成当前证据；知识风险重新实验，回流保留范围/ACL且不改原始结果；缺切面/屏障为INCONCLUSIVE。 |
| 接入04 / 用最小权限读取 | 03–08、15/G0/G1/G4；20后续 | 首期测试域必需；真实企业来源延期 | 固定commit、只读DB、分配消息范围、独立故障注入能力/lease/恢复；Observer不写业务、候选不拿SQL/云管理/发布凭据；网络及缓存隔离。 |
| 接入04 / 外部写回独立授权 | 15–17/G4；20后续 | 首期仅指定Fork CREATE_DRAFT_PR；评论/工单/文档写回延期 | 独立Action Gateway核对授权、精确对象/目标/payload/expiry/operation；UNKNOWN先observe；读取token与ACTIVE合同均不自动授写权。 |
| 接入04 / 按证据逐步激活 | 00/G0–G5；19–21后续 | 保留先native闭环后Connector顺序 | 源码→ML环境/屏障→适用ML检查→Agent→一个只读来源→遥测/企业沙箱；8类测试、同步/撤权时效、漏删与归因记录；失败隔离不伪装正常。 |
| 接入05 / 实施操作顺序 | 20；19身份/运营 | 延期每个真实来源，先合成Adapter | 注册合同/Owner→最小身份→读/拒绝测试→小范围回填→增量/ACL/删除→新鲜度/重试→激活→周期复核；配置变更有revision。 |
| 接入05 / API事实的适用范围 | 20后续 | 具体厂商账号/API/配额待选，不能预填成功 | 实施时保存版本官方依据、账号资格/限制、验签与授权分别测试；403/超时/缺页不是删除证明。 |
| 接入06 / 所有外部读取先变成可审查的来源对象 | 20后续；03/06来源索引局部复用 | SourceEnvelope正式Schema延期；原JSON只是design-example-only | 正式Schema/来源校验及必需字段负例；null保持未采集含义，不能从示意值生成实际证据；字段逐项见第8节。 |
| 接入06 / 引用可用，仍需逐次授权 | 14、15/G3/G4；20源ACL扩展 | 首期Artifact引用权限必需；企业源身份后续 | content_ref读取按当前权限定；credential_ref只可信组件解析；content/ACL digest分开变化；源ID/revision/时间/定位可追溯。 |
| 接入06 / 升级为检查证据必须增加什么 | 07、08、10、14/G1–G3；20回流 | 首期必需，不依赖真实Connector | SourceEnvelope不能直接写CheckResult；完整执行身份、Oracle/参数/seed/fixture reset/epoch/collector与观察覆盖；伪造verified=true被拒。 |

### 7.2 接入01原字段检查表：5行

| 原名 | 实施章节 / 阶段 | 首期/延期 | 具体验收证据与前置 |
|---|---|---|---|
| 身份与范围 | 20，19身份前置 | Connector延期；首期Run scope另在15 | connector_id、tenant/project、真实Owner、允许来源与用途；越范围读取拒绝；真实Owner仍待决。 |
| 版本与血缘 | 03/06/G0/G1基础；20 | 固定源码纳入，其他来源延期 | object_id/revision/content_digest/读取时间/源定位；内容变化新revision，旧引用仍指向原范围。 |
| 权限与数据 | 15/G4基础；19/20 | 合成身份/数据纳入；企业ACL/保留政策延期 | ACL版本、主体映射、级别、保留与脱敏；撤权和派生摘要拒绝测试；政策由Owner明确。 |
| 同步与恢复 | 20 | 延期 | snapshot/cursor、对象级幂等、删除语义、重试/速率预算；重复/中断/失效游标的原始回执与恢复水位。 |
| 运行边界 | 15/G4基础；20 | native工具范围纳入；Connector边界延期 | 凭据引用、网络、可用动作、停用/复核；不允许凭读取资格写回或持有通用管理权限。 |

### 7.3 接入02原变化事件矩阵：5行

| 原事件名 | 实施章节 / 阶段 | 首期/延期 | 具体验收证据与前置 |
|---|---|---|---|
| 内容更新 | 20后续接入 | 延期，单来源合成测试先行 | 对比来源版本，持久化任务再索引；新旧事件乱序不覆盖新对象；不透明版本按Adapter规则而非字典序。 |
| ACL收紧 | 19/20 | 延期源ACL；首期运行授权撤销由L3-08另验 | 撤权先阻断读/工具，再失效缓存与派生摘要；实测延迟满足批准预算，不等下轮全量索引。 |
| 明确删除 | 20 | 延期 | 来源明确删除回执、墓碑、索引/保留处理；迟到更新不复活；合法留存仍不恢复当前访问。 |
| 403 / 超时 | 20 | 延期Connector；平台访问错误分类首期有 | 标连接/权限失效并复核；不全量删除、不填同步成功；有界重试与隔离记录。 |
| 游标失效 | 20 | 延期 | 标缺口，按厂商机制回填/重新同步并对账；不静默跳过；中断前后水位与对象清单可核对。 |

### 7.4 接入03原双路径验收边界：5行

| 原问题名 | 实施章节 / 阶段 | 首期/延期 | 具体验收证据与前置 |
|---|---|---|---|
| 绑定对象 | 06–08、10、14/G1–G3；20 | 首期证据绑定；企业来源绑定后续 | 知识绑定来源/权限，证据绑定冻结candidate/check/环境实例；相互串用的负例必须拒绝。 |
| 允许结论 | 06、13、14/G1/G3；20 | 首期必需，企业检索扩展复验 | 假设/影响提案与一次适用断言结果分离；模型自述不得成为Owner批准/PASS。 |
| 权威检查 | 07、08、14、15/G1/G3/G4；20 | 首期可信采集；源ACL/新鲜度后续 | 知识核对来源版本/当前ACL/时效；证据核对采集身份/因果/完整性；签名不证明Oracle语义。 |
| 不足处理 | 08、11、14/G1–G3；20 | 首期必需 | 知识过期/冲突显示或拒绝；运行缺失为ERROR/INCONCLUSIVE并阻断；不能从缺数据推出业务正常。 |
| 回流使用 | 14证据不可变/G3；20 | 首期保留原事实；知识回流延期 | 回流摘要保留原证据版本/范围/ACL；不能反向覆盖原观察，隐藏holdout不泄漏到日常知识。 |

### 7.5 接入04原单Connector激活门槛：5行

| 原门槛名 | 实施章节 / 阶段 | 首期/延期 | 具体验收证据与前置 |
|---|---|---|---|
| 正确读取 | 20后续接入 | 企业Connector延期 | 允许范围的正向读取、字段/revision映射、来源定位，实际API/对象版本；不能以连通性代替。 |
| 权限隔离 | 19/20 | 延期每个来源；native角色资格另在15 | 越权、跨tenant、过期/撤销凭据、缓存和原文读取负例全拒绝；当前身份映射与最小scope。 |
| 变化恢复 | 20 | 延期 | 重复/乱序、游标恢复、编辑/删除/撤权组合测试；完整事件、水位、墓碑、最终对象/ACL对账。 |
| 故障处理 | 20 | 延期 | 限流、超时、403、缺页、源不可用不造成误删/漏删未报或误放行；有界重试、明确未决和停用。 |
| 审批与运营 | 19/20 | 延期，实际Owner/时效/保留均待决 | Owner签收、同步/权限新鲜度SLO、周期对账、停用/凭据回收、删除与留存政策及演练结果。 |

### 7.6 接入05原代表性来源表：4行

| 原来源名 | 实施章节 / 阶段 | 首期/延期 | 具体验收证据与前置 |
|---|---|---|---|
| GitHub | 03固定源码/G0；17发布/G4；20知识Connector | 源码固定与指定Fork发布分别纳入；Contents/PR知识同步与Webhook Connector延期 | repo/ref/commit白名单、原payload验签、delivery去重、补拉对账；具体API/账号资格复核；17发布收据不能代替读取Connector资格。 |
| SharePoint / OneDrive | 19/20 | 延期；无首期依赖 | 实施时核对Graph driveItem delta；nextLink遍历完整批次才登记deltaLink，deleted facet/游标失效/ACL变化分别测；权限不能靠delta永久继承。 |
| Slack / 工单 | 19/20 | 延期；工单厂商尚未选定 | Slack scope/可访问会话、next_cursor分页、频道/项目/时间窗限制、原引用/当前权限；工单单独Adapter，消息不自动成为批准或PASS。 |
| 测试DB / 事件 | 05、07、08、15/G0/G1/G4；20企业扩展 | 隔离ML Job/队列/结果事实观察纳入；企业DB/消息Connector延期 | 独立只读观察身份、真实Redis key/Stream/result映射、受控查询范围、模型字节/预处理身份及correlation，Collector绑定；不能访问生产或猜表名/header。 |

## 8. 接入叙述中的子测试和字段完整性

接入04“按证据逐步激活”明确列出8类测试，均应进入20的每来源case set，而不是只保留一条泛化的“Connector通过”：

| 原测试名称 | 对应原激活门槛 | 必须增加的实际断言 |
|---|---|---|
| 正向读取 | 正确读取 | 内容、字段、版本、scope与定位一致 |
| 越权拒绝 | 权限隔离 | 错主体/用途/tenant/对象拒绝，缓存和原文一致 |
| 撤权传播 | 权限隔离、变化恢复 | 实测入口阻断与索引/摘要失效时延，超预算拒绝 |
| 重复与乱序 | 变化恢复 | 不重复业务处理、不回写旧版本、不按内容hash漏掉ACL变更 |
| 删除墓碑 | 变化恢复 | 明确删除后迟到更新不复活，403不伪删除 |
| 游标恢复 | 变化恢复、故障处理 | 中断/失效可重放或重建，水位与对象最终对账 |
| 脱敏 | 权限隔离、审批与运营 | canary/敏感字段不进入模型、索引摘要或公开报告；原始受控证据仍最小化 |
| 失效凭证 | 权限隔离、故障处理 | 过期/撤销后停止受保护调用，不用旧缓存和无限重试绕过 |

接入06的SourceEnvelope原示例共有16个字段：`purpose`、`tenant_id`、`connector_id`、`source_kind`、`object_id`、`source_revision`、`content_digest`、`acl_digest`、`observed_at`、`source_updated_at`、`classification`、`allowed_use`、`credential_ref`、`content_ref`、`deleted`、`trust_status`。正式Schema设计应逐键核对原键集合；示例的 `design-example-only/UNVALIDATED/null` 不是生产事实。实现20时需要逐键类型/必需性、权限与泄漏负例，尤其凭据引用仅可信组件解析、内容与ACL摘要分开、引用知道地址也仍需授权。

运行证据的额外绑定单独检查：tenant/run、candidate、ContractBundle、check与Oracle版本、参数/seed、环境实例、fixture reset、execution_attempt、lease epoch、collector身份及观察完整性。不能仅加一个verified字段、复制旧日志或给SourceEnvelope签名就生成有效CheckResult。

## 9. 完成汇总与“必需项全通过”的准确规则

未来CoverageManifest应至少保存：source_document/digest、source_id或原章节/原名、实施check_id/version、章节、stage、profile/tier、适用性决定、mandatory_set_hash、实现状态、execution/assertion/attribution、subject/candidate、bundle、环境/fixture/reset、runner/oracle/collector、参数/seed、原始证据引用、qualification范围/有效条件、Owner决定和未决项。本附件只是这份机读表的审查基线，当前没有产生任何运行Manifest。

完成汇总分四层，不能压成一个百分比：

1. **来源映射完整性**：56个原ID全部出现且名称无漂移；接入六节17小标题、24表行及8类子测试都能定位。重复说明不重复算测试。
2. **实现完整性**：每个适用项有实际模板、参数域、可信Oracle/观察能力和正反控制。只有文档、Schema或测试文件存在不能记COMPLETE。
3. **本次范围验收**：先冻结经批准的适用mandatory集合。所有必需检查对准确subject、phase/execution set、版本和环境身份完成，证据有效且assertion为PASS；平台资格有效，无阻断Finding、未解决UNKNOWN或权限/清理阻断。FAIL、ERROR、INCONCLUSIVE、NOT_RUN、过期/篡改结果均不能放行。
4. **目标完成**：change_kind比较冻结任务base（Release为Stable，Incident为原输入），不比较Agent是否改动。UNCHANGED须当前独立Final且需求满足才NO_CHANGE，publication/CI=NOT_APPLICABLE；有新功能而无修复仍是CHANGED，FINAL_ONLY合格后SUCCEEDED且发布未请求，DRAFT_PR_CI另需精确批准/远端收据/准确CI。回退Stable遗漏新增需求不能通过。企业/生产还需19–21对应资格，不从G4自动继承。

NOT_APPLICABLE必须来自批准的适用规则，记录原因和Owner；延期、能力UNKNOWN、没预算、难注入、没账号都不是自动N/A理由。完整目录里未纳入首期的项保持DEFERRED/NOT_RUN并展示范围，不能放进“全量已通过”的分母或分子玩数字。

同一候选中间Discovery的局部PASS不等于Final；不同candidate/环境reset/Oracle/profile的结果不混用。保留首次失败和物理重试，不以重试到绿抹去flaky。已有有效小范围qualification可复用，但新增组件/身份/网络/provider不能继承未覆盖资格。

## 10. 仍需落实的缺口与Owner决策

| 缺口 | 所在映射 | 下一步产物 / 责任角色（实际主体待决） |
|---|---|---|
| 历史L1与当前ML不是一对一等价 | 3.1逐项处置，ML-01…07 | 06/08明确新Profile；21补HTTP幂等、质量与全面恢复，Business/ML/Verification Owner确定各tier |
| 关联/终态/消息重复的注入能力未证明 | L1-10/13–17 | 07固定框架hook与真实envelope调查、正反校准；未证实前不登记SUPPORTED |
| 本地DB回退用例仍须具体化 | L2-R11 | 11/15补精确故障计划与运行证据，19做完整备份/PITR；Platform/SRE确认scope与恢复阈值 |
| 本地身份不代表企业SSO/多租户 | L2-S05/06/09 | 15–17 verified-local-owner范围，19/20/21真实身份/数据/隔离资格；Security/Data/Repo Owner确认 |
| 真实发布与simulator必须分报 | L2-R10、L3-05/06/07 | 17首次限定provider资格与后续常规资格，实际目标/动作/预算/清理计划；RepoOwner待确认 |
| Connector仍无具体厂商范围和正式Schema | 接入01–06 | 20先合成Adapter，再选一个真实只读来源，发布ConnectorContract/SourceEnvelope/case set；Source/Data Owner待确认 |
| 活性/撤权时效/保留/DR数字未批准 | L1活性、接入运营、19 | 实测建议值、来源与风险记录，Owner批准后版本化；不沿用历史研究与v3中互异数字当既定SLO |

附件维护规则：新增/更名check时保留原ID映射；调整首期范围时按授权更新Acceptance Profile；本附件和docs内决定/状态记录只有获用户明确允许后才能修改，运行事实可先保存reports/artifacts。不得修改过去未通过的事实。报告必须带Profile、候选、环境与阶段，不能笼统声称v3全部通过。

## 11. 当前ML Release新增覆盖（不计入原56项）

| 新增规则 / 检查 | 需求 / 主要章节 | 完成前必须提供 |
| --- | --- | --- |
| ML-01 / ML-C01 | 结果持久化先于逻辑成功；06–09、14 | 实际wire状态映射、失败/保留模型、独立持久结果观察、hold/release与活性 |
| ML-02 / ML-C02 | 提交时固定模型并与实际加载/结果一致；06–09、14 | 模型字节manifest、排队alias切换、正确/late-lookup变异对照；非上游已有承诺 |
| ML-03 / ML-C03 | 输入/feature snapshot与预处理一致；06–09、14 | 输入/预处理摘要、schema/形状/顺序、正确/错配对照 |
| ML-04 / ML-C04 | 重复投递结果一致；06–09、21 | Stream/Job/attempt关联、fencing、重复/冲突/旁观对象观察；HTTP幂等另案 |
| ML-05 / ML-C05 | Worker崩溃恢复或明确失败；06–09、21 | 精确故障窗口、pending/claim、状态/结果/确认对账与批准恢复窗口 |
| ML-06 / ML-C06 | 模型B、preprocessing/schema v2及批准v1兼容；06–09、14 | 新需求、真实模型/输入矩阵、旧新功能正反对照，回退stable不能满足新需求 |
| ML-07 / ML-C07 | 模型质量、切片与性能；21后续 | 数据/标签/许可、泄漏隔离、指标/统计方法、环境/阈值批准；当前未准入 |
| Release入口与选择 | task_mode及Stable/Candidate/需求增量→风险/检查；01、09–14 | 无Coding可验外部CHANGED；模型文件变化仍是变化；历史PASS不冒用 |
| 发布QA效率 | 固定候选三组、相同能力/预算/Final；18 | 漏报/误报、Finding时间、成本、人工分钟与accepted release；修复成本另计 |

这些业务Oracle及评测均未取得实际验收资格。ML-01…07是7类规则，不意味着7项已经实现，也不能将source/API adapter单元测试计入该表PASS。06–09仍可按授权以DRAFT做校准；正式Final需批准的适用检查闭包与当前候选证据。

章节导航：[03](03-environment-preflight.md)、[05](05-async-ml-baseline.md)、[06](06-contract-registry.md)、[07](07-observer-and-experiment-barrier.md)、[08](08-native-oracles.md)、[09](09-seeded-defects.md)、[10](10-durable-runtime.md)、[11](11-recovery-and-budget.md)、[12](12-codex-coding-worker.md)、[13](13-langgraph-verifier.md)、[14](14-final-gate-and-evidence.md)、[15](15-security-and-qualification.md)、[16](16-console-and-approval.md)、[17](17-publication-and-ci.md)、[18](18-evaluation-and-pilot.md)、[19](19-deployment-and-operations.md)、[20](20-enterprise-connectors.md)、[21](21-business-and-platform-extensions.md)、[22](22-future-roadmap.md)。
