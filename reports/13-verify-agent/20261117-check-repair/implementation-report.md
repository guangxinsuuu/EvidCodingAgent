# Verify Agent：具体缺项反馈、有界修正与独立留出验证

日期：2026-11-17（Australia/Sydney）。本报告记录实现与实测事实；不修改冻结的 docs/README，不授予业务执行或发布权限。

## 本轮实现

CHECK 不再因为“包含目标和观察文字”就直接结束规划。通过身份、材料和合同读取校验后，Runtime 会检查结构化 `check_spec`，一次返回可发现的具体缺项。模型可以在固定额度内修正；原提案不会被新提案覆盖。

调用关系：

```text
Python LangGraph.load → decide → Runtime.decide → 本地 ModelPort
  → 持久化原始响应和 token 用量
  → LangGraph.register → Runtime.submit → assessCheckQuality
      ├─ NEEDS_REVISION → 保存缺项及修正链 → 下一次受预算限制的 decide
      ├─ STRUCTURALLY_COMPLETE → PROPOSED_ONLY / PLANNED
      └─ 额度用尽 → STOPPED / CHECK_REPAIR_LIMIT
```

`STRUCTURALLY_COMPLETE` 仍带有 `semantic_correctness=NOT_ESTABLISHED`、`dispatch_qualified=false`。它不证明输入可执行、断言符合合同、实验有区分力或业务通过；当前候选 coverage 仍为空，Final 资格仍为 false。

## 具体检查内容

| 规格 | 检查内容 | 典型缺项代码 |
| --- | --- | --- |
| 阶段 | `stage_ref` 必须属于选择的登记场景 | `STAGE_REQUIRED`、`STAGE_NOT_IN_SCENARIO` |
| 输入 | 明确参数与取值，名字唯一，无明显占位内容 | `INPUTS_REQUIRED`、`INPUT_VALUE_REQUIRED` |
| 观察 | 唯一观察 ID、登记来源、具体字段 | `OBSERVATIONS_REQUIRED`、`OBSERVATION_FIELD_REQUIRED` |
| 来源 | 区分业务/身份/产品/模型执行来源与规则、代码、历史资料 | `SOURCE_NOT_OBSERVABLE` |
| 预期 | 观察引用、关系算子、具体值及适用合同绑定 | `EXPECTATIONS_REQUIRED`、`EXPECTATION_REQUIREMENT_NOT_BOUND` |
| 对照 | 登记场景/阶段、同名因素的不同取值、观察和 SAME/DIFFERENT 理由 | `CONTROL_REQUIRED`、`CONTROL_NOT_DISTINGUISHABLE` |

例如，没有 `check_spec` 的旧 CHECK 会同时收到规格、阶段、输入、观察、预期、对照六类缺项，而不是只得到一句“计划不够详细”。旧 `observation_requirements` 直接填规则 ID 也会列入质量问题。

共享接收协议保留兼容性：`check_spec` 可缺省，内部主要栏目可部分填写，这些情况进入质量反馈；错误类型、未知字段和不完整的嵌套对象仍由严格 JSON Schema 拒绝。修正阶段的本地模型生成 Schema 提示完整结构，但不能替代接收端独立校验。

既有 YAML 继续提供版本化的场景、阶段、规则、能力和来源身份。本轮只扩展只读装配投影，将来源导航提供给规划器，没有改 YAML、合同内容或 `dispatch_enabled`。新装配包可附带来源 `allowed_fields` 提示；旧冻结包没有该提示也可使用。

## 修正预算与恢复

- 每个任务 `plan_repairs` 默认为 1，允许 0–2，必须小于模型总调用额度。
- 修正额度从 `model_calls` 总额内预留，不是额外免费调用。探索不能挪用预留额度。
- 本次真实评估统一为总调用 6、材料读取至多 3、修正至多 2：首次计划前至多 4 次模型调用，其后至多 2 次修正调用。
- 首次质量拒绝创建任务级修正链。后续每次模型调用都消耗修正机会，包括 READ、无效 JSON 和未解决问题的 CHECK；换 proposal ID 不能重置额度。
- `check_repair` 与 `latest_feedback` 分开持久化，读取回执不能抹掉缺项。最后一次修正仍不完整则停止，不记成规划成功。
- 并发重放和重启恢复使用 PostgreSQL 行锁及既有调用/操作身份，不重复扣费、调用或覆盖旧提案。
- 取消与 UNKNOWN 以任务终态控制派发；迟到回执只留账，不能恢复任务。

修正计数是调用预留记账，不属于决策材料身份。它从 `context_ref` 的依据视图中排除，剩余额度另由 `execution_limits` 提供，避免预留本次调用就使其响应身份过期。

上下文只投影一份完整原计划和质量缺项。合法但过大的提案仍可能无法装入上下文；此时持久化 `BLOCKED / CONTEXT_TOO_LARGE`，保留所有材料，不消耗未派发的调用或修正次数，不陷入反复失败的 ACTIVE 状态。

## 原提案和成本如何留存

PostgreSQL 和每个 run 的 `full-evidence.json` 保留：

1. 实际发送的 `request_text`、模型原始响应、解析后的每个提案及上下文身份。
2. 每次操作的原提案、质量缺项、修正链 root/previous/attempt 和最终回执。
3. 每次调用的 prompt/output token、预算池与预留事件；失败与重复错误也计入。
4. 固定的 B/C Git 身份、目录身份、Runtime 模块摘要、Prompt/协议/模型摘要。

本次使用已安装的本地 `mistral:latest`；没有云提供商账单，金额字段为 `null`、依据为 `LOCAL_NO_PROVIDER_BILL`。这不代表计算成本为零，也没有估造金额。原始实验、连接配置和凭据仅留本机；GitHub 只同步实现、测试及脱敏报告。

## 实现阅读位置

| 文件 | 职责 |
| --- | --- |
| `packages/contracts/src/verify.ts` 与 `protocols/verify-agent/v1/decision-proposal.schema.json` | `CheckSpec` 类型及跨语言共享协议 |
| `packages/verify-runtime/src/check-quality.ts` | 无模型、无副作用的具体缺项检查 |
| `packages/verify-runtime/src/runtime.ts` | 质量准入、修正链、预算预留、持久化和终态 |
| `packages/verify-runtime/src/context.ts` | 来源导航、单份修正上下文、生成 Schema 与稳定身份 |
| `services/verify-agent/planner-prompt.txt` | 解释如何形成阶段、输入、预期和对照，以及如何消费反馈 |
| `services/verify-agent/src/verify_agent/graph.py` | 继续既有 Runtime 循环，报告修正状态 |
| `tools/verify-agent-live.py` | 本地真实模型运行器，接受有界修正参数 |
| `tools/verify-agent-holdout.py` | 独立准备新版本变化、可判别见证和隔离评分材料 |

## 自动化验证

- `pnpm typecheck`：通过。
- `pnpm test`：325 项通过，零失败、零跳过；使用专用本机 PostgreSQL。
- Python Verify Agent：33 项通过。
- 目录装配与校验：89 项通过。
- 留出准备与隔离：5 项通过。
- 协议共享 corpus：74 个样本，由 TypeScript、Python 及模型 Schema 测试消费。

新增测试包括原提案与 token 不丢失、并发重放、重启、0/1/2 次修正、READ/INVALID 不能重置额度、取消、UNKNOWN、上下文身份稳定、355 条缺项仍可修正，以及超限时的持久阻塞。数据库测试的 Provider 是受控 stub；真实模型能力由下述独立运行单独报告，二者不混算。

## 独立评测约束

两份新变化由独立线程先准备，使用真实仓库源码衍生的合成 B/C Git 对象；不是完整产品 PR，也不是客服系统端到端执行。两份任务使用相同中性 goal。评估参考答案、可接受方案及纯函数见证与模型输入分别存放。

- 留出冻结时间：2026-11-16T23:19:30.848669+00:00。
- 留出冻结摘要：`60d9abcabeafeb3d270027b8fbedef3396075564befed00a587e06a6eb69dd34`。
- 实现冻结时间：2026-11-16T23:27:34.443992+00:00。
- 实现冻结摘要：`6b45be62653d4cf3781bf8656cf80de1f9ff870a8dbc38cce67089e085b299d9`，覆盖 85 个文件。
- 主实现线程在冻结后才看到新案例的具体变更；冻结后不修改实现、Prompt、模型或参数，每个留出案例只运行一次。

预登记评分为五维各 0–2 分：来源依据、可判别输入、预期关系、观察与对照、能力/阶段资格。总分至少 8 且可判别输入和预期关系均为 2，才算语义规划合格；Runtime 接受和语义合格分别统计。

这些案例在本轮公开后应转为回归集，后续改进必须再准备新的留出变化，不能继续把它们称作未见样本。仅两份合成变化不能证明生产环境泛化能力。

实际逐案结果、调用成本与独立评分见同目录 `implementation-summary.json` 和 `holdout-evaluation.json`。

## 真实模型结果

**反馈、有限修正和留账机制已跑通；当前本地 Mistral 配置没有产出合格的业务测试计划，留出合格为 0/2。** 结构缺项减少不代表理解了变化，也不等价于测试质量提高。

| 运行 | 原 CHECK → 修正 1 → 修正 2 的缺项数 | 独立语义评分 | 最终结果 |
| --- | --- | --- | --- |
| 开发：既有投影变化 | 2 → 2 → 2 | 非留出，不纳入合格率 | STOPPED / CHECK_REPAIR_LIMIT |
| 留出 A：付款币种相等检查被删除 | 4 → 4 → 4 | 1 → 1 → 1 / 10 | STOPPED / CHECK_REPAIR_LIMIT |
| 留出 B：稳定事实投影错误忽略所有 `*_at` 字段 | 4 → 3 → 1 | 1 → 1 → 1 / 10 | STOPPED / CHECK_REPAIR_LIMIT |

A 最初正确读取 R4，但没有提出 EUR/USD 不匹配及同币种对照，后来转向 R1。它将规则文档当作运行观察，预期值为空，且引用了提案未绑定的要求。四项问题在两次修正后仍存在。

B 读取了候选和基线，却只读取 R1，没有围绕真实业务时点 `paid_at` 与观察元数据 `observed_at` 设计区分实验。修正修改了输入与对照标签、移除了不合适的观察来源，最后仍是宽泛的过程字段和空预期。缺项数下降，语义判别力没有建立。

两个留出都没有被 Runtime 接受成 PLANNED，没有生成业务 PASS。完整请求显示反馈确实送入模型；可以证明模型获得了机会，不能声称模型已经能够稳定消费反馈和正确自我修复。

开发运行在冻结前启动，使用的构建尚未包含反馈去重和持久化上下文超限阻塞；该差异单独标记，不把它冒充最终构建的留出结果。两次留出使用相同的最终冻结 Runtime/Prompt/模型/参数；评估后重新核对 85 个文件摘要，全部未变。

| 运行 | 模型调用 | 输入 token | 输出 token |
| --- | --- | --- | --- |
| 开发 | 6 | 51,219 | 3,185 |
| 留出 A | 6 | 48,074 | 2,431 |
| 留出 B | 6 | 50,922 | 2,887 |
| 合计 | 18 | 150,215 | 8,503 |

总计 9 次材料读取、9 份 CHECK、6 次修正调用。失败提案全部计入。仅留出修正部分为 4 次调用、39,058 输入 token、2,586 输出 token，没有用成功样本掩盖这部分消耗。

原始证据目录：

- `artifacts/verify-agent/20261117-repair-development-01/`
- `artifacts/verify-agent/20261117-holdout-a-run-01/`
- `artifacts/verify-agent/20261117-holdout-b-run-01/`
- `artifacts/verify-agent/20261117-holdout-prepared/`：输入与评分材料隔离封存。
- `artifacts/verify-agent/20261117-validation/`：自动化日志、实现冻结摘要与资源清理记录。

每个运行目录保留 `full-evidence.json`、报告、最终投影及自有进程清理记录。专用 PostgreSQL 已恢复为原先停止状态，数据库和证据保留；未改动其他业务容器。

## 后续改进应解决的问题

本轮没有继续修改提示词去追逐这两份留出答案。下一轮应先评估“是否正确选择合同、是否把 diff 转成可判别输入、是否真正修改被指出的问题”，再用新的留出集合比较改进效果。

还需要把 `check_spec` 与真实能力的输入、输出字段和执行前提绑定。目前字段和多数取值仍是规划字符串，来源字段提示不等于可执行协议，结构检查也不能证明场景与能力组合能够运行。现有机制支持拒绝与有限修正；业务语义审核、实际实验派发、可信 Finding 和 Final 仍未由本轮完成。
