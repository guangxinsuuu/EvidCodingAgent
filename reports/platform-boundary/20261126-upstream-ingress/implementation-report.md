# 虚拟上游多来源接入与 Agent 读取联调

日期：2026-11-26。用户已明确选择“先虚拟接入”。本轮完成固定 Jira 修订版、独立 QA 契约、服务维护者接口契约，以及模拟 Project 1 三件交接材料的装配和读取联调。未修改 docs 或 README。

## 1. 本轮实际结果

三个独立来源实例通过真实 HTTP 返回七份固定版本材料。真实 PostgreSQL、TypeScript Runtime 和 Python LangGraph 完成了 `UPDATE_DIRECTION → 7 次 READ → STOP`。模型端使用明确标记的协议桩，因此验证的是调用协议、材料关联、权限、持久化和恢复，不是模型自主调查质量。

| 项目 | 已确认结果 |
| --- | --- |
| 最终 Run | `ff6f5f34-20ab-4d12-a307-9df862ac80e2`，STOPPED |
| 实际操作 | 1 次方向更新、7 次读取、1 次停止，均 COMPLETED |
| 账本 | 9 条模型协议调用已完成并应用；9 条 MODEL、7 条 READ outbox 均 DONE |
| 重启恢复 | 关闭并重新创建 Runtime/HTTP Server，再启动新 Graph 进程；调用数与操作数不增加 |
| 来源权限 | ACL 服务不可用、准确诊断对象撤权时，任务投影、已缓存 READ 结果、模型决策入口均拒绝；检查后恢复权限 |
| 角色边界 | product-reader、outsider-reader、qa-publisher 均不能读取诊断对象 |
| 输入越权 | 上游自带修复授权、其他候选的 handoff 均在准入被拒 |
| 模型上下文 | 最大请求 97,502 字节，保留原有 100,000 字节上限；已读正文及摘要逐份核对 |
| 成本与资格 | 9 次协议桩响应，0 次真实 LLM 调用，0 次业务写入，覆盖为空，Final=false |

权限试验会修改本实验来源的 ACL；“0 次业务写入”指没有修改产品、客服、计费和商业业务对象，不表示本轮没有来源发布或控制操作。

## 2. 七份材料分别是谁提供的

| 材料 | 发布身份 | 当前用途与限制 |
| --- | --- | --- |
| Jira `EGAPLAB-100` revision 2 | `lab:requirements-publisher` | 发布任务、NEW/PRESERVE 要求、负责人和非目标；要求继续保持 PENDING |
| `rules:refund-rules` / lab-v1-extracted | `lab:business-publisher` | 适用业务规则原件；标为 PENDING_USER_REVIEW，没有成为公司批准 |
| `acceptance:phase-review-contract` / phase-v1 | `lab:qa-publisher` | 描述已有归档检查器的输入、阶段、输出和失败语义；读取契约不获得执行资格 |
| `providers:phase-platforms` / provider-v1 | `lab:service-owner-publisher` | 声明平台接口、角色、对象范围、返回字段和失败语义；`runtime_dispatch_authorized=false` |
| `diagnostics:INV-LAB-100` / 1 | `lab:investigation-publisher` | 模拟上游诊断：候选、观察范围、未确认假设和缺项；不继承根因结论 |
| `diagnostics:INV-LAB-100-OBSERVATION` / 1 | 同上 | 已有真实本地归档的指纹和索引；`REFERENCE_ONLY_NOT_INGESTED`，没有把指针当已读原件 |
| `diagnostics:INV-LAB-100-HANDOFF` / 1 | 同上 | 关联诊断、证据、建议调查路径、验证问题；不含修复或发布授权 |

这不是实际 AWS Project 1 的部署，也没有接入真实 Jira/Confluence。Project 1 三件材料为显式模拟输入；其中归档指纹引用已执行的本地实验。OpenSpec 状态为 `HANDOFF_INPUT_NOT_GENERATED_OPENSPEC_CHANGE`，没有声称运行过 OpenSpec CLI。

本任务比较基线是 SUT `3d9bfb18275998426571d844f89cc4ed87b5ec6f`，候选为 `76076dd17368a8cf5627e45758db25300b2a2bd2`。材料来自独立产品仓库的真实 diff；此比较基线不因此取得“生产稳定版本”资格。

## 3. 独立身份与发布版本

| 独立仓库 | 固定提交 |
| --- | --- |
| EvidEnterpriseSources | `e59dfd7c2b5fb5d91101b73b386f870774a2304b`；本轮增加新发布角色、Project 1 材料与 Jira revision 2 |
| EvidBusinessPlatforms | `171aeb389d5e4e04a47a09bfb3eb2efcd879451f`；本轮发布 Provider Contract 与来源清单 |
| EvidAcceptanceAssets | `be8b133d665ac1d7be93a8dec2b7eb203657aca6`，本轮未修改 |
| EvidBusinessRules | `3e49cb1559242bea1f93ced0a5cc4aeb9ea674f5`，本轮未修改 |
| EvidSupportSUT | `76076dd17368a8cf5627e45758db25300b2a2bd2`，本轮未修改 |

Jira/规则沿用 `evid-sources-6b1f4995a5dc`，QA 沿用 `evid-sources-906d8c1857bb`。新诊断/Provider 来源实例为 `evid-sources-35ea9faf3b4e`，服务代码固定 `f6274d7bfbe6f0798b3ce3cc4c8f9999ce193c69`，镜像为 `sha256:c71376ca1152d95b0607c4aa03df6ad11befa5f75daab0f23b1576ca29678b8d`。

来源服务代码版本与发布材料版本分别登记：服务运行在 f6274d7，Jira revision 2 从 e59dfd7 发布到原 Jira 实例。Provider 声明的实现依据固定为平台 a9ef2b5 及三个实现文件摘要；171aeb3 的契约发布不会更改已经运行的平台镜像。

## 4. 入口、函数与数据传递

```text
独立维护者 Git 原件
  → host.publish（固定对象/版本、发布账本、历史快照）
  → 独立来源 HTTP / 对象 ACL
  → assemble.normalize_issue + attach_sources
  → 冻结 ReleaseBundle（独立 SUT B/C、真实 diff、来源身份）
  → Runtime.acceptRun / validateSourceBindings / validateUpstreamInputs
  → SourceReaderSet 按准确 instance_id 使用专有读取配置
  → Graph → Runtime.decide → 模型协议 → READ
  → 来源重新授权 → 原片段与摘要核对 → PostgreSQL 回执
  → contextView → 主图下一轮
```

| 文件 | 实现职责 |
| --- | --- |
| `config/upstream-publications.v1.json` | 固定四个新增来源对象、版本、仓库、路径、发布者、可读角色；任务不能自带 URL |
| `config/upstream-review-request.v1.json` | 对同一 Jira 对象追加 revision 2，保留 revision 1 |
| `tools/source-platform/host.py` | `publication_objects` 限制追加目录只能针对已登记对象/发布者/仓库；`publish` 保存原状态内容寻址快照、核对目录摘要并复用发布幂等身份 |
| `tools/source-platform/assemble.py` | `normalize_issue` 保持要求 PENDING；`attach_sources` 取得真实原件、核对发布者与候选，将来源作为独立材料加入；不跟随诊断指针下载原件 |
| `packages/verify-runtime/src/source-authority.ts` | `SourceReaderSet` 支持最多四个独立实例，准确路由凭据；必需来源任一拒绝即停止使用旧包；`SimulatedSourceReader` 每次 HTTP 核对版本、原文摘要、ACL 和片段 |
| `packages/verify-runtime/src/upstream-inputs.ts` | 核对诊断/证据索引/handoff 三件关联、准确候选、范围、引用、需求和权限限制；Provider 是声明接口，QA 是归档复核契约，均不授予执行能力 |
| `packages/verify-runtime/src/main.ts` | 受信私有启动配置支持 `source_reader_configs`；与原单来源配置互斥；Graph 不拿来源 token |
| `packages/verify-runtime/src/runtime.ts` | 冻结新增模块及三个补充目录摘要；代码或配置变更后不能假装同一 Runtime 恢复旧 Run |
| `packages/verify-runtime/src/context.ts` | 已读正文只保留一份，重复授权审计数据使用摘要和原回执引用；数据库仍保留完整原件，历史摘要不充当新授权 |
| `packages/verify-runtime/src/review-profile.ts` | 明示上游假设、声明接口与原件的权限边界；原生 Schema 共享相同字段并只保留可达定义，不放宽服务端协议 |
| `tools/verify-upstream-ingress.mjs` | 真实来源/PG/Graph 联调、实际请求完整性检查、进程重建、撤权/故障及越权输入验证；使用明确标记的模型协议桩 |

目前仍使用 `Graph → RuntimePort → 来源 HTTP 适配器`。本轮没有新增 MCP Server，也没有把声明中的 HTTP 方法、路径或建议修复路径自动注册成可执行工具。

## 5. 联调遇到的问题及处理

1. `upstream20261126a1`：向相同 Jira request/revision 提交扩展后的 Bundle，准入正确拒绝 `REVIEW_REQUEST_REVISION_CONFLICT`。随后发布明确的 revision 2；没有删除旧请求或放宽冲突检查。
2. `upstream20261126a2`，Run `e6da29e1-0cf3-41cc-841b-01b89754e177`：完成三次协议调用、两次 READ 后，下一请求 108,253 字节，Runtime 持久化 `BLOCKED / CONTEXT_TOO_LARGE`；未派发第四次模型调用。
3. 修正重复上下文和 Schema 表示后，使用完全相同的 revision 2 Bundle 创建 `upstream20261126a3`。新旧 Runtime 身份不同，没有用新代码续跑旧 Run；旧失败回执保留。
4. 最终九次请求均在原上限内。联调对每个实际模型请求里的已读正文、行范围和片段 SHA 逐份检查，完整授权回执仍在 PG。不是截断业务事实来换取通过。

97,502 字节仍接近当前上限。本次只能证明这份七来源任务包可容纳；更大任务仍会被拒。这里没有实现通用检索、模型感知的 token 预算或任意长度证据压缩。Schema 语义由 AJV 和服务端协议测试验证，本轮没有重新评价真实模型对新 Schema 的生成表现。

导出审计摘要时另有一次 UUID 序列化中断；保留 partial 文件后，以字符串表示 UUID 重新导出。它发生在业务联调完成之后，没有改变任何 Run、来源原件或操作结果。

## 6. 验证与证据

- `pnpm build` 成功。
- Verify Runtime：221 项通过，0 失败、0 跳过，启用真实 PostgreSQL。
- 来源 Python 适配器：8 项通过；Python Agent：38 项通过。
- 实际多来源联调：10 组检查通过。统计中的 9 次响应是固定模型协议，不计为真实模型调用或 Agent 选测准确率。
- 独立来源新增发布角色测试、Provider 声明与访问边界测试已在各自仓库实施；本报告的 267 项主仓库回归数不重复包含它们。

`evidence-index.json` 登记最终 Run、七份来源的固定身份、原件摘要、测试日志及前两次失败记录。来源部署/发布状态复制为本次私有快照，避免以后发布使旧索引失效。凭据、Bundle 正文、模型请求、业务原件只留在 ignored artifacts；公开报告不携带 token 或 DSN。

## 7. 六步交付核对与后续边界

这六步的首版本地模拟范围已经有实现和运行证据。下表引用的是不同阶段的实际记录，不将它们累加为一次新的候选通过证明。

| 六步任务 | 已交付依据 |
| --- | --- |
| 1. 产品仓库、镜像、部署与 EGAP 分开 | 独立 EvidSupportSUT；`reports/03-product-boundary/20261126` 及后续平台/模型接入报告 |
| 2. 规则与独立 QA 资产、发布身份和谱系分开 | EvidBusinessRules、EvidAcceptanceAssets；`reports/source-platform/20261126-virtual-ingress`、`reports/platform-boundary/20261126-published-phase-qa` |
| 3. 原生平台与准备/产品/观察权限分开 | 独立 Chatwoot/Lago/Keycloak 与商业事实服务；`20261126-native-separation`、`20261126-commercial-source` |
| 4. Jira 与规则外部入口 | 已按用户授权使用独立模拟 HTTP 来源；本轮支持明确 revision 2，权限每次重验 |
| 5. 实验绑定候选镜像、对象、原生回执 | `20261126-independent-phase-execution`：真实阶段实验、中断恢复、112 份原件关联；本轮没有重跑业务实验 |
| 6. 接口契约、Project 1 和更多来源 | 本报告：Provider/QA 声明与 Project 1 三件交接材料，七来源任务包实际进入同一 Graph |

后续 Agent 主线仍需完成：让真实模型基于 diff 自主选择有价值的调查/实验，把阶段计划编译到可恢复 Runtime 的正式执行准入，再连接当前候选真实观察与独立判定。当前实验执行和 QA 归档复核仍由操作员入口驱动，Agent 读到契约不等于已经执行它。

此外仍未完成真实企业 Jira/文档系统、真实 Project 1 AWS/检索服务、通用来源搜索/Webhook、企业 IAM 和跨主机信任隔离、Codex 修复授权与独立复验、语义裁决及发布 Final。旧内嵌产品材料仍用于历史回放和部分能力校准，尚未全面删除。本轮没有通过更换架构表述把这些事项算作完成。
