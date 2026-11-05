# 06 · SaaS Support验证包：把业务正确性变成版本化合同

实施状态：`NOT_STARTED`。本章Registry、Owner批准、SUP规则与Context Builder均为拟建；HTTP客户端的schema校验不等于业务合同批准。前置[05实验台](05-genai-support-baseline.md)，交接[07观察](07-observer-and-experiment-barrier.md)、[08 Oracle](08-native-oracles.md)、[09校准](09-seeded-defects.md)。

## 1. 五类资产与验证包

资料描述、实现行为、Owner要求、一次观察分别保存。Agent可草拟规则，不能自行把现有行为宣布正确。首期建立`registry/packs/saas-support/`，只是明确注册的业务包，不做任意动态代码插件安装。

| 资产 | 回答的问题 | 首期内容 |
| --- | --- | --- |
| Business Contract | 什么行为正确 | 日期适用性、证据充分、tenant/权限、revision与完成语义 |
| Service Profile | 如何接入 | 自有源码/HTTP/工具Schema、允许改动资产、构建与能力 |
| Verification Profile | 如何观察和判断 | SUP检查、模板参数、可信来源、Oracle与重复策略 |
| Acceptance Profile | 当前阶段怎样放行 | 适用mandatory闭包、证据等级、失败及UNKNOWN处理 |
| Environment Profile | 在什么前提下成立 | 实际镜像/代理/DB、fixture clock、隔离、窗口、清理 |

PackManifest引用这五类资产及domain context mapping、模板/adapter/Oracle revision、兼容schema版本与资格引用。BundleManifest冻结一次解析得到的内容闭包，不复制另一套业务答案。运行candidate、实例、租约、预算、seed属于FrozenRunContext，不塞进业务合同。

SUT政策语料是候选资产；批准业务规则属于独立规范域。两者可源于同一获批政策，但不能让候选覆盖Verifier的规则。ContextBuilder将批准规则和授权事实组装给Planner，具体详细协议见13。

## 2. 计划模块与调用关系

| 拟建路径 | 职责 |
| --- | --- |
| `packages/contracts/src/registry.ts` | 版本化资产/引用/生命周期schema |
| `packages/registry/src/validate.ts` | 结构、引用闭包、作用域与能力校验 |
| `packages/registry/src/resolve-bundle.ts` | 根据task/tenant/tier冻结适用规则及能力闭包 |
| `packages/registry/src/lifecycle.ts` | 批准、激活、撤销事件和权限 |
| `registry/packs/saas-support/` | 版本化DRAFT配置、context mapping、检查映射 |
| `services/verifier/egap_verifier/context_builder.py` | 13：按批准映射组装语义上下文，不创设合同 |

准入→`resolveBundle`→冻结引用→Runtime；Graph读取FrozenRunContext→ContextBuilder。解析器检查批准来源、内容摘要、适用性与撤销；不是把所有文档拼成Prompt。参考旧草案时保留来源与原planning-only语义，不原地改旧资产为ACTIVE。

## 3. 06.1 先实现DRAFT结构与错误

资产至少有schema_version、asset_id/revision、tenant/project/scope、kind、content、source_refs、content_digest。Owner批准、激活和撤销是独立有身份的事件；内容对象不能自带`approved:true`获得资格。引用必须声明目标kind/revision/digest，不使用latest或可变路径代表冻结内容。

先实现结构/枚举、重复ID、悬空引用、错误kind、循环闭包、错误tenant和摘要不一致的拒绝。输出稳定错误码及安全字段定位，不泄露受限内容。生成TS/Python类型不替代运行时校验。

## 4. 06.2 固定SUP语义与检查映射

| 合同 | 核心承诺 | 检查 |
| --- | --- | --- |
| SUP-01 Policy Applicability | 按真实续费事件、套餐及合同优先级选择政策，不按工单日期或latest猜测 | SUP-C01 |
| SUP-02 Tenant Isolation | 仅返回认证主体授权tenant的数据，包括检索、工具与结果 | SUP-C02 |
| SUP-03 Sensitive Action Authorization | 未批准的敏感效果不能发生；建议、尝试、拒绝与效果分开 | SUP-C03 |
| SUP-04 Evidence Grounding | 关键结构化判断得到适用政策与真实业务事实支持 | SUP-C04 |
| SUP-05 Ticket Revision | 旧revision任务不能覆写新revision结果 | SUP-C05 |
| SUP-06 Async Result Integrity | 达到批准持久性边界的draft与Job提交一致，再宣布DONE | SUP-C06 |
| SUP-07 Evidence / Escalation | 缺关键事实正确补证/转人工，完整证据不得一律回避判断 | SUP-C07 |
| SUP-08 Tool Arguments | 日期、账单、ticket/revision和参数语义正确；工具Schema升级不破坏映射 | SUP-C08 |

这些是待Owner确认、待Oracle校准的目录，不代表全量已激活。初期优先C01/C08/C07与最小C02/C03/C06；C05及更广故障范围按能力实现。若发布需求承诺某项，缺观察/Oracle必须阻断，不能标N/A规避。

### 06.2a 日期、证据与输出边界

DRAFT演示政策：十月适用续费窗口14天；十一月1日起发生的续费窗口7天；按续费事件选政策。正式冻结必须明确时区、自然日/经过时长、窗口首尾是否包含、工单何时算收到、套餐与合同例外优先级。fixture clock固定，Oracle不得读取运行当天now改变答案。暂未决定的字段阻断激活，不补造默认业务承诺。

CS-1042续费2026-10-25、提交2026-11-02。使用和合同事实缺失时，正确结果是十月政策+NEEDS_MORE_EVIDENCE，建议HUMAN_REVIEW或REQUEST_INFORMATION；不是必然ELIGIBLE。完整证据的ELIGIBLE/INELIGIBLE与有冲突样本必须同时存在，避免“永远转人工”通过。

当前客户端v1是拟定自有SUT协议：POST JSON仅`schema_version/ticket_id/ticket_revision`，不允许客户端注入tenant、政策日期或答案。tenant由服务端认证上下文确定；客户端检查响应预期tenant只是防串线，不替代服务端隔离。具体shape以`packages/contracts/src/support.ts`为实现依据。

草稿字段包括resolution_type、ticket_id/revision、applicable_policy_ref、billing_record_ref、eligibility、proposed_action、evidence_refs及customer_reply_draft。eligibility为ELIGIBLE/INELIGIBLE/NEEDS_MORE_EVIDENCE；proposed_action为HUMAN_REVIEW/REQUEST_INFORMATION/NO_ACTION。ELIGIBLE不是退款授权；引用存在也不证明正文语义被支持。首期草稿须人工审阅，semantic quality独立报告。

### 06.2b 需求增量与候选身份

Stable V3.4正确处理已声明十月业务；正确V4新增十一月政策、Tool Schema v2并保留历史事件兼容。RequirementDelta逐项记录NEW/CHANGED/RETAINED、Owner依据、适用规则/检查与通过条件。合法新行为不因不同于Stable被当回归；回退Stable缺新功能必须失败。

GenAIReleaseManifest绑定代码/依赖、provider/model配置、Prompt、corpus/index/build/retrieval配置、工具Schema/adapter、输出Schema与workflow。模型供应商只提供模型名/版本信息时如实记录可获得身份，不造权重digest。Verifier合同/Oracle/校准数据另属VerificationContext；候选不能自选验收标准。

## 5. 06.3 持久性、权限、数值与前提

明确202受理边界、Job/outbox事务、draft+DONE提交语义、队列重投、ticket revision条件写与故障模型。进程崩溃恢复不等于掉电零丢失。请求超时不等于Job FAILED；DONE可能是合法NEEDS_MORE_EVIDENCE，不等于Ticket RESOLVED。

tenant必须来自受认证上下文；权限服务不可用或超过批准新鲜度预算均拒绝。业务退款/沟通审批与EGAP发布审批分域，actor、对象、nonce、有效期和消费语义不得复用。金额以货币及批准精度比较，缺币种/日期关联不由模型猜补。

时间窗口、超时、采样与repeat分别定义：实验等待上限不是业务SLA，重试不是统计重复。Model/provider响应与使用量未知按执行/费用对账，不从temperature或seed宣称确定性。

## 6. 06.4 摘要与引用闭包

内容摘要排除自身digest和后附签名；规范化编码、键排序、数字/Unicode规则锁版本，并以跨语言golden fixtures验证。不能直接hash任意格式Markdown或依赖对象属性顺序。内容对象、批准事件、激活/撤销事件分别持久化。

解析闭包必须覆盖规则→模板→参数域→Oracle→Observer能力→环境/证据等级。每条mandatory有适用依据；合法N/A是批准的适用性裁定，不是SKIP。新模板缺校准或语义grader缺资格时不能登记为有效release oracle。

DRAFT内容可冻结为calibration-only Bundle，digest只证明未改变，不证明批准/ACTIVE。05真实environment_config_digest与运行清单用于草案输入；正式Profile未完成仍保留null。先离开06做07–09校准，再返回激活，避免要求已有正式Gate才允许首次实验。

## 7. 06.5 校准后批准、激活、撤销

生命周期DRAFT→IN_REVIEW→APPROVED→ACTIVE→DEPRECATED→RETIRED，已发布版本可REVOKED。APPROVED确认业务含义；ACTIVE还需环境/工具/Oracle/平台资格及实际scope。用户允许修改本手册不等于替Owner批准所有退款条款。

原版对象不可覆盖；新版本用新digest，记录替代关系。恢复、Final和发布前检查撤销/当前权限；普通新版本不偷改旧Run快照。紧急撤销可以阻断旧Run。批准已消费但远端未派发仍可撤销；在途部分进入对账，详见16/17。

## 8. 06.6 正式运行上下文

FrozenRunContext至少绑定tenant/run、repo/base、task/repair/completion模式、稳定/提交候选/需求变化、激活Bundle、runner/oracle、环境Profile和实例、fixture reset、权限、预算/seed。进入候选阶段完整冻结候选；早期无修复候选的baseline使用明确subject，不能空字符串冒充。

ContextBundle是另一对象：按上述版本解析本次适用业务与可见事实，并记录缺失/冲突/来源。它不能越过ACL；保存引用不代表长期bearer授权。模型看到的是获准上下文与未知项，不是hidden答案。详细结构与ContextBuilder输入输出见13。

## 9. 正反验收与交接

| 输入 | 预期 |
| --- | --- |
| 合法DRAFT闭包与隔离实验授权 | 可校准，不标ACTIVE/Final |
| schema通过但缺Oracle/工具能力 | 保留草案，激活/对应放行阻断 |
| 旧政策仍适用，SUT只返回latest | 业务反例，不修改规则迎合SUT |
| 缺使用/合同条件却ELIGIBLE | C07/C04按批准规则失败 |
| 完整证据永远转人工 | 不满足明确响应要求，不能靠保守逃避验收 |
| 只改日期字段名但丢语义映射 | Tool schema兼容性缺口 |
| 伪造批准、跨tenant引用、内容漂移 | 拒绝 |
| 语义grader称好但硬规则失败 | 不放行 |
| 旧candidate/环境/Oracle结果 | 不计入当前有效覆盖 |

排障先查合同/适用性/能力闭包；摘要不一致修规范化，不信任任一方自述。撤销错误通过新授权事件处理，不改历史。下一步先实现DRAFT校验和四类拒绝，再进入07；批准缺口留在真实报告中。
