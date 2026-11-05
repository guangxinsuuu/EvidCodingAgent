# 00 · 总体计划：企业 GenAI 客服版本发布验证

状态：`GENAI_MIGRATION_IN_PROGRESS`。通用控制面骨架、file-lab 与 Docker 低层适配已实现；本轮迁移来源准备和客服 API 客户端协议。自有 GenAI SUT、真实 LLM/工具调用、独立 SUP Oracle、Verifier 与发布闭环尚未运行。完成事实以[92进度](92-progress-and-session-log.md)及实际报告为准，不把目标架构算成已有系统。

## 1. 最终交付与固定业务

主场景为 **Enterprise GenAI Release Verification & Governed Coding Harness**。被测业务固定为 B2B SaaS 客服 Agent 的续费退款争议工单：查询订阅/账单、检索适用政策、核查使用与合同例外，生成结构化建议和待人工审核的回复草稿。EGAP 根据代码、Prompt、检索、工具与模型配置的实际变化选择高价值实验，用独立证据发现回归，再按授权请求修复和交付。

客服 Agent 是被测产品，LangGraph Verification Agent 是测试规划器。它们不能共享验收写权、隐藏答案或发布授权。首期仅合成企业数据和沙盒敏感动作；无真实退款、客户发送或生产账户修改。Job DONE 只表示处理结果已保存，不代表 Ticket RESOLVED，更不代表 EGAP PASS。

固定示例 CS-1042：2026-10-25 续费，2026-11-02 提交工单。演示规则按续费事件选择十月14天政策；因使用记录与合同例外待核验，预期为 `NEEDS_MORE_EVIDENCE` 和人工复核/补证，不能仅凭14天窗口断言可退款。政策时区、计日、边界及例外优先级必须在06形成明确DRAFT，正式激活需要真实Owner决定。还要有证据充分的符合/不符合条件案例，防止“永远转人工”骗过验收。

首个发布切片是自有实验 Stable V3.4 → Candidate V4.0。V4新增十一月7天政策和Tool Schema v2，同时保留十月事件的历史政策适用性。第一轮固定LLM、Worker与无关Prompt；分别植入 `latest-policy-only` 和 `ticket-date-as-renewal-date` 人工变异。V3.4/V4.0不是上游发行版或真实线上运行史。正确新功能候选无需调用Coder也可成功；回退Stable删除新功能必须失败。

RELEASE_VERIFICATION先接收已有候选；INCIDENT_REPAIR保留事故入口。`repair_policy=DISABLED|ON_VALIDATED_FINDING`与`completion_mode=FINAL_ONLY|DRAFT_PR_CI`独立冻结。DRAFT_PR_CI的终点是精确人审后的自有Fork Draft PR和准确head的可信CI，不包含合并、部署或退款。UNCHANGED候选仍须独立验收，只在完整输入等于任务base且满足需求时进入NO_CHANGE。

## 2. 三个交付层级

| 层级 | 能演示什么 | 必须留下什么 | 不能宣称什么 |
| --- | --- | --- | --- |
| 原生实验台 | 实际模型请求、政策/账单工具、异步草稿、可信观察 | 来源/候选/环境身份、原始工具记录、健康/变异对照、清理回执 | Agent省成本、自动对客质量、生产隔离 |
| Release Verification Pilot | 真实功能候选、变更影响、自适应选测、按权限修复及Final | 选择与省略理由、可信反例、漏报误报、全部成本和人工时间 | 已替代绝大多数QA、任意公司零改造接入 |
| Enterprise / Production | 企业身份、真实系统接入、运营与恢复 | ACL、数据区域/保留、SLO/DR及业务交接证据 | 从本地Demo自动继承生产资格 |

[93验收映射](93-v3-acceptance-coverage.md)与[94数据流向](94-data-flow-and-retention.md)是可核验附件。语义评分、敏感动作、连接器和运行恢复分别有范围，不合并为一个“企业级通过”。

## 3. 为什么按此顺序建设

先明确业务问题与输入输出，再建设真实、可观察的实验台和独立判定；随后接入可恢复Runtime与规划模型。这样一次失败可区分业务错误、模型输出、环境和采集缺口。稳定历史覆盖仅辅助选测与资格核对，不按每个候选全量重测Stable，也不转换成当前候选PASS。

```mermaid
flowchart LR
  A[03-05 来源与真实SUT] --> B[06-09 合同 观察 Oracle 校准]
  B --> C[10-11 Runtime与预算]
  C --> D[12-14 选测 可选修复 Final]
  D --> E[15-17 隔离 人审 Draft PR CI]
  E --> F[18 公平评估]
  F --> G[19-22 企业接入与扩展]
```

13的详细协议现在就设计；真实执行仍依赖05–11。初次05–09使用明确file-lab/calibration-only，不提前虚构PostgreSQL Runtime或ACTIVE合同。

## 4. 每段验收

| Gate | 章节 | 产物 | 通过条件 |
| --- | --- | --- | --- |
| G0 | 03–05 | 固定来源、自有SUT、运行清单、真实smoke | 真实LLM和工具路径可观察，任务可查，资源可精确清理 |
| G1 | 06–09 | SaaS Support Pack、Oracle、健康/变异对照 | 正确候选通过，政策/参数变异失败，坏观察器不能通过 |
| G2 | 10–11 | PostgreSQL事务、Outbox、租约、预算 | 真实DB恢复、去重、取消、UNKNOWN对账及受保护Final预算 |
| G3 | 09、12–14 | ChangeSet、Context/Impact/Hypothesis、Planner、可选修复 | 正确V4零Coding可通过；有反例时按授权修复；不能删新功能逃避验收 |
| G4 | 15–17 | 隔离资格、两类审批边界、发布与CI | 准确候选与批准绑定、撤销窗口和远端UNKNOWN被正确处理 |
| G5 | 18 | 公平发布QA对照及单列消融 | 质量底线不降的情况下，测量全部费用、漏报/误报与人工时间 |

六个实施批次为：范围与详细协议 → 来源/入口 → 单条真实SUT链 → 业务验证包 → Runtime与针对性Verifier → 修复交付。不因换业务跳过治理资格。

## 5. 首期业务与非目标

SUP-01政策适用性、SUP-02租户隔离、SUP-03敏感动作授权、SUP-04依据充分、SUP-05工单revision、SUP-06结果完整性、SUP-07补证/转人工、SUP-08工具参数。优先01/08/07与最小02/03/06；05及更广恢复按承诺准入。目录不等于全部已实现或已激活；发布范围承诺的规则缺能力必须阻断。

Oracle分确定性业务判定、执行/trace证据核验、语义质量报告。首期客服文本是待审草稿；语义grader不覆盖硬规则FAIL/UNKNOWN，也不直接签发自动对客资格。是否把语义评分变成正式Gate，需独立校准、阈值和不确定策略。

OpenAI航空客服Demo仅作固定来源和工作流参考，不原生提供SaaS账单、历史政策RAG、持久Job或企业退款。自有SUT明确记录新增业务。旧ML与Eventuate源码、账本和证据保留历史；其编译或mock通过不变成GenAI资格。

## 6. 学习与实施方法

每轮只完成可检查的一段：阅读当前章节和真实进度 → 明确输入/前提/退出条件 → 实现并补必要中文注释、真实调用链与不变量 → 做有意义正反验证 → 保存原始证据及简明报告 → 获得本轮文档许可时更新相应章节。文档中的未来命令须实现注册后才可执行。

业务知识和阈值外置；Agent内部放可替换的分析与策略；工具采用受控注册接口；CI/EGAP强制权限和可信放行。首期不引入任意插件安装、自由生成测试执行、Kubernetes或完整客服UI。新场景的知识库属于SUT业务输入，EGAP企业知识Connector属于另一条第20章权限路径。

## 7. 完成标准与下一步

- [ ] 来源、派生业务、候选、观察、Oracle和证据身份可追踪。
- [ ] CS-1042及证据充分的正反例有明确规则，日期与异常语义未靠模型补造。
- [ ] 实际执行与计划、mock/replay与live、草稿与业务动作分开记录。
- [ ] Verifier可说明为什么测、为何省略、什么证据改变策略；未知影响不当作低风险。
- [ ] 全部SUT/Verifier/grader/Coder调用与环境/重试计费，无隐藏免费模型调用。
- [ ] 旧历史未覆盖；没有把未实现的Runtime、Oracle、Agent或CI写成已完成。

下一步先完成03来源/依赖与受控模型通道预检，再按05建设一条真实工单链。第06章DRAFT与第13章对象协议可以并行细化；不先扩成完整SaaS产品。
