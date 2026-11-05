# 02 · 工程取舍：GenAI客服发布验证

状态：`GENAI_MIGRATION_IN_PROGRESS`。本章记录目标方案与当前限制；不表示已经运行真实客服Agent、独立Oracle或LangGraph。前置[01架构](01-system-architecture.md)，下一步[03预检](03-environment-preflight.md)。

## 1. 选择标准

业务固定为合成B2B SaaS续费退款争议，展示Prompt、政策检索和工具Schema变更如何产生业务语义回归。评估目标是准确发现当前候选的违规及减少无关探索，不以测试数量或“另一个LLM赞同”为成功标准。

第一轮固定模型和Worker，只增加十一月政策与Tool Schema v2并保留历史兼容。分别校准latest-policy-only和wrong-date变异；六个因素同时变化不利于区分根因。模型升级、handoff、多语言和复杂异步恢复随后独立研究。

## 2. 技术栈与代价

| 选择 | 原因 | 代价与验证 |
| --- | --- | --- |
| TS控制面，Python Verifier | 保留现有骨架；Graph策略和治理事务分离 | 共享版本化JSON Schema，双端运行时校验 |
| 自有Python API/Worker客服SUT | 能明确Job、工单、工具与草稿协议 | 不声称开源Demo天然拥有这些业务 |
| SUT PostgreSQL + Redis | 持久Job/draft/outbox，队列只投递 | 真实事务、重复投递和崩溃窗口需验证；不承诺天然exactly-once |
| 单个主要Support Agent起步 | 先校准工具、政策与证据链 | handoff后续是新增行为，不隐藏为当前能力 |
| 固定本地政策语料与简单检索 | 先解决版本、日期和tenant语义 | 向量索引/reranker后续有独立manifest和资格 |
| 受控模型/工具网关 | 凭据不交给候选，实际调用可独立记录 | 出站绕过、费用UNKNOWN、采集缺口需要真实否定测试 |
| 确定性Oracle + trace核验 | 业务规则与执行事实可复算 | 不证明所有自然语言语义；语义grader单列 |
| 首期草稿和模拟敏感动作 | 用最小真实LLM流程验证业务，控制外部影响 | 无自动对客/真实退款生产结论 |

源码参考[OpenAI客服Demo](https://github.com/openai/openai-cs-agents-demo)的MIT许可与Agent/tool编排。其航司场景、内存存储和mock业务工具只作参考。固定commit后仍须锁Python/SDK依赖；不直接改造完整Next.js/ChatKit UI作为前置条件。

## 3. 复用边界

保留control-plane、命令信封/身份、file-lab日志与锁、Docker低层动作、摘要原语。ArtifactStore当前是内存实现，不能称作持久证据平台。正式CheckRun Runtime、Codex Worker、Verifier、Final、OPA与发布CI仍待实现；OPA可后续替换可审查TS策略，不为概念新增服务。

ML adapter继续保留历史，GenAI新建自己的HTTP协议、来源锁和账本。公共源码校验原语可抽取，业务来源/资格保持分开。不得把ML源码冻结、14项mock HTTP或Eventuate编译结果当作客服业务PASS。

## 4. 独立性与调用边界

候选可改批准代码、Prompt、检索配置、工具映射；不能修改Owner批准的业务规则、Oracle、隐藏ground truth、证据写口或Gate。Verifier只能提出实验，不能自行授权或签发PASS。两个LLM/两个类不自动构成隔离；必须验证进程、挂载、网络、凭据与写权。

SUT构建域不含provider key、宿主socket、治理DB或发布凭据。运行时使用限定实验的模型/工具能力；外部网关记录真实请求摘要、工具参数/返回值、授权与效果。候选SDK日志辅助诊断，不能取代独立观察。必须防止直连其他模型或DB绕过受控通道。

客服退款Approval与EGAP发布Approval分域。批准Draft PR不批准退款；批准一笔退款也不授予候选修改权。

## 5. 首期实验和Oracle取舍

优先SUP-01/08/07：续费事件日期、政策版本、工具参数和必要证据。最小SUP-02/03/06确认tenant、未授权效果与草稿完成语义。完整SUP-05旧工单覆写和更多故障模型按能力扩展。范围承诺哪项就须有证据，不能为追绿删mandatory。

Oracle用固定账单/政策事实独立判断，不调用SUT的eligibility函数。正确引用ID不能证明正文正确；自然语言草稿由独立语义报告与人工审阅补充。后续把语义评分作为Gate时，需版本化rubric、校准集、重复/聚合、阈值及UNKNOWN处理。

首期Verifier自动执行已有检查与批准参数组合；目录外实验仅提案。成本排序和模型信心不减少Final mandatory。历史PASS仅先验；首期Final仍使用单独execution_set，重复成本计入评估。

## 6. 必须通过的技术spike

- 真实模型请求的Prompt、Tool Schema及返回身份可被候选外代理记录；远端model ID不等于可冻结权重。
- 网关可派生tenant身份，并记录实际返回对象与敏感动作效果；模型传入tenant_id不是授权。
- 精确屏障能命中某Job、ticket revision与提交窗口；sleep和最终快照不能证明因果。
- SUT数据库提交与Job DONE关系、outbox重发与旧revision条件写可在真实崩溃下验证。
- 独立事实与模型假设分离；缺观察、规则冲突、provider超时分别保留UNKNOWN/INCONCLUSIVE。
- 跨语言规范化摘要、Schema、错误码和候选完整输入绑定有golden fixtures。

## 7. 明确延期与开启条件

| 能力 | 首期做法 | 开启条件 |
| --- | --- | --- |
| 真实客服/账单/支付Connector | 合成业务库、沙盒工具 | 20/94的ACL、区域、保留与实际授权 |
| 自动发送/真实退款 | 待审草稿、模拟拒绝路径 | 独立业务审批、风险策略、实际副作用对账 |
| 多Agent handoff、向量数据库 | 单Agent、小规模版本化语料 | 明确新增需求和可测收益 |
| 自由测试生成 | 模板内参数化，新模板仅提案 | 独立审核、隔离与Oracle正反校准 |
| 生产多租户、Kubernetes | 受控单宿主，合成A/B tenant否定测试 | 19部署/容量/恢复与跨租户资格 |
| 自动合并/部署 | 精确Draft PR + CI | 独立批准、回退及运营证据 |

## 8. 决策变更

代码、Prompt、语料/索引、检索配置、模型配置、工具与Oracle变更产生新revision。记录来源、影响资格和重验范围，不覆盖旧结果。对候选缺陷使用版本化`CANDIDATE_DEFECT`及`candidate_asset_kind`；旧`CODE_DEFECT`记录保留原schema，迁移显式映射，不悄悄改写历史。

- [ ] 能区分产品Agent、Verifier和Oracle职责。
- [ ] 能说明单场景、简单检索和草稿范围的取舍。
- [ ] 工具插件化没有放开任意执行。
- [ ] 模型随机性与总成本没有被省略。
