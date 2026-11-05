# EGAP · Enterprise GenAI Release Verification

本项目验证企业GenAI应用的新版本：输入稳定版、新功能候选与批准需求，分析代码、Prompt、政策检索、工具和模型配置的变化，选择高价值实验；独立证据确认缺陷后，按授权调用Coding Agent修复，再完成当前候选Final与可选Draft PR/CI。

首个被测业务是 **B2B SaaS续费退款争议的客服处理建议与回复草稿**。使用合成Tenant、账单、使用情况、合同例外和版本化政策。客服Agent是被测产品；Python LangGraph Verifier是EGAP规划器。首期无真实付款或对客发送，业务人工复核与代码发布审批分域。

主实验为自有Stable V3.4→正确V4：新增十一月政策和tool schema v2，保留历史续费事件适用性；latest-policy-only与wrong-date为分别校准的人工变异。版本标签不代表上游发行版或线上运行史。缺少使用/合同证据时应补证/转人工，不能仅凭退款时间窗口宣布可退款；完整证据样本防止一律转人工通过。

**已有基础：**TypeScript控制面健康骨架、共享身份/命令与file-lab协议、可恢复单机实验账本、受限Docker操作及来源冻结机制。ArtifactStore当前仅内存原语，正式证据存储、数据库Runtime与执行鉴权仍待建设。本轮迁移新增GenAI来源入口及客服HTTP协议/适配器；精确实现、测试及资格范围见[92进度](docs/92-progress-and-session-log.md)和对应报告。最终typecheck/build通过，默认测试100/100、控制面生命周期集成3/3通过。模拟HTTP测试不是真实客服业务证据。

**尚未完成：**真实客服SUT、模型通道与LLM运行、独立工具trace/Observer/屏障、SUP Oracle、Registry激活、LangGraph、Codex Worker、Final、审批及PR/CI闭环。参考源码冻结不是健康SaaS基线，没有生产或业务PASS资格。

- [实施手册](docs/README.md)、[总体计划](docs/00-overall-plan.md)、[系统架构](docs/01-system-architecture.md)和[Verifier详细设计](docs/13-langgraph-verifier.md)。
- [03来源与环境](docs/03-environment-preflight.md)、[05 GenAI客服基线](docs/05-genai-support-baseline.md)是后续真实运行入口。
- [93验收追溯](docs/93-v3-acceptance-coverage.md)、[94数据治理](docs/94-data-flow-and-retention.md)保留企业门槛。

已注册的本地命令：

```sh
pnpm typecheck
pnpm build
pnpm test
pnpm test:integration
pnpm egap:lab status
pnpm egap:lab prepare-source
pnpm dev:control-plane
```

`prepare-source`依当前业务策略核验固定来源，可用`--reference-repository <固定来源本地Git库>`提供受控缓存；它不安装Python依赖、不调用模型、不启动SUT。控制面健康端点只声明骨架就绪。新业务由[活动配置](config/active-business.json)选择，锁为[sut/genai-support/upstream.lock.json](sut/genai-support/upstream.lock.json)，参考副本为`workspaces/reference/`，账本为`artifacts/lab/genai-support/`。43文件参考已导出并复核；模型未调用、SaaS派生SUT未实现。实验身份独立于历史ML/Eventuate。

参考[OpenAI Customer Service Agents Demo](https://github.com/openai/openai-cs-agents-demo)的Agent/tool编排；其航空示例、内存状态与模拟工具不能作为我方SaaS账单、历史政策、持久任务或企业权限实现。源码、许可证、我方扩展与实验变异分别记录。

**协作约定：**本次文档迁移已获用户明确授权；后续修改docs或README仍需明确许可。代码实现按[AGENTS.md](AGENTS.md)补必要中文注释，验证后同步指定GitHub；该同步不代表EGAP自动发布功能已经实现。旧参考项目只读，旧业务源码与证据不改写为GenAI资格。
