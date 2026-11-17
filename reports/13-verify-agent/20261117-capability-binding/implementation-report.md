# Verify Agent 能力绑定与隔离评测：实现报告

日期：2026-11-17。范围：`commercial-decision.v1` 的真实接口绑定、纯 fixture 输入装配、规划准入、有限修正，以及冻结实现后的独立隔离评测。

本轮已实现真实接口绑定和业务关系评估，但当前本地 `mistral:latest` 在三项新变化、共六次运行中，**0/6 形成可接受的绑定计划**，没有通过预登记的稳定性门槛。因此依照“稳定形成可判定计划后再接真实实验”的先后条件，本轮没有开启 Runtime 业务执行。工程测试通过与模型任务未通过是两个不同的结论，均完整保留。

本轮将“看起来具体的检查描述”进一步收敛成“能够引用真实函数、真实输入字段、真实输出类型和准确版本材料的待执行计划”。系统仍处于阅读与规划阶段。`BOUND_PLAN` 表示通过绑定编译，`STRUCTURALLY_COMPLETE` 表示结构完整；两者都不表示业务正确、实验已经运行或允许发布。

## 1. 绑定的是哪个业务组件

目标函数是 `services/genai-support-sut/src/genai_support/integrations/commercial.py::commercial_decision(context)`。它是客服产品内部的确定性商业判断：根据冻结的合同、续期事件、原申请、受控用量覆盖及模拟付款资料，生成资格建议、拟议动作、缺失证据和理由。它属于被测产品，不能同时充当独立 Oracle。

现有目录的 `unit.commercial` 实际指向 `services/commercial-source/tests/test_commercial_source.py`，检查商业来源服务的协议、真实 PostgreSQL 和 HTTP 行为；它不是这个判断函数。现有 `unit.sut_decision` 才指向产品判断单测。因此新增能力为 `probe.commercial_decision`，父能力明确为 `unit.sut_decision`，没有改写旧能力的含义。

旧 `tools/multi-system/release_check.py` 仍然只有 `--directory` 入口，固定 `TARGET_COMMIT=bdccf89ea431ab6ff678e754a97113b54547e5d4`，遍历预置对象、执行完整阶段矩阵和边界检查。它没有本轮所需的任意 `select`、标量覆盖或任意候选接口。`run.release`、`check.business`、`unit.commercial` 等既有目录能力不会因名字存在就变成已绑定能力；绑定器对非本期能力返回 `CAPABILITY_NOT_BOUND`，不静默转交旧脚本执行。

新增场景为 `commercial_unit`，阶段为 `commercial_unit.evaluate`，执行模式为 `PURE_FIXTURE`。装配器把它们标为 `LOCAL_TRUSTED_BINDING_CONTRACT_OVERLAY`，与原冻结 YAML 目录的身份分开；旧目录没有被编辑，也没有获得派发授权。

## 2. 入口与控制权流向

产品现有调用关系是：

```text
ResolutionGateway.resolve
  → ExternalSnapshotTools.collect：读取已绑定 Job 的冻结上下文
  → ExternalSnapshotTools.evaluate_refund_policy
  → workflow.external_decision
  → commercial.commercial_decision
  → 产品草稿装配及后续模型链路
```

本轮 Verify Agent 的规划调用关系是：

```text
可信操作员 / development_release.py
  → release_assembler.assemble_release：准确 B/C、diff、规则及能力合同
  → Runtime.acceptRun：核对本机 authority 与发布包合同
  → LangGraph / RuntimePort
  → Runtime.decide：预留预算，持久化请求，调用本地模型
  → parseDecisionProposal：严格共享协议
  → Runtime.submit
      → semanticError：版本、上下文、读取回执、引用与范围
      → assessCheckQuality：具体性与结构缺项
      → assessCheckBinding：真实字段、类型、来源与版本关系编译
  → NEEDS_REVISION：全部缺项入账，有限次修正
     或 PROPOSED_ONLY + BOUND_PLAN：封存待执行计划，结束规划切片
```

`tools/verify-capabilities/commercial_fixture.py` 是另外一层可信输入装配工具，提供 `prepare_probe` 与 `validate_result`。当前 Graph/Runtime 没有通过它执行候选。它不是隐藏的执行后门，也不从模型请求中接受函数、模块、文件或网络地址。

## 3. 20 个实际可控输入

每个参数都对应固定 fixture 内已存在的标量路径；数组下标也是登记路径的一部分。输入全部为可选覆盖项，未覆盖字段取受信 fixture 默认值。模型不能增加路径或通过属性访问注入代码。

模型协议中的 `value` 仍使用字符串；TypeScript 编译器按字段合同明确转换为整数、布尔或字符串。Python adapter 接收已经类型化的值，不再猜测字符串 `"false"` 或 `"100"` 的意思。

| 输入路径 | 类型与边界 |
| --- | --- |
| `billing.total_amount_cents` | integer；0–1000000000 |
| `billing.currency` | string；枚举 USD, EUR, GBP |
| `billing.external_customer_id` | string |
| `subscription.external_subscription_id` | string |
| `commercial_source.scope.tenant_id` | string |
| `commercial_source.events.0.term_number` | integer；1–100 |
| `commercial_source.claim.claimed_at` | string；带时区、日历有效的 date-time |
| `commercial_source.contracts.0.status` | string；枚举 ACTIVE, DRAFT |
| `commercial_source.contracts.0.effective_from` | string；带时区、日历有效的 date-time |
| `commercial_source.contracts.0.acceptance.accepted_at` | string；带时区、日历有效的 date-time |
| `commercial_source.contracts.0.terms.special_terms` | boolean |
| `commercial_source.coverage.status` | string；枚举 OPEN, SEALED |
| `commercial_source.coverage.pending_count` | integer；0–1000 |
| `commercial_source.coverage.reconciliation.matched` | boolean |
| `commercial_source.payment.status` | string；枚举 PAID, UNPAID |
| `commercial_source.payment.amount_minor` | integer；0–1000000000 |
| `commercial_source.payment.currency` | string；枚举 USD, EUR, GBP |
| `commercial_source.payment.customer_id` | string |
| `commercial_source.payment.paid_at` | string；带时区、日历有效的 date-time |
| `commercial_source.invoice_link.native_invoice_kind` | string；枚举 INITIAL, RENEWAL, TERMINATION |

覆盖字段只是实验输入。比如覆盖付款金额不会自动修正发票金额；修改申请时间不会自动改写用量覆盖区间。这样保留了跨字段矛盾供验证者发现，也防止适配器替模型“修好”实验条件。设计有效实验时，Agent 仍需判断哪些关联条件必须保持或显式调整。

## 4. 21 个真实输出及类型约束

观察使用 `decision.*` 路径，指向该函数实际返回的字段，来源绑定为 `sut_runtime`。这里的来源名称表示产品函数输出；纯 fixture 输出不是 Chatwoot/Lago 的独立原生读回。

| 输出字段 | 类型 | 可为空 | 枚举限制 |
| --- | --- | --- | --- |
| `decision.workflow_revision` | `string` | 否 | — |
| `decision.policy_ref` | `string` | 是 | — |
| `decision.billing_record_ref` | `string` | 否 | — |
| `decision.elapsed_seconds` | `number` | 是 | — |
| `decision.window_seconds` | `number` | 是 | — |
| `decision.within_window` | `boolean` | 是 | — |
| `decision.usage_units` | `string` | 是 | — |
| `decision.maximum_usage_units` | `string` | 是 | — |
| `decision.usage_within_limit` | `boolean` | 是 | — |
| `decision.contract_terms_match` | `boolean` | 是 | — |
| `decision.billing_paid` | `boolean` | 是 | — |
| `decision.missing_evidence` | `string[]` | 否 | — |
| `decision.eligibility` | `string` | 否 | `ELIGIBLE`, `INELIGIBLE`, `NEEDS_MORE_EVIDENCE` |
| `decision.proposed_action` | `string` | 否 | `REQUEST_INFORMATION`, `HUMAN_REVIEW`, `NO_ACTION` |
| `decision.reason_codes` | `string[]` | 否 | — |
| `decision.evidence_refs` | `string[]` | 否 | — |
| `decision.authority_scope` | `string` | 否 | `LOCAL_LAB` |
| `decision.assessment_scope` | `string` | 否 | `LAB_SCOPED` |
| `decision.review_status` | `string` | 否 | `LOCAL_CALIBRATION` |
| `decision.payment_simulated` | `boolean` | 否 | — |
| `decision.usage_completeness_scope` | `string` | 否 | `LOCAL_CONTROLLED_GENERATOR_ONLY` |

每个输出的可用算子写在合同的 `operators` 内。数字字段才支持 `GREATER_THAN`、`LESS_THAN`，且比较值必须为有限数字，不能比较“大于 null”。数组 `CONTAINS` 接收单个字符串成员；数组整体比较使用 JSON 字符串数组。存在性关系只对可空输出开放，协议要求 `PRESENT`、`ABSENT` 的 `value` 为 `""`。`UNKNOWN`、`null` 不再被通用关键词检查一概当作占位词；是否允许由具体输出类型和枚举决定。

编译器不把类型正确当作断言正确。例如它能拒绝未知的 `decision.is_refundable`，却不会仅凭某个有效枚举就认定该枚举符合公司的退款合同。所有类型、枚举与算子检查之后，`semantic_correctness` 仍为 `NOT_ESTABLISHED`。

## 5. fixture 来源、规则日期与身份

fixture 的构造起点是已有单测中的纯输入构造逻辑 `source_asset` / `example_context`，没有调用 `commercial_decision` 来计算输入或填入预期。原单测的简化政策分界为 2026-11-01，本次没有把它直接冒充正式目录规则。

本次将续期设为 `2026-11-09T00:00:00Z`，申请为 10 天后，指定指标用量为 0；加载真实冻结的 `business-source-pack/v1/policies/lab-v1.json`、`lab-v2.json`，政策分界统一为 R1 的 `2026-11-10T00:00:00Z`，并为合成客户/订阅渲染固定的标准合同模板，重新登记文档摘要和 `refund-standard` 政策引用。变换过程在 `fixture_provenance.transform` 中明确记录。

上下文内看似 Lago 回执、用量或付款的结构全部是合成输入。本轮没有查询原生 API，也不能据此宣称已验证自然续费、真实付款或全部生产事件到齐。

| 身份 | 值及含义 |
| --- | --- |
| 能力合同 | `commercial-decision.v1`，`schema_version=1` |
| 基础 fixture | `commercial-standard-renewal-v1` |
| fixture 内容 SHA-256 | `c10aa513aef11f1e3619876e4d61efd61e5494b2759b49431808f086b4682d49` |
| 已审商业源码 SHA-256 | `48a1b5a123beda68c6eb26a7d02a7e04d3ba0f9caf9471fda89bd7219308d28d` |
| fixture 来源提交 | `0f46c9b0602334db2b8955c018ed076c6c059c37` |
| 规则原文 SHA-256 | `409799ea7f462f13dde5ddaad566db1d842a64f133bf4a4b6c23e3bc8829b07c` |
| 合同文件字节 SHA-256 | `9f0b97746eb128a9cd9d4b63dd3fd92faa825aa864a46a33752cd12e6da85ae3` |
| Runtime 合同规范摘要 | `c9cbeec93cee67b545915b48e31f517d2e50616dff69299bd28e8ad3fe5dbf54` |

fixture 使用 `canonical-json-sort-keys-utf8-v1`：UTF-8、键按码点排序、无多余空白、数组保序、禁止非有限数值。它与合同文件的原始字节摘要、Runtime 对合同对象的规范摘要是不同身份，不能相互替换。

Runtime 从本机固定合同文件加载 authority，并核验当前受信商业源码摘要。发布包中的 `planning_contracts` 必须与本机 authority 一致。绑定还要求准确基线与候选入口材料：基线源码必须匹配合同审查的源码摘要，候选必须绑定本次 `candidate_sha` 且内容摘要一致。缺少入口文件材料、基线仅含摘录函数、基线漂移或发布包自造合同均不能形成绑定计划。候选材料的存在与摘要一致仍不证明候选保留了正确函数签名；真正装载和调用入口、处理导入或输出协议失败，属于未来 Runner 的执行责任。

## 6. `expectations` 与 `change_relation` 分别表达什么

`expectations` 表达“依据已读取的业务要求，观察结果应满足什么条件”。每个断言必须绑定具体观察、允许的算子、类型合法的预期值和适用要求；每个观察都要有断言，提案中的每项要求都要落到受支持的输出断言。

`change_relation` 表达“针对这个准确版本变化，模型预测 B/C 行为会怎样”。它包含 `diff_ref`、实际改动行中的 `changed_fields`、显式输入 `input_refs`、观察 ID `output_refs`、要求引用及原因。`MAY_CHANGE` 要求列出至少一个不同的 B/C 预测；`SHOULD_PRESERVE` 要求两边一致。多个输出使用按观察 ID 索引的完整 JSON 对象，不能遗漏或夹带额外字段。

这两类信息不能混用。模型可以预测候选会输出一个不符合规则的值，用于设计证伪实验；这并不改变规范断言，更不等于已经观察到缺陷。编译结果明确标记版本关系为 `MODEL_HYPOTHESIS_NOT_VERIFIED`。

编译器只确认字段或符号确实出现在新增/删除行、引用属于当前 diff、输入和输出能够解析。它不证明这条变化在因果上影响该输出，也不证明实验足够有区分力。即使所有引用检查通过，仍需要独立语义评审和实际执行证据。

主实验/对照与 B/C 是两条不同的比较轴：前者改变实验条件，后者比较代码版本。编译计划分别保留两份输入上下文、对照关系、B/C 预测及版本身份，没有把某一份模型预测当作执行结果。

## 7. 纯 fixture 阶段与真实多系统阶段

`commercial_unit.evaluate` 的前置条件包括冻结候选源码、核验可信 fixture/合同摘要、隔离且无网络的 Runner、独立 primary/control 副本和计划稳定性门槛。它们在合同中标为 `REQUIRED_AT_EXECUTION`，编译后仍标为 `NOT_EVALUATED`。

这不满足 `late_usage.initial → late_usage.after_seal` 的真实前提。后者还需要创建并保存初始 Job/快照，实际发送用量，读回 Lago，核对事件集合并封存 coverage，显式 reevaluate，检查父子 Job 和旧快照不变。不能仅设置一个阶段字符串或把 fixture 的 `coverage.status` 改为 `SEALED` 就声称做过这些动作。

当前 `prepare_probe` 只校验固定能力/场景/阶段以及登记的类型化覆盖，返回完整合成输入及摘要；primary 和 control 每次从基础 fixture 深拷贝，互不污染。`validate_result` 只校验实际函数返回的字段集合、类型、枚举和可空性，回执仍标为 `PRODUCT_FUNCTION_OUTPUT_ONLY`、`business_verdict=NOT_ESTABLISHED`。

adapter 合规测试确实调用了受信当前版本的纯商业函数，并禁止 socket，以确认 20 个登记输入与 21 个输出兼容；基础 fixture 还检查没有自身装配造成的缺失关系。这是适配器自检，不是 Graph 执行新候选，不是独立业务 Oracle，也不构成跨系统覆盖。

## 8. 阅读回执、模型反馈与预算

READ 读取准确材料的本地行号，一次最多 200 行。合同要求只有在对应片段被连续、无缺口地完整读取后，才成为 CHECK 可用的依据。生成 Schema 的 CHECK 要求列表取“已读完要求”和“能力支持要求”的交集；只读了 R6 不会被诱导去生成本能力不支持的 CHECK。

模型第一次宣称 `NOVEL_CHECK` 能力缺口前，也必须取得至少一次相关材料 READ 回执。生成器和 Runtime 均约束此事。这个门槛只避免完全未阅读就宣布接口缺失；它不证明一次 READ 已充分理解业务，也不会把“执行前提尚未评估”自动等同“能力不存在”。

模型只能得到接口描述和 20 个默认标量。`planningContractView` 移除完整 `fixture.context`，不提前给出完整规则原文或任何 fixture 执行结果。原始材料仍经受控 READ 进入上下文。

具体性和绑定错误会一起返回，包括阶段、输入类型/值域、观察来源、输出路径、断言关系、对照与版本差异引用。数据库保留每一份原提案、原始模型响应、缺项和用量，不以修正结果覆盖旧记录。

缺项超过 64 条时，模型上下文中的缺项压缩为 `[code, path, message_ref]` 三元组，并提供 `issue_columns` 与共享 `issue_messages` 字典。所有缺项仍在，数据库中的完整消息不变；这只是消除重复文字。`latest_feedback` 不重复携带已经存在于 `check_repair` 的整份规格与缺项。若实际请求仍超过 100KB，Runtime 持久化 `BLOCKED / CONTEXT_TOO_LARGE`；因为尚未登记或派发本次调用，不消耗这次模型额度或修正次数。

修正额度从总 `model_calls` 内预留，默认最多一次，允许显式配置到两次；不是额外免费调用。每次修正阶段的模型调用都记账，包括继续 READ 或产生无效响应。并发重放不重复计数；重启从 Runtime 状态恢复；取消后的迟到响应只留账，UNKNOWN 不自动补发。读取回执、拒绝记录与模型 token 用量分别保留，预留额度也不冒充已确认的供应商费用。

本地模型的 seed 已进入固定配置与请求身份，不在失败后悄悄换 seed 重试。修正次数的预增只属于记账，不改变本次提案的 `context_ref`，避免合法修正在返回时自我过期。

## 9. 逐文件职责与阅读顺序

| 文件 | 本轮职责 |
| --- | --- |
| `protocols/verify-agent/capabilities/commercial-decision.v1.json` | 唯一能力合同：真实入口、20 输入、21 输出、fixture、来源摘要与未满足的执行前提。 |
| `packages/contracts/src/verify.ts` | 为 `CheckSpec` 增加可选 `binding` 和 `change_relation` 类型；保留旧 CHECK 传输兼容。 |
| `protocols/verify-agent/v1/decision-proposal.schema.json` | 跨语言严格协议：提供的结构必须合法，未知字段拒绝；缺少可选规划内容交由 Runtime 具体反馈。 |
| `protocols/verify-agent/v1/conformance.json` | 78 个共享合法/非法传输样本，供 TS、Python 与生成 Schema 对齐。 |
| `packages/verify-runtime/src/capability-authority.ts` | 加载本机 authority、核对受信源码、校验发布包合同，并生成不含完整 fixture 的模型导航。 |
| `packages/verify-runtime/src/check-binding.ts` | 将字符串提案绑定为类型化输入、真实输出断言和版本关系；拒绝未知能力、假字段、错阶段与漂移源码。 |
| `packages/verify-runtime/src/check-quality.ts` | 保留结构完整性检查，避免将合法的 UNKNOWN/null 值一概当作占位词。 |
| `packages/verify-runtime/src/context.ts` | 受控阅读导航、合同接口约束、生成 Schema、支持要求交集及无损反馈压缩。 |
| `packages/verify-runtime/src/runtime.ts` | 统一准入、结构与绑定合并反馈、有限修正、持久原提案/用量、配置身份与不派发边界。 |
| `packages/verify-runtime/src/types.ts` | 发布包 `planning_contracts`、模型 seed 及既有修正状态的类型约定。 |
| `services/verify-agent/planner-prompt.txt` | 说明真实接口、规范断言与版本预测的区别，以及阅读、有限修正和未执行边界。 |
| `tools/registry/release_assembler.py` | 加入独立能力合同 overlay，保存完整 B/C 入口材料；候选仓库不能自行提供 authority。 |
| `tools/verify-capabilities/commercial_fixture.py` | 纯 `prepare_probe` / `validate_result` 适配；没有执行候选的公开函数。 |
| `tools/verify-capabilities/development_fixture.py` | 生成公开的 100/99 付款输入对照，只提供事实与摘要，不提供答案。 |
| `tools/verify-capabilities/development_release.py` | 将已公开的窗口 `<=`/`<` 干预应用到完整商业模块，生成开发 Git 版本对和只读 bundle，不执行候选。 |
| `tools/verify-agent-live.py` | 将 seed、预算等固定参数传给真实模型—Runtime 环路，保留证据与运行身份。 |
| `tools/verify-agent-binding-holdout.py` | 独立准备完整源码的合成 B/C、冻结业务关系评分和接入门槛；只对实际绑定成功的模型计划原样进行受审纯函数离线见证，不替模型补参考输入。 |
| `packages/verify-runtime/src/check-binding.test.ts` | 覆盖真实合同、类型/枚举/日期/可空值、原型注入、版本关系和来源身份。 |
| `packages/verify-runtime/src/binding-context.test.ts` | 独立验证真实生成 Schema、R6 限界、安全投影、伪造合同、B/C 缺失与上下文稳定性。 |
| `packages/verify-runtime/src/check-repair.integration.test.ts` | 真实 PG 验证有限修正、重放/恢复/取消/UNKNOWN、原提案和成本保留、密集反馈及超限阻塞。 |
| `packages/verify-runtime/src/runtime.integration.test.ts`、`model-schema.test.ts` | 验证新增准入和生成边界与既有 Runtime 行为兼容。 |
| `tests/verify_capabilities/` | Python adapter 接口合规、基线 fixture 健康性、真实函数输出类型、开发包与 overlay 测试。 |
| `tools/registry/test_release_assembler.py` | 核对新增 overlay 后的能力/场景计数与不授予执行、不泄漏历史答案的边界。 |
| `tests/verify_binding_holdout/test_preparation.py` | 核对新样本来源、变更范围、参考实验可区分性、良性变化、数据隔离和离线评价约束。 |

建议先读能力 JSON 的 `inputs/outputs`，再读 `assessCheckBinding`，随后看 `Runtime.submit` 怎样消费结果。最后阅读 Python adapter，可区分“计划怎样被编译”与“输入怎样被准备”；未来 Runner 还需要独立实现。

## 10. 已完成的工程验证与冻结身份

主线程汇总的本轮适用验证如下。它们是实现验证，不等于模型留出评测结果或产品发布资格。

| 验证 | 实际结果与边界 |
| --- | --- |
| TypeScript 全量测试 | 350/350，通过；包含真实 PostgreSQL 路径，0 skip。日志为 `artifacts/verify-agent/20261117-binding-validation/ts-final.log`。 |
| Verify Agent Python 测试 | 33 项通过；验证图与协议行为。 |
| Registry 测试 | 89 项通过；已更新新 overlay 后的目录计数与边界断言。 |
| Python 能力 adapter 测试 | 12 项通过；包括 20 个登记输入逐一与受信当前纯函数的兼容性检查。 |
| 共享协议 corpus | 78 个样本；覆盖兼容提案、完整绑定及非法结构。 |
| 独立留出准备器测试 | 独立准备侧报告 9 项通过；仅证明准备器检查通过，不表示 Agent 已通过留出。 |

冻结清单：`artifacts/verify-agent/20261117-binding-validation/implementation-freeze.json`。

清单文件 SHA-256：`846beec299714d93e1352470fa2765401ced07ed04de9ddbfcc41a3f92553af5`。

冻结时间：`2026-11-17T12:30:56.816080+00:00`。清单记录当时源码 HEAD，并用逐文件摘要覆盖尚未提交的本轮实现；不能把来源 HEAD 本身当作已经包含全部本轮变更的交付提交。最终 Git 同步身份由主线程交付时报告。

第 1–11 节的静态实现整理与账本用量汇总由未读取新样本答案的协作者完成；新样本准备、语义评分由另一协作者独立完成。主线程冻结实现后才阅读新变化并汇总第 12 节，未将结果反向写入冻结实现调参。

## 11. 尚未开启的执行与后续门槛

当前仍没有由 Runtime 执行候选函数、跨系统场景或退款业务实验；没有新的业务覆盖，也没有受保护 Final Oracle 或发布授权。完整计划仍带 `execution_ready=false`、`execution_status=NOT_EXECUTED`、`dispatch_qualified=false`。

冻结的稳定性门槛要求：三份独立留出变化，各运行两个全新任务；六次都需形成 `BOUND_PLAN`，达到预先固定的语义评分、具有区分力的条件与预期关系，并取得独立离线见证。这个门槛是否满足，必须由后续实际结果决定，不能用本报告的单测数字替代。

达到该门槛后，下一步才考虑为一个固定计划接入真正的 Runtime 实验：准确 B/C 隔离运行、无网络与凭据隔离、受信输入副本、输出采集、确定性断言执行、资源/时间预算、停止与清理、未知结果对账，以及独立证据身份。再扩展到真实跨系统阶段时，还必须补齐各阶段重建、原生操作、父子 Job、状态前提和精确清理，不能把本轮纯函数能力自动提升为完整客服系统测试能力。

## 12. 冻结后的真实模型验证结果

### 12.1 这次怎样区分“填对字段”和“理解业务”

绑定编译器检查真实输入、输出、字段类型、阶段、固定 fixture、源码身份和 diff 引用。它不负责用词面匹配证明因果关系。独立评价在模型运行之前固定五个评分维度，每项 0–2 分，并增加“正确识别破坏或保持、实际离线见证成立”的硬门槛。总分至少 8/10，关键输入与预期关系各为 2 分，仍不能代替硬门槛。

独立见证仅在模型实际产出被 Runtime 接受的 `compiled_plan` 后，原样使用其中的主实验和对照输入运行已审查、固定摘要的合成 B/C 纯函数；不会拿参考答案替代模型输入。本轮没有任何被接受的计划，因此六次离线评价均为 `NOT_RUN`。准备器为证明样本本身可区分而执行的参考实验，不计为模型成功，也不属于 Graph 的业务执行。

### 12.2 三项新变化与全部结果

这次没有把公开的退款窗口或付款边界开发案例重新命名成未见任务。独立准备者在主线程冻结实现前隔离准备了三个新 diff，模型只获得正常发布材料和接口合同。实现、提示词、模型与预算冻结后才开始六次运行，过程中没有改进后重跑或挑选更好结果。

| 新变化 | 应识别的业务关系 | seed 19 / 31 实际结果 | 独立评价 |
| --- | --- | --- | --- |
| A：合同 `accepted_at` 的上界从续费时间改成申请时间 | 合同必须在续费时已经被接受；续费后、申请前才接受的合同不能追认此前资格 | 两次均 `READ → CHECK_INTENT → 修正 CHECK_INTENT → STOPPED / CHECK_REPAIR_LIMIT` | 四个原案/修正提案均 2/10；没有可接受计划，离线 `NOT_RUN` |
| B：删除 `pending_count != 0` 条件 | `SEALED` 状态与没有待处理事件必须同时满足，不能用标签掩盖未闭合用量 | 两次均首次调用 `CLARIFY → BLOCKED / CLARIFICATION_REQUIRED` | 没有 READ、CHECK 或计划；任务计划分按缺失记 0/10，离线 `NOT_RUN` |
| C：交换两个整数金额相等比较的左右操作数 | 付款金额与发票金额相等的要求保持；语法变化不自动代表回归 | 两次均首次调用 `CLARIFY → BLOCKED / CLARIFICATION_REQUIRED` | 没有 CHECK 或计划；任务计划分按缺失记 0/10，离线 `NOT_RUN` |

A 的原案和修正都仍使用默认的历史合同接受时间，没有构造“续费后、申请前”的区分条件；对照输入为空。模型选读 R2，把合同接受截止点错误理解成退款窗口。它还把输出字段名当成观察 ID，并用日期表达布尔输出预测。Runtime 两次均返回以下五项实际准入缺项：`INPUTS_REQUIRED`、`CONTROL_NOT_DISTINGUISHABLE`、`BOUND_CONTROL_NOT_DISTINGUISHABLE`、`CHANGED_FIELD_NOT_IN_DIFF`、`CHANGE_OUTPUT_NOT_BOUND`。由于观察引用先失败，日期/布尔错误没有被本轮实际回执进一步报告为独立类型错误；不能把人工阅读发现与 Runtime 已报告的缺项混写。

B 直接泛问合同条款与资格之间的关系，未读取可用的 R3，也没有构造仍有待处理用量的输入。C 提到了相关金额字段和 R4，但把交换操作数说成比较算子的改变，未形成关系保持的检查。两者安全阻塞，但没有完成用户要求的规划。C 没有实际宣告 Bug，因此本轮不能据此计算或宣称已测得误报率。

这六次是三个业务变化各运行两个全新任务、seed 分别为 19/31。温度为 0，两种 seed 的结果一致，不能当作六个统计独立业务样本。三项变化都是明确标记的本地合成版本，不是真实企业发布 PR，也不足以估计总体准确率。

### 12.3 原提案、身份与成本

| 运行 | Runtime run_id | 实际模型调用 |
| --- | --- | --- |
| A / 19 | `3bdca05c-7f0b-4bc2-a602-3d22e005c72d` | 3 |
| A / 31 | `e66d967d-81d1-40b3-bf42-05b525aec016` | 3 |
| B / 19 | `b6ccd2d1-6db4-43d1-abf5-db6067b20892` | 1 |
| B / 31 | `8926de0b-364e-4d03-b952-75ceb607a61b` | 1 |
| C / 19 | `12cef18e-49ba-438a-825c-ad5b91a45d4a` | 1 |
| C / 31 | `2616ba23-fe41-4b1c-a0d5-c8cf3d8a020f` | 1 |

六次新变化运行合计 10 次模型调用，输入 88,520 tokens、输出 7,457 tokens，共 95,977 tokens。另有冻结前两次公开开发运行，合计 3 次调用、21,935 tokens：一次未阅读即提出能力缺口，另一次请求 Final 后请求澄清；两次均未形成计划，且保留各自实际实现身份，不冒充最终冻结版本的结果。

本轮全部八次真实运行共 **13 次调用、117,912 tokens**，其中输入 109,507、输出 8,405；13 次用量均完整。全部原提案、修正和失败调用保存在本地账本。金额为 `null / LOCAL_NO_PROVIDER_BILL`，没有供应商账单，不能解释成“计算免费”或零成本。

每次预登记预算为总调用 4 次、阅读 2 次、修正 1 次、每次最大输出 3,000 tokens、超时 180 秒、上下文 16,384。修正预算包含在总调用额度内，不是额外赠送。模型固定为本机 `mistral:latest`，digest 为 `6577803aa9a036369e481d648a2baebb381ebc6e897f2bb9a766a2aa7bfbc1cf`。

### 12.4 完整性、资源和交付位置

最终复核 104 个冻结文件，摘要变化为 0。数据冻结摘要为 `117477e5cfc2573a132f4efa6f1aabd9dce76f78b22ac5690e700f09cf368a4f`；评价器冻结摘要为 `0d1fa45604f8576f2ffe701eedcdb785929a5607e460dd8cd857166bb2a21d99`。评价器在任何模型运行前单独修正过 Python 数字 JSON 比较的兼容问题并再次冻结；原始数据和评分冻结保留，没有利用模型结果修改判分。

| 材料 | 内容与边界 |
| --- | --- |
| `reports/13-verify-agent/20261117-capability-binding/implementation-summary.json` | 八次运行的脱敏身份、每次调用用量、动作、全部修正/绑定缺项、预算及证据路径/SHA；不含完整模型请求或凭据。 |
| `reports/13-verify-agent/20261117-capability-binding/holdout-evaluation.json` | 独立业务关系评分、每次未执行原因、冻结及输入隔离检查、稳定性门槛结论。 |
| `reports/13-verify-agent/20261117-capability-binding/resource-cleanup.json` | 冻结复核和验证数据库精确停止记录。 |
| `artifacts/verify-agent/20261117-binding-{a,b,c}-seed{19,31}/full-evidence.json` | 本地完整账本，保留原请求/回复与事件；不提交 Git。 |
| `artifacts/verify-agent/20261117-binding-holdout-prepared/` | 原始 B/C 仓库、模型材料与隔离评价材料；不提交 Git。 |

八次运行各自启动的 Graph、Runtime 和 Ollama 进程均已退出或由所有者停止。专用 PostgreSQL 容器按完整 ID 和 owner 标签核验后停止，恢复本轮开始前的停止状态，保留数据库账本。没有改动其他业务系统资源。本轮未修改 `docs/**`、README、原 YAML 目录或被测业务源码；`reports/` 仅记录本轮实现和实测事实。

## 13. 当前结论与下一轮实施重点

接口绑定、有限修正和账本链路已具备可验证实现；当前模型配置尚未稳定形成可判定计划，**真实 Runtime 实验的前置条件未满足**。不能因为工程测试通过、JSON 合法或系统能安全拒绝就宣称 Agent 已具备业务验证能力。

下一轮应围绕通用的关系推导步骤改进规划器：先从 diff 识别具体条件变化及适用要求，再读取对应规则，明确主实验与对照需要改变的事实，最后编译真实输入/输出。语义保持也必须有明确推导，不能默认所有 diff 都有 Bug。当前 Mistral 在这项任务上的能力适配需要单独验证，不能只增加输出栏目或放宽准入。以上是后续建议，本轮没有再改代码或更换模型。

本次 A/B/C 已公开为回归材料，后续不能继续当未见任务。下一次改进后仍须另准备隔离变化、事先固定门槛并完整报告结果。只有计划稳定性门槛通过，才进入第 11 节所列的隔离 Runner 与真实结果回流：同一个 Graph 根据 Runtime 可信执行回执继续决策，而不是消费模型猜测或手工补齐的结果。
