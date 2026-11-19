# 迟到用量跨系统验证：可重建、可恢复执行切片

日期：2026-11-19，Australia/Sydney。实验：`egaplate20261119a`。

本轮已经创建新业务对象，实际运行 Chatwoot、Lago、Commercial Source、Keycloak、SUT PostgreSQL/Redis/Worker 和本地 qwen2:7b，完成主实验、对照、进程中断恢复及完成后的再次回放。结果为 **PASS_SCOPED_DETERMINISTIC_CHECKS**：183 项限定业务断言通过；4 个 Job、4 次模型调用、4 条私有备注完成；恢复没有重复发送已确认事件或增加模型调用。

这次交付是 **MANUAL_FIXED_PLAN_INTEGRATION_ONLY**。它验证执行工具、独立观察和恢复协议，不能算 Verify Agent 自主验证成功。自动派发资格没有开放，没有修改业务规则、客服产品源码、六份目录 YAML、`docs/**` 或 README。全文案语义、模型推理质量、代码修复与发布均未获得 PASS。

机器结果见 [summary.json](summary.json)，183 条断言名称与结果见 [checks.json](checks.json)。原始材料保留于本机 `artifacts/business-source/20261119-late-usage/`，不提交 Git。

## 1. 这次实际验证什么

业务问题是：一个退款申请处理时，用量事件还没有到齐；事件到齐后，产品能否明确重新评估，并正确区分事件发生在申请之前还是之后。

两组分别使用新会话、新订阅、新发票、新申请、新 coverage 和新事件。它们共享本轮新建的 tenant-b 客户与计量定义，按各自订阅、发票、会话和申请隔离。既有 Chatwoot 账户、Keycloak 身份和原生服务部署被明确复用，没有复用历史已封存的业务对象。

| 项目 | 主实验 `late_usage` | 对照 `post_claim_usage` |
|---|---|---|
| Chatwoot 会话 / 原消息 | 23 / 50 | 24 / 51 |
| Lago 订阅 | `egaplate20261119a_late_usage` | `egaplate20261119a_post_claim_usage` |
| 发票 | `dbfaa736-953c-4ff7-b117-80cfbc23c57f` | `9f021fff-34c1-4dd7-afc1-1b171d13ea75` |
| 申请 | `egaplate20261119a-claim-late_usage` | `egaplate20261119a-claim-post_claim_usage` |
| coverage | `egaplate20261119a-coverage-late_usage` | `egaplate20261119a-coverage-post_claim_usage` |
| 目标事件 | `egaplate20261119a_late_usage_delayed` | `egaplate20261119a_post_claim_usage_after_claim` |
| 业务发生时间 | 11 月 20 日，申请之前 | 11 月 22 日，申请之后 |
| 原申请业务时间 | 11 月 21 日 | 11 月 21 日 |
| 封存后原生整期目标用量 | 1 | 1 |
| 封存后申请区间用量 | 1 | 0 |

上述业务日期均为 UTC 零点。真实原生账期为 `2026-11-19T00:00:00Z` 至 `2026-12-18T23:59:59Z`；政策为 `refund-standard:lab-v2`，申请距模拟续期 172800 秒，退款窗口 604800 秒。准备器没有改写原生期间来适配测试。

**三个时钟独立记录。** 业务事件发生时间与申请时间来自 `LOCAL_SIMULATED_BUSINESS_CLOCK`；两个预登记原生事件 timestamp 为 11 月 19 日 00:43:10；实际发送与观察也发生在 11 月 19 日。这不是在真实时间等待三天，也不证明真实客户在未来日期已经申请。合同接受、商业续期与付款仍为合成实验材料；原生 INITIAL 发票被明确映射到模拟续期，不证明 Lago 自然续期调度。

初态通过真实 GET 读回：目标指标 0、无关指标 9、coverage OPEN、pending=1、事件已预登记但尚未发送/接收、没有封存与对账、申请与原消息已固定。两组均为 READY。9 单位无关指标用来检查产品是否错误混加计量。

## 2. 真正运行的调用链

```text
late_usage_execution.py prepare
  → candidate_manifest：核对提交、文件集合与实际产品字节
  → ExperimentLedger：登记实验身份
  → NativeObjects.prepare / SourceObjects.prepare
  → observe_initial_state：独立确认 OPEN 初态

deploy → 受信 environment host → 独立 PostgreSQL / Redis / SUT 进程

run → 提交 J1 → Observer 读取 Job、快照与原生事实 → 独立断言
    → dispatch_usage：只发送预登记事件，保存 HTTP 回执
    → observe_usage：只读指定指标聚合
    → record_usage_received → reconcile_usage → seal_usage
    → 普通重试复用 J1 → 显式 reevaluate 创建 J2
    → Observer 读取 J2、重新读取旧 J1 → 独立断言与汇总
```

发送事件、看见聚合、登记接收、对账、封存现在是分开的操作。`send_usage()` 保留兼容入口，但内部组合这些端口；旧整批脚本及既有场景没有被替换。

观察器同时读取 SUT HTTP、只读 SQL 冻结快照/ACCEPTED 事件、Chatwoot 会话/消息、Lago 发票/用量、Commercial Source。观察与裁决分开，不导入产品资格函数来计算预期。

本次两次首次聚合读取即看见目标用量 1，没有实际触发“聚合尚不可见”的额外调查分支。相关 PENDING/WAITING/UNKNOWN 恢复由测试覆盖；不能描述为 Agent 根据新证据自主选择了分支。

## 3. 实际业务结果

| 场景 / 阶段 | 结构化结果 / 后续动作 | Job | 私有备注 |
|---|---|---|---|
| 主实验 J1，OPEN | NEEDS_MORE_EVIDENCE / REQUEST_INFORMATION | `425d8254463041e589a3c103623b1613` | 52 |
| 主实验 J2，SEALED | INELIGIBLE / NO_ACTION | `34a4a0cb8e7c4d84bfbd568868f88a03` | 53 |
| 对照 J1，OPEN | NEEDS_MORE_EVIDENCE / REQUEST_INFORMATION | `b47633b4199e4582b3e17a9f10f8f5af` | 54 |
| 对照 J2，SEALED | ELIGIBLE / HUMAN_REVIEW | `6f295f3135154eccb3f9b4114f610499` | 55 |

两组普通重试均取得 HTTP 202、`reused=true`、原 J1。显式重评均取得 HTTP 202、新 J2 与正确父 Job；独立数据库 ACCEPTED 记录也支持父子关系。新事实摘要改变，coverage 从 revision 1 / OPEN 变为 revision 5 / SEALED；原申请、预登记事件与旧快照保持不变。最后写回正确会话的私有备注，没有退款、对客公开发送或关单。

主实验的 38 项初态、46 项末态和 7 项转换断言通过；对照的 38、47、7 项通过，共 **183/183**。断言覆盖申请区间、指标、封存发送/接收/聚合依据、重试/重评、原申请、旧快照、父关系和交付范围。初态准备资格另行记录，不重复计入这 183 项。

## 4. 中断恢复具体证明了什么

在主实验 J1 完成、目标事件取得 Lago HTTP 200 且父步骤 COMPLETED 提交后，执行进程调用 `os._exit(75)` 真正退出。中断位置在第一次聚合观察之前，回执与 PID/时间保存在 `interruption.private.json`。

中断时：8 个步骤完成、1 个 Job DONE、1 次模型调用 COMPLETE。新进程恢复复用了 `late_usage.submit`、`late_usage.observe.j1.1`、`late_usage.dispatch` 的原结果，随后完成其余阶段。完成后共有 27 个操作，全部 COMPLETED，每个 `dispatch_count=1`。

之后再次启动新执行进程，回放 22 个 run 阶段操作；数据库中 Job、模型调用、预算记录逐对象与首次完成时一致，仍是 4 个 Job、4 次模型调用，两个目标事件各发送一次。原生聚合各为 1，`events_count=1`。准备阶段另外发送的两个无关指标事件不计入这两个目标事件。

中断期间复核并增强了执行器部署身份校验、独立读回恢复和汇总逻辑，**没有改被测产品或业务对象身份**。准备工具身份另存，每个后续实际执行操作记录当时的工具 SHA，旧步骤保留原工具版本；本轮恢复包含执行器升级，不能称为全程同一工具构建的实验。真实 PG 测试另有同版执行器在写效果后、完成前退出的覆盖。

账本使用 PostgreSQL 独立 schema，按 experiment/operation 固定候选、配置和输入身份，以会话 advisory lock 阻止并发抢占。DISPATCHED 在外部调用前提交；仅有真实 HTTP 回执才能登记已发送。COMPLETED 回放校验原证据；孤立 DISPATCHED/UNKNOWN 拒绝盲目重发，需要显式只读对账。它不声称消除了跨网络事务的未知窗口。

WAITING 或终态缺少关键独立读回时，新建有界只读观察，不创建新 Job；每个 Job 阶段最多三轮观察。聚合读每次最多 30 次、跨恢复最多 90 次。原始观察包逐次落盘，不能只保留最终 success。模型请求/输出未采集属于能力缺口，不会因此无限重读。

本轮真实业务中断发生在已确认提交之后；“写效果发生但完成回执未知”的路径由 13 项真实 PostgreSQL 测试中的受控外部效果测试验证，不冒充真实 Lago 网络故障实验。

## 5. 候选与执行身份

- 被测产品提交：`45a0378585b8970fb2860897c9e49792e15a9df0`。
- 31 份产品文件的集合摘要：`9871f6f896cfb27dd7fb79367642f7593814d21ad2c82b0a0edc993f49045378`。
- SUT 实例：`environment_f0284266-e2d6-4913-aa9b-201b4d5f2ec5`。
- 商业来源实例：`commercial_25b803ae30d347ff95ccad710f0442b4`。

每次步骤执行前核对产品文件集合/字节、原生配置、部署账本回执、实际 URL、实例目录、角色进程签名与配置。单独改本地 `sut.json` URL 不能把另一实例冒认成该候选。原证据按字节摘要校验，本轮已核验 151 个引用。

资格仍为受信本机工作树的精确源码匹配。它没有安装任意候选到隔离不可信代码沙箱，也未证明依赖制品供应链、内核隔离或企业防篡改。本轮只运行一个产品候选；主例与对照是业务输入比较，没有实施 B/C 双候选环境或 Codex 修复后的 C1 对比。

## 6. 模型结果与没有取得的证据

4 次真实模型调用均为本地 `qwen2:7b`，预算 4/8。网关实际记录 prompt 11365 tokens、completion 191 tokens，共 11556。未使用外部付费模型。这是被测客服产品的模型调用；Verify Planner 本轮没有调用或自主派发。

仍缺完整确定性决策记录、实际发给模型的请求和原始模型响应。当前网关的 `request_body` 是 Job 身份信封，receipt 中的 decision/request/response SHA 也不能替代原文。观察器保留三项明确 gap，不反推模型看到了什么，也不根据最终文字宣称根因在生成或组装层。

文案结果必须与上述硬断言分开：

- 三条自动探针为 REVIEW_REQUIRED：对照初态两条（原草稿未显式说明实验范围、缺证时出现资格相关词），对照末态一条（原草稿未显式说明实验范围）。词汇探针可能误报，“确保符合退款标准”也可能是目的表述，不能据此签发语义 FAIL。
- 人工阅读额外观察到：两个初态都出现“使用情况似乎超出政策限制”。在 coverage OPEN、结构化结果为补证时，这种暗示需要业务评审；现有探针没有完整覆盖，空 findings 也不表示文案通过。
- 两个对照草稿没有实验范围措辞，但真实交付的四条私有备注包装全部包含本地实验/模拟声明。不能将原草稿问题误写成交付整体缺失声明。
- 历史“申请超窗”理由本轮没有重现。主例 J2 依据用量超限拒绝，没有声称超窗。**产品没有修改，本轮也没有 C0→C1 对比，所以不能称历史缺陷已修复或稳定不再出现。**

`model_semantic_quality` 保持 `NOT_FULLY_EVALUATED`，没有发布授权。

## 7. 文件职责与测试

| 文件 | 本轮实现 |
|---|---|
| `tools/multi-system/release_seed.py` | 新实验两种规格、原生对象与初态核验；将发送、聚合观察、接收登记、对账、封存拆开；原兼容入口仍可用。 |
| `tools/multi-system/experiment_ledger.py` | PostgreSQL 实验/操作/状态事件账本、并发控制、证据哈希、UNKNOWN 拒绝重发与只读对账。 |
| `tools/multi-system/late_usage_execution.py` | prepare/deploy/run/status 入口，精确候选部署绑定，固定计划、稳定操作键、退出点、预算和恢复。 |
| `tools/multi-system/late_usage_observer.py` | 独立只读观察、阶段与转换断言、受理失败观察、显式证据 gap、保留各阶段失败与提示的汇总。 |
| `tools/multi-system/test_late_usage_stages.py` | 分步前提、旧接口兼容、无重复发送与观察身份测试。 |
| `tools/multi-system/test_experiment_ledger.py` | 真实 PostgreSQL 并发、子进程退出、未知写拒绝重发、对账与证据篡改检查。 |
| `tests/multi_system/` 三份测试 | 候选/部署绑定、观察恢复、输入/协议拒绝、独立规则与失败汇总。 |

最终测试为 **156 项通过、0 跳过**：`tools/multi-system` 107 项（包含 13 项真实 PostgreSQL），`tests/multi_system` 49 项。真实业务的 183 项断言另计。受理端口的无 Job/HTTP 503 观察由测试覆盖；本轮真实主实验没有注入订阅 503，也没有修复它。

测试命令使用项目 venv；PG 连接从本机 0600 私有文件读取，不能把凭据放入命令或 Git：

```bash
EGAP_LEDGER_TEST_DSN_FILE=/absolute/private/dsn-file PYTHONDONTWRITEBYTECODE=1 \
  services/genai-support-sut/.venv/bin/python -m unittest discover -s tools/multi-system -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/multi-system \
  services/genai-support-sut/.venv/bin/python -m unittest discover -s tests/multi_system -q
```

本轮入口的使用顺序为：先用 `commercial-source.py up` 创建新的来源目录，再 `late_usage_execution.py prepare` 明确 candidate/run-id/period-start，随后 deploy、run。`--interrupt-after late_usage.dispatch` 是已实现的测试退出点。再次实验必须使用新 ID 和符合实际原生账期的日期，不删除旧状态或覆盖旧证据制造初态。

## 8. 保存与下一步边界

`object-inventory.private.json` 保存新对象及初态；`execution-manifest.private.json` 固定身份；`interruption-*` 保存中断时事实；`first-completed-result.private.json` 保留首次完成结果；`execution-result.private.json` 是再次回放结果；`completed-audit.private.json` 与 `replayed-audit.private.json` 是只读数据库导出。PG 内仍保留全部操作和事件，原始证据位于 `execution-evidence/`。关键文件摘要在公开 summary 中，私有正文和凭据留在忽略目录。

本轮专属 SUT 角色与商业来源服务已停止，专属 PG/Redis/商业数据库容器已停止；本轮启动的六个原有依赖恢复为先前停止状态。所有数据库卷和原生业务对象保留，原先运行的原生应用未停止，旧 active 指针未覆盖。停止后的环境不是当前可访问的在线演示；离线证据仍可复核。

后续要接通的是 Agent 的执行授权与观察反馈：在现有能力绑定/变化关系门槛通过后，由 Runtime 调用这些分步端口，把原始观察交回同一 Graph，记录真实决策分支。本轮独立执行账本不是替代正式 Runtime 的第二套生产授权中心，接入时应统一操作身份和执行所有权。

同时需要补网关完整决策/实际请求/原始响应的安全留存，再形成有证据的文案 Finding，交给隔离的 Codex 编码入口产出 C1。之后按真实 diff 比较独立等价初态、执行必要保留检查与新的隔离样本。已有公开样本不能算未见能力测评；MCP 注册不等于执行授权，企业规则批准、实际维护者反馈、Protected Final 和发布链均另行验收。
