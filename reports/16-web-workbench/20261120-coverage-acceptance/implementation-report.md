# 十二项核心材料覆盖与浏览器验收

日期：2026-11-20（Australia/Sydney）。范围：固定历史归档的隔离联调复验。

后续状态：窗口恢复后，已完成桌面 Chrome 的真实 QA 主流程，见 [浏览器验收补充](../20261120-browser-acceptance/implementation-report.md)。本报告保留较早一次 HTTP 联调和浏览器受阻时的原始范围；后续模型产生的 `PARTIAL` 结果也单独保留，没有改写为完整成功。

## 本轮修正

1. 补证表单默认选择服务端真实枚举：`SAVED_PRODUCT_DRAFT`、`DELIVERED_PRIVATE_NOTE`、`DELIVERY_RECEIPT`。`RAW_PROVIDER_RESPONSE` 是显式选择的扩展材料。用户取消选择后，页面重绘不会擅自恢复默认值；测试直接消费服务端模板。
2. `WorkflowStore.acceptResponse()` 与 `withdraw()` 在原有 Case / Task 事务锁内共用 `refreshObligationStatus()`。聚合只统计当前有效、已接受、经过关联核验的核心材料，以精确 `kind + target` 去重；扩展原件不能填补核心缺项。
3. 完成需要同时满足：四个冻结目标的十二项核心关系齐全，以及所有仍有效的请求均已完整响应并被接受。接受 `UNAVAILABLE` 说明不会使缺失原件变为已取得。
4. 撤回请求重新计算任务状态。撤回非必需的缺口请求可能解除阻塞；撤回核心请求会使已完成任务恢复待补证。原响应、人员接受、撤回原因、Bundle、读取记录和消耗成本均保留。
5. 页面允许接手人明确撤回已接受的请求；这不是删除历史接受。已撤回的缺口仍展示为缺口，但不会再错误提示它仍阻塞当前任务。只读身份、已撤回和已取代请求不显示撤回入口。

## 代码阅读顺序

- `apps/review-workbench/public/app.js`：`workflowRequestForm()` 初始化真实材料选项；`workflowRequestPanel()` 展示请求、原件、回执与人员决定；`workflowCommander()` 冻结绑定版本的幂等命令。
- `apps/review-workbench/src/workflow-store.ts`：HTTP 人员命令进入 `human()` 完成身份、归属、版本与 Bundle 校验，再执行接受或撤回，最后重新聚合完成条件。
- `apps/review-workbench/src/ui-journal.test.ts` 与 `workflow-v2.integration.test.ts`：表单默认值、请求提交、真实 PostgreSQL 聚合及回退边界。
- `tools/review-evidence-browser.mjs`：恢复指定隔离 Case，启动 4178 工作台、真实模型 Runtime、MCP 与 Worker；精确限制队列领取范围，不自动作出人员决定。
- `tools/review-evidence-coverage.mjs`：窗口通道不可用时的显式 HTTP 验收工具。人员命令先持久化，重跑复用原命令；该工具不能替代浏览器点击验收。

## 自动验证

TypeScript 类型检查通过。工作台整套测试使用真实隔离 PostgreSQL：54/54 通过、0 跳过，含 15 项 v2 工作流测试与初版 11 项前端 VM 测试。追加撤回入口后，前端 VM 测试 14/14 通过；其中 11 项与整套重叠，本轮共覆盖 57 个不同用例，不将重复执行相加。构建、JavaScript 语法和 diff 空白检查通过。

真实数据库测试已验证：核心 12/12 且 raw 缺口请求有效时仍待补证；撤回 raw 后关联任务完成；再撤回一组核心请求后降为 9/12、恢复待补证；业务语义任务不会因原件齐全自动完成；重复撤回命令返回原回执，不重复推进任务版本。

## 真实 Agent 联调结果

复用隔离 Case `evidence-live-4a4640d4-36d4-4dcb-b849-4429dccb5d35`，候选 `45a0378585b8970fb2860897c9e49792e15a9df0`，历史业务 Run `egaplate20261119a`，归档 SHA-256 为 `e04eccfdf8f25852aa8b98c297e12bccda115f44f9c24bcb7762d167851ab843`。没有重建归档、替换原件或重置旧预算。

| 冻结目标 | 工单版本 | 核心材料 | 本轮模型 / MCP 读取 | 结果 |
| --- | --- | --- | --- | --- |
| `late_usage.before` | `cw:1:23` v1 | 草稿、最终私有备注、交付回执 | 0 / 0，复用前轮 | 3/3 已接受 |
| `late_usage.after` | `cw:1:23` v2 | 同上 | 4 / 3 | 3/3 已接受 |
| `post_claim_usage.before` | `cw:1:24` v1 | 同上 | 4 / 3 | 3/3 已接受 |
| `post_claim_usage.after` | `cw:1:24` v2 | 同上 | 4 / 3 | 3/3 已接受 |

调用链是：HTTP 实验 QA 提交绑定版本的请求 → PostgreSQL outbox → `EvidenceWorker.runOnce()` → 原 Python ReAct Graph → Runtime 模型决策 → `evidence.read_registered` MCP 工具 → 真实读取回执进入后续模型上下文 → 证据响应提案 → 服务端关联核验与 Bundle 合并 → 实验 QA 限定接受。

三个新增 Run 分别为 `d595340e-b04b-49c5-b686-2dffbf304fec`、`0a13800e-7578-4f6e-8f5b-074f8fc2ac1e`、`51dd1c4a-10f2-433d-b676-108333bd3bca`。均正常结束并结算；每个取得 3 条 `MCP_ARCHIVE` 回执，后续模型调用确实看到了这些回执。复核逐项验证了原件 SHA-256、种类、候选、完整目标身份，以及响应引用与回执对应关系；所有业务语义字段仍为 `NOT_EVALUATED`。

实际完成状态经历了两个不同步骤：

1. 接受最后一组核心原件后，核心覆盖 **12/12**，但额外 RAW 请求仍有效且 `UNAVAILABLE`，关联任务保持 **WAITING_EVIDENCE**。
2. 实验 QA 通过绑定当前任务和 Bundle 的命令明确撤回该非必需扩展请求后，关联任务变为 **COMPLETED**。RAW 请求仍保存原响应 ID、摘要、`ACCEPTED` 和 `UNAVAILABLE`；未采集原因仍为 `NOT_CAPTURED_IN_PINNED_ARCHIVE`。语义任务保持 **ASSIGNED / 未接手**，没有被代为批准。

这次撤回只调整当前扩展调查范围；冻结的十二项核心要求没有减少。真实 PG 回归另外验证了撤回核心请求会降为 9/12 并重新打开任务，不能用撤回来绕过核心覆盖。该回退测试使用独立测试夹具，没有破坏上表保留的完成案例。

使用既有本地 `mistral:latest`，本轮新增 **12 次模型调用、9 次 MCP 读取**，输入 71,547 tokens、输出 5,342 tokens。本案累计 **17 次模型调用、12 次读取**，输入 97,272 tokens、输出 7,362 tokens；上限仍为 24 次模型调用、48 次读取，剩余预留为 0。没有外部供应商账单，因此金额为 `null / LOCAL_NO_PROVIDER_BILL`，不把本机资源消耗宣称为免费。

最终又执行了一次复验：复用已有请求、响应和接受状态，加强原件与回执的绑定断言，**新增模型调用 0、MCP 读取 0**。原件 HTTP 读取属于工作台审查读取，不伪计为 Agent 的 MCP 读取。首次执行摘要和命令日志单独保留，复验未覆盖首次成本边界。此前其他 Case 的调试失败历史仍保留，未混入本案准确率或隐藏。

脱敏机器结果见 [validation-summary.json](validation-summary.json)。私有命令、回执、首次与复验摘要位于本地 `artifacts/review-workbench/browser-coverage/<case-id>/`；模型原始记录留在隔离 Runtime 数据库，不提交 Git。

## 浏览器验收的实际边界

本轮通过 Chrome 原生窗口接口选择了专用空白标签并导航到 `127.0.0.1:4178/#/tasks`；随后标签清单显示“登录 · EGAP 审查工作台”。但窗口读取持续返回 `cgWindowNotFound`，扩展接口也出现 `timeoutMs` 错误或 selector 超时。重取窗口和使用同一标签的替代接口均未恢复。已询问用户保持 Chrome 窗口可见。

因此，本轮没有完成登录、默认勾选、提交、读取原件、接受和撤回的完整浏览器点击验收，也没有取得可以支持这些结论的浏览器截图。前端 VM 测试及下述 HTTP 实验不能替代此项；该项保持未完成。

原 4174 本地工作台已使用原配置重启并通过 `/health/live` 检查；隔离验收数据只进入 live-review / live-runtime 两个独立库。重启会使旧内存会话失效，需要用原本地访问码重新登录；数据库中的请求、Bundle 和审计记录保留。

## 验收边界

此轮使用已知归档、既有候选与本地测试 QA 身份，不代表用户实际签署。第一组材料来自上一轮已完成的真实模型联调，其余三组由本轮继续处理。原始 provider 响应未被该历史归档采集；该事实必须保持为缺口。

本轮不重新执行客服业务实验，不计作新候选覆盖或 Final；不计算未知问题发现率、通用准确率；不完成业务语义裁决、多部门会签、企业身份隔离、通用企业证据接入、Codex 修复或发布。`docs/**` 和 `README.md` 未修改。
