# GenAI Support 业务迁移：本轮实现与验证

日期：2026-11-05。用户授权按已审阅的迁移方案更新 docs 及当前实现；此次授权不改变今后文档修改仍需许可的约定。

## 1. 本轮结果及范围

主业务切换为 **Enterprise GenAI Release Verification & Governed Coding Harness**，首期被测场景为 B2B SaaS 续费退款争议的处理建议与待审核回复草稿。

本轮完成实施文档迁移、固定公开参考来源、独立实验账本、业务入口切换、共享 DRAFT API Schema、客服 HTTP 客户端及模拟协议检查。没有实现或启动自有 GenAI Support SUT，没有调用模型、发送客户消息、执行退款，也没有运行业务 Oracle、LangGraph、Codex Worker、Final 或自动发布链。

当前阶段为 **来源与接口准备**。OpenAI 航空客服 Demo 是编排参考，不是已完成的 SaaS 基线。源码完整性和 mock 测试不构成业务通过证据。

## 2. 当前代码改动与阅读顺序

| 路径 | 实际实现 |
| --- | --- |
| `config/active-business.json` | 选择 genai-support、参考来源、独立账本和新适配器；SUT/runtime 保持未实现/未运行 |
| `tools/environment-host/src/business-profile.ts` | 限定已注册业务与路径；校验来源 commit、build revision、business revision 及完整锁摘要 |
| `tools/environment-host/src/cli.ts` | `egap:lab` 使用新账本，调来源准备器；已存在副本离线复核，不运行上游 |
| `tools/environment-host/src/source-export-core.ts` | 抽出双业务共享导出核心；逐文件 SHA/Git blob/mode 校验、原子提交、拒绝额外文件及符号链接 |
| `tools/environment-host/src/genai-source.ts` | 固定 GenAI 策略摘要与来源锁绑定；输出参考资格，不伪造健康基线 |
| `tools/environment-host/src/ml-source-core.ts` | 保留旧 ML 门面、错误命名与锁字节；旧业务不再是默认入口 |
| `packages/contracts/src/support.ts` | DRAFT v1 请求、受理和 Job JSON Schema；严格形状、状态与工单版本关联；独立冻结返回值 |
| `packages/genai-support-adapter/src/api.ts` | 固定本机路由、每次环境 guard、内存会话、受限响应、无重定向、无 POST 自动重试、脱敏观察 |
| `packages/genai-support-adapter/src/fixtures.ts` | 只发送合成工单引用与 revision；不向被测系统发送期望政策或答案 |
| `packages/genai-support-adapter/src/smoke.ts` | health、有效工单、非法 revision、未知 Job 四项固定接口检查；不是针对变更的 Agent 选测 |
| `tsconfig*.json`、`pnpm-lock.yaml` | 纳入新 workspace；离线安装只增加本地包连接，没有新增外部依赖 |

主要调用链：

```text
pnpm egap:lab prepare-source/status
  → loadActiveBusiness
  → GenAI LabLedger + assertActiveSourceBindings
  → prepareGenAiReferenceSource
  → prepareSourceWithPolicy

未来受控 SMOKE action（尚未接通）
  → runSupportSmoke
  → GenaiSupportApi
  → environment guard → HTTP → shared Schema → evidenceSink
```

请求为 `{schema_version, ticket_id, ticket_revision}`，无 tenant、日期或期望政策。服务端将来必须从认证上下文派生 Tenant；客户端比对响应 Tenant 不代表服务端租户隔离已经实现。

`DONE` 要求形状合法且同工单/revision 的草稿。Schema 不判断引用真实性、退款资格、回复语义或实际持久性。测试故意返回形状正确但政策错误的草稿，确认 HTTP smoke 不冒充业务 Oracle。

负例收到 202 时立即记录 `UNEXPECTED_ACCEPTANCE`，该检查 FAIL；即使响应正文回显非法 revision，也不能降格为只有不确定结果。可能创建的任务身份保持 UNKNOWN，停止后续请求并交上层对账，不重发 POST。

## 3. 真实参考来源与实验身份

| 字段 | 实测值 |
| --- | --- |
| repository | https://github.com/openai/openai-cs-agents-demo |
| commit | `bd7bfca0f5abf50529370814c3e7c88542011925` |
| tree | `2a42aef137e25a239b5b5ed6ad90f21e81d057e7` |
| tracked files | 43 |
| license | MIT，原 LICENSE 保存在参考副本 |
| policy SHA256 | `4a1a93a904a93df4ea11b1bed2824e59c3b862a936303c226467453ebac63011` |
| source lock SHA256 | `ff404d353d0c9b1067fd31a6cb9efc62124cf95af8568fc833d77a0a6bf3f27f` |
| source role | `NON_SAAS_DEMO_REFERENCE` |
| qualification | `SOURCE_REFERENCE_ONLY_NOT_RUNTIME_VALIDATED` |
| dependency / model | `NOT_RESOLVED` / `NOT_CALLED` |

来源锁为 `sut/genai-support/upstream.lock.json`，原样副本在忽略目录 `sut/genai-support/workspaces/reference/`。只读取 Git 对象、导出及核验，没有执行上游安装/构建脚本。

在 `artifacts/lab/genai-support/` 实际完成来源准备和同一实验的复核：1 个 experiment、2 个 attempt、journal sequence=3、0 个 environment、0 个 action。`pnpm egap:lab status` 已真实执行，返回新业务及上述记录。原始本地账本含主机运行身份，不纳入 Git 提交。

旧 ML 的27个文件再次离线复核为 `VERIFIED_EXISTING`，来源锁 SHA256 仍为 `8ac054f6a0ed27d842c04a5f55a551dc65d99f8a5cc8ccd7a457ed15b388c91f`。旧来源策略、锁、Eventuate 目录和历史证据未重写，不能转为 GenAI 资格。

## 4. 文档中的实质调整

- 主线改为工单、账单、历史政策、工具参数与待审草稿；05改名 `05-genai-support-baseline.md`。
- SUP-01…08、对应检查与范围、缺证据时补证/转人工、信息充分时不能一律转人工。
- 自有 Stable、正确 V4、latest-policy-only 与 wrong-date 两种独立人工变异；不得回退旧版删除新需求。
- 独立 Tool/LLM 网关、Observer 和结构化证据；SDK 自报 trace 仅辅助。
- GenAI 候选身份与验证侧合同/Oracle身份分开；远端 model ID 不等于冻结权重。
- 第13章补领域上下文、影响映射、风险假设、选测、成本/无进展、模型提案协议及结果驱动分支。
- 新结果协议拟采用 `CANDIDATE_DEFECT` 和 `candidate_asset_kind`；旧 CODE_DEFECT 保持历史版本，实际正式 DTO 尚未实现。
- SUT 退款/沟通批准与 EGAP 发布批准分域；权限、预算池、UNKNOWN、撤销与独立 Final 原则保留。
- SUT 模型、Verifier、grader、Coder、环境及重试全部计费；语义评价先单独报告，公平策略对照与业务上下文消融分开。
- 保留旧决策/进度历史；验收映射与数据流转按当前范围重新登记。

## 5. 验证结果与限制

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `pnpm install --offline --ignore-scripts` | 通过 | workspace接入，未下载新依赖或运行安装脚本 |
| `pnpm typecheck` | 通过 | 最终代码类型检查 |
| `pnpm test`（含build） | 100/100 | 含 shared Support 协议4项、新HTTP adapter15项，以及新旧来源/通用机制回归 |
| `pnpm test:integration` | 3/3 | 控制面真实本机HTTP、配置、进程退出和端口释放；不是GenAI端到端 |
| 实际来源准备/复核及CLI status | 通过 | 43文件参考来源及独立账本；无环境和业务动作 |
| 旧ML副本离线复验 | 通过 | 27文件及原锁摘要保持不变 |
| 文档结构与追溯 | 通过 | 检查32份说明/报告、259处本地链接及围栏；原56项L1/L2/L3的ID与名称保持一致 |
| `git diff --check` | 通过 | 无空白格式错误 |
| 真实客服SUT / LLM / SUP Oracle | NOT_RUN | 后续实施条件，不从本表推导业务通过 |

首次在默认沙盒执行新协议/HTTP测试时，4项纯协议测试通过，14项因 `listen EPERM 127.0.0.1` 未能建立模拟服务器。取得本机端口执行权限后，原18项通过；审查补充负例202回归后，全量达到100项。初次环境失败记录和后续输出分别保存，没有删除失败来制造一次通过。

本地原始证据位于 `artifacts/evidence/migration/20261105-genai-support/`，包括 initial-adapter-tests.json、adapter-tests.output.txt、full-tests.output.txt、final-tests.output.txt、integration-tests.output.txt、typecheck-final.output.txt、source-prepare.json、source-reverify.json、legacy-ml-reverification.json 与 lab-status.output.txt。该目录不推送GitHub；本报告只保留必要摘要。

文档核验保存为 docs-validation.json。初版核验器只识别纯数字L1/L3后缀，遗漏L2-R/L2-S；修正解析后完整核对56项，未为迎合计数修改历史目录。初版输出另存，供追溯检查方法。

## 6. 下一实施切片

按03/05核对 Python/模型 SDK/镜像及受控模型访问；在自有 `services/genai-support-sut/` 建立合成账单、政策和工单版本，落地持久 Job/outbox、Worker、受控工具与草稿保存。先用协议测试验证基础路径，再在明确模型、合成输入和调用预算下执行真实模型链路。

同时按06–08构造政策适用性与工具参数的独立 Oracle。必须先取得健康、目标变异及坏观察对照，再进入13的确定性规划闭环；真实模型、企业规则批准、生产隔离、语义质量资格和自动发布均仍待完成。
