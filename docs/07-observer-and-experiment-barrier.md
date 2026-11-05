# 07 · 独立观察与实验屏障：证明实际工具、政策与提交事实

状态：`NOT_STARTED`。新HTTP客户端的mock记录不是独立GenAI trace；本章网关、Observer与真实屏障尚未实现。前置[05真实SUT](05-genai-support-baseline.md)、[06 DRAFT](06-contract-registry.md)，下一章[08 Oracle](08-native-oracles.md)。

## 1. 两类观察目标

第一类是语义路径：实际传给政策/账单工具的参数、返回对象与policy revision、LLM实际请求的Prompt/Tool Schema摘要，以及结构化建议。用于SUP-01/04/07/08。

第二类是业务效果与因果：认证tenant与工具权限、是否产生敏感效果、ticket revision、draft事务提交与DONE顺序。用于SUP-02/03/05/06。候选自报trace、UI事件和最终数据库快照不能单独证明全过程。

所有事实绑定candidate或baseline、environment/reset、job/ticket revision、call/attempt、collector身份和摘要；模型hypothesis单独保存，不进入事实数组。

## 2. 07.1 建立事实与权限映射

| 事实 | 可信采集位置 | 不能替代它的内容 |
| --- | --- | --- |
| tenant与授权主体 | 外部认证/工具网关的实际决策记录 | 模型传入tenant_id、客户文本 |
| 账单/合同/使用事实 | 只读fixture服务/DB快照及版本 | 候选生成的解释或重新构造的日志 |
| 检索请求与返回政策 | 候选外检索网关完整输入/输出及corpus/index版本 | 仅输出policy_ref或截断UI事件 |
| 工具参数、拒绝、真实效果 | 工具执行网关与受控业务状态 | 只记录“调用了refund”字符串 |
| LLM实际输入与返回 | 受控模型代理，request ID、model/config、usage、摘要 | 候选配置文件或任意stdout中的model字段 |
| draft提交与DONE | DB提交/变更轨迹、事务与job关联 | 最终两个字段都存在 |

语料中含Prompt Injection属于被测数据；采集器不执行其指令。候选运行域无凭据/网络直连路径绕过网关。Observer只读业务数据；屏障控制身份、业务执行身份和证据写身份分开。共包不等于共享权限。

## 3. 07.2 精确屏障与非干扰

首期需两个窄能力：`BEFORE_POLICY_LOOKUP`用于固定调用时点并记录真实参数；`BEFORE_DRAFT_COMMIT`用于观察完成状态是否提前。SUP-05另需`BEFORE_JOB_CLAIM`或revision提交屏障。每个能力独立校准，不因一个可用就宣布全部支持。

优先在受控工具代理/存储适配边界实现可观察hook；业务代码新增hook须记录独立build revision，说明透明转发和目标窗口，不直接插入“正确答案”。屏障只控制时机，不改变账单、规则、候选参数或工具返回值。无法保持非干扰则该实验无资格。

`arm → 实际命中 → HOLDING → release → 原请求继续`必须有独立回执。不能用sleep或“任务大概已排队”代替命中。selector限定environment/reset、job、ticket revision、operation及epoch，禁止全局暂停所有租户。

## 4. 07.3 受限控制协议

拟建`armBarrier/inspectBarrier/releaseBarrier`只接收已注册模板、资源身份、有限TTL和请求摘要，不接受自由SQL/脚本/生产URL。arm前登记EffectOperation；重复相同ID/摘要读取同一状态，摘要不同冲突。

状态为`ARMED → HOLDING → RELEASED`，另有`EXPIRED/CANCELLED/UNKNOWN`及明确恢复路径。release不是把原操作重新发一次。返回未知时先查询同一barrier，不创建新ID“重试”。TTL到期的处置必须由预先冻结策略决定并记回执，不能悄悄释放后宣称还在hold。

记录目标/实际命中、控制器版本、caller、租约、因果顺序、原请求摘要、释放结果和清理。只有真实HOLDING命中后才执行相应故障窗口断言。

## 5. 07.4 完整采集与原始对象

Observer从独立来源采集，Collector校验摘要/身份后保存不可变ObservationBundle。大对象进入受保护持久ArtifactStore；当前MemoryArtifactStore不具备此能力。原始授权头/key不落盘；需要保留的模型/工单内容按94限制读者、区域和期限。

候选stdout只作为不可信辅助附件。代理摘要与正文绑定；截断必须注明范围，不能以截断trace证明没有某次越权。每个来源提供序列/水位/断段状态；不同机器wall clock不能替代因果，优先使用事务提交、调用父子ID和屏障回执。

“从未访问其他tenant”“hold期间从未DONE”需要完整受保护轨迹或等价证明。缺权/断段为GAPPED/UNKNOWN并阻止PASS。已捕获满足批准最小反例的违规可留为predicate，但不能把未知执行伪造成COMPLETED。

实际Prompt包括模板版本与渲染后的请求摘要，工具Schema记录实际发送内容；引用manifest而不核对实际调用，不能证明候选遵守了manifest。远端model返回信息不足时如实标未知，不构造权重hash。

## 6. 07.5 正反与非干扰校准

健康版本分别运行无控制器、控制器旁路、真实hold/release，确认业务输入、输出结构、合法失败与有限恢复语义一致。LLM措辞不要求逐字相同；结构化事实与费用/重复策略按冻结合同比较。

然后分别运行latest-policy-only、wrong-date、提前DONE和坏观察器。前两者区分“参数错”和“正确参数下检索错”；提前DONE需要提交屏障；坏观察器必须导致ERROR/INCONCLUSIVE，不能产生PASS。变异属SYNTHETIC_MUTATION，答案不提供给Planner/Coder。

SUP-01证据链：fixture账单事件→实际检索参数→返回政策版本→实际模型输入→结构化判断。SUP-06链：arm→提交命中→hold期间状态轨迹→释放→事务提交→DONE。SUP-03分别记录建议、调用尝试、授权拒绝与实际效果，不能混为一项。

## 7. 07.6 中断、撤销与清理

恢复先读账本和真实barrier/operation状态，不能再次提交工单或再发LLM请求。取消后停止新动作，有界释放/对账并保留在途事实。模型请求可能已经计费；没有响应不意味着未发生。

清理核验屏障、短期权限、队列消息、fixture数据、代理路由、卷/容器均无未登记残留。未知资源隔离并移交，无法证明环境干净则不复用；不删除日志消除不确定性。

## 8. 退出门槛与交接

| 负例 | 预期 |
| --- | --- |
| 错job/revision/reset/epoch命中 | 拒绝或明确未命中，不能评价目标实验 |
| 候选伪造policy_ref、trace或model标签 | 独立采集能识别不一致/不足 |
| 模型参数tenant与认证tenant不同 | 授权拒绝/独立记录，不能按body改租户 |
| trace丢段、错误collector或另一候选证据 | INCONCLUSIVE/身份拒绝，不PASS |
| 网关可被直接外连绕过 | 资格失败，不继续生成可信trace声明 |
| 重复release、提交未知、取消后迟到效果 | 幂等、对账及残留可审查 |

只有真实hook、权限、精确命中、完整性、非干扰、正反对照与清理都通过，才登记单项capability SUPPORTED。证据保存`artifacts/evidence/observer/<calibration_id>/`。修改hook、代理、权限或存储路径产生新revision，旧资格不自动继承。

下一步先完成政策工具参数/返回记录这一窄观察链，再接08 SUP-C01/08；在观察可靠前，不用模型判定掩盖采集缺口。
