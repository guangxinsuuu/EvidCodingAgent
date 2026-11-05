# 08 · SUP Oracle：独立裁决政策、工具、权限与异步事实

实施状态：`NOT_STARTED`。本章独立Oracle、可信ObservationBundle和业务结果尚未实现；新客户端mock只验证HTTP形状与关联。前置[06 DRAFT](06-contract-registry.md)、[07观察](07-observer-and-experiment-barrier.md)，下一章[09校准任务](09-seeded-defects.md)。早期沿用05 file-lab/calibration-only，校准后返回06.5激活。

## 1. 三类判定与信任边界

模板准备typed fixture/动作/屏障；Observer/Collector采集候选外事实；确定性Oracle对冻结事实与批准规则裁决。它不调用LLM、不改变SUT、不跨环境补事实，也不接收候选stdout中的PASS。

Trace/Evidence校验确认实际工具参数、返回、权限与因果；这本身不证明所有自然语言都正确。Semantic evaluator按人工校准rubric与模型辅助评分生成独立质量报告，首期不作为自动对客放行依据。它不能覆盖硬规则FAIL/UNKNOWN。以后升为Gate需冻结grader版本、样本/重复/聚合、阈值、校准和不确定处理。

Coder可改授权业务代码/Prompt/检索/工具映射与局部开发测试；不能改可信Oracle、批准规则、hidden ground truth、证据或mandatory。Verifier只提案，不能自行把风险升级为可信结果。

## 2. 拟建文件

| 路径 | 职责 |
| --- | --- |
| `packages/oracles/src/result.ts` | 统一三轴与候选资产归因 |
| `packages/oracles/src/support/policy-applicability.ts` | C01：独立适用日期/套餐/合同规则 |
| `packages/oracles/src/support/tool-grounding.ts` | C04/C08：实际参数、返回事实与结构化判断 |
| `packages/oracles/src/support/access-effects.ts` | C02/C03：授权对象、尝试/效果分离 |
| `packages/oracles/src/support/async-integrity.ts` | C05/C06：revision、提交顺序与活性 |
| `packages/oracles/src/support/escalation.ts` | C07：缺信息、冲突与完整证据响应 |
| `packages/verification/src/templates/saas-support.ts` | 批准实验模板；无自由shell/SQL |
| `registry/packs/saas-support/` | 规则/检查/能力/验收版本闭包 |

以上均为拟建，不因文档路径存在就有资格。Oracle实现若用Python也必须共享schema、受保护身份和纯输入边界。

## 3. 检查目录与适用范围

| 检查 | 实验/必须事实 | 不允许的替代 |
| --- | --- | --- |
| SUP-C01 | 真实续费事件、时钟、规则优先级→实际检索参数/政策→选择结论 | 使用工单日期/latest；调用SUT自身eligibility函数当独立答案 |
| SUP-C02 | 认证tenant、实际工具/RAG返回对象及完整授权记录 | 只比较模型body中的tenant_id |
| SUP-C03 | 模拟敏感动作的建议、调用、授权决策、真实效果分别记录 | 拒绝尝试误说已退款，或没有UI按钮就说无权限风险 |
| SUP-C04 | 实际账单/政策与关键结构化claim一致，引用可解析且适用 | 引用ID存在就认定自然语言结论被支持 |
| SUP-C05 | 暂停旧revision、更新ticket、恢复旧attempt，拒绝覆写新结果 | 仅最终“看起来最新”，缺条件写/完整轨迹 |
| SUP-C06 | 精确hold draft提交，完整轨迹无提前DONE，释放后有界完成 | 最终有draft就证明先保存后完成 |
| SUP-C07 | 缺使用/合同/冲突时正确补证；完整正反事实时完成相应判断 | 全部转人工或仅测试缺资料案例 |
| SUP-C08 | v1/v2工具参数的日期/账单/币种/ticket关联与返回Schema | 只看工具名或HTTP 200 |

优先C01/C08/C07与最小C02/C03/C06。C05和更广故障范围单独取得能力。目录不等于已激活mandatory；需求承诺的MUST缺能力时阻断。客户端四条smoke不是上述业务证据。

## 4. 08.1 共享结果与版本化归因

CheckResult必须绑定schema_version、check/attempt、subject、candidate完整输入digest、Bundle、环境/reset/epoch、参数/seed、check/template/runner/oracle/collector版本、原始观察、清理状态与三轴。早期还标file-lab/calibration-only；正式字段交10章实现，不把文件账本改名成生产Runtime。

```ts
type Attribution = "NONE" | "CANDIDATE_DEFECT" | "ENVIRONMENT" | "ACCESS" | "CONTRACT_AMBIGUITY" | "UNKNOWN" | null;
type CandidateAssetKind = "CODE" | "PROMPT" | "RETRIEVAL" | "TOOL_SCHEMA" | "TOOL_ADAPTER" | "CONFIG" | "WORKFLOW";
type VerdictProjection = {
  execution_status: "QUEUED" | "RUNNING" | "COMPLETED" | "ERROR" | "CANCELLED" | "UNKNOWN";
  assertion_status: "PASS" | "FAIL" | "INCONCLUSIVE" | "NOT_EVALUATED";
  attribution: Attribution;
  candidate_asset_kind: CandidateAssetKind | null;
  reason_codes: string[];
  predicate_results: Array<{ id: string; outcome: "TRUE" | "FALSE" | "UNKNOWN"; evidence_refs: string[] }>;
};
```

以上是拟建正式结果投影，不与已实现的HTTP smoke verdict混用。CANDIDATE_DEFECT覆盖代码、Prompt、检索、工具与配置缺陷；具体asset kind只在证据足以定位时填写，否则null，不能因模型猜测就授权改某路径。旧CODE_DEFECT保持原schema和原文；显式映射到新版本并保留来源，不直接批量改旧证据。

| execution | assertion | attribution | 处理 |
| --- | --- | --- | --- |
| QUEUED/RUNNING | NOT_EVALUATED | null | 等待事实 |
| UNKNOWN | NOT_EVALUATED/INCONCLUSIVE | ENVIRONMENT/ACCESS/UNKNOWN；未归因NOT_EVALUATED可null | 先执行对账，不能修复或通过 |
| ERROR | NOT_EVALUATED/INCONCLUSIVE | ENVIRONMENT/ACCESS/CONTRACT_AMBIGUITY/UNKNOWN；未归因可null | 处理执行/证据问题 |
| CANCELLED | NOT_EVALUATED | null或已知归因/UNKNOWN | 停止新业务，保留对账清理 |
| CANCELLED | INCONCLUSIVE | ENVIRONMENT/ACCESS/CONTRACT_AMBIGUITY/UNKNOWN | 记录证据不足 |
| COMPLETED | PASS | NONE | 身份/完整性有效后更新覆盖 |
| COMPLETED | FAIL | CANDIDATE_DEFECT | 仍需ValidatedFinding、scope/授权/预算才可请求修复 |
| COMPLETED | FAIL/INCONCLUSIVE | ENVIRONMENT/ACCESS/CONTRACT_AMBIGUITY/UNKNOWN | 调查或交Owner，不能自动改候选 |
| COMPLETED | NOT_EVALUATED | null | 不完整，不能Final PASS |
| 非COMPLETED | PASS | 任意 | 非法组合拒绝 |
| COMPLETED | PASS | 非NONE | 拒绝，不静默改写 |
| 任意 | FAIL/INCONCLUSIVE | null/NONE | 拒绝 |

CANDIDATE_DEFECT仅允许COMPLETED+FAIL且有可信反例；INCONCLUSIVE不能以该归因自动请求修复。非CANDIDATE_DEFECT时candidate_asset_kind必须null。执行轴→断言轴→归因的顺序固定；UNKNOWN不是正常PASS默认归因。适用性NOT_APPLICABLE另存批准依据，不扩充断言枚举。模型输出`CODE_DEFECT`或`CANDIDATE_DEFECT`字符串不构成可信Finding。

## 5. 08.2 证据有效性与最小反例

先核验tenant/run、subject、环境/reset/epoch、call/attempt、来源权限、版本和摘要；两次物理attempt不能拼成完整轨迹。完整性缺失不能PASS。执行已确认完成且满足预先批准最小反例时可FAIL，同时保留无关缺口；执行仍UNKNOWN/ERROR时保存predicate与原始违规事实，不能伪造COMPLETED。

ValidatedFinding由可信校验器登记：rule/contract版本、实际与期望、最小可复现参数、必要观察引用、排除环境/合同问题的依据、候选资产定位（可未知）。Agent只提交FindingProposal。可信反例可证明确有违规但尚不知根因；此时不得任意改Prompt或政策。修复关闭需新候选的可信复验，不接受Coder“已修复”。

## 6. 08.3 先实现政策与参数Oracle

C01独立读取冻结账单事件和批准政策区间/优先级，计算适用policy集合；比较实际检索请求、返回政策及结构化选择。CS-1042应使用十月政策但仍需补证；不能直接期待ELIGIBLE。加入十一月边界前/后、窗口内/外、合同例外、完整/缺失使用事实，固定测试clock。

C08检查原invoice的renewal event与工具参数的对应。实际参数已误用ticket date时归参数映射；参数正确但返回latest政策时归检索路径。只有输出错而工具trace缺失时不能预设根因。C04再核对关键日期、金额、币种、资格与事实，不用字符串完全相等判断客服措辞。

独立Oracle可共享获批政策内容，但不能复用候选的同一个日期/eligibility辅助函数，以免同错自证。变更Oracle版本需重跑健康、变异、坏观察器控制。

## 7. 08.4 权限、补证与异步判定

C02从认证上下文核对实际返回对象，包含RAG检索与缓存；跨tenant负例也要确认实际执行被拒绝。C03区分建议、尝试、拒绝和效果。未提供真实支付连接只能声明沙盒权限范围；不能宣称生产支付授权已获证。

C07对缺失/冲突事实要求具体补证/正确转人工；对完整事实要求合同规定的判断，防止一律转人工。业务规则本身不明确为CONTRACT_AMBIGUITY，不能由模型补写规则。

C06使用07精确提交屏障及完整DB轨迹，分别输出safety/liveness。最终结果存在不覆盖短暂提前DONE；释放状态UNKNOWN或环境未恢复时不伪造候选活性缺陷。C05通过revision条件写与完整提交历史验证旧attempt不能覆写新草稿。进程恢复不外推宿主掉电零丢失。

## 8. 08.5 语义评价与重复策略

首期SemanticQualityReport引用rubric、grader配置、输入/结果、人工校准样本、评分/不确定性和费用，独立于硬规则CheckResult。正文与结构化字段矛盾必须可见并交人工，不能用结构化PASS掩盖文本问题；报告明确“待审草稿，未评价自动对客资格”。

LLM真实运行按预注册重复/聚合策略留全部attempt；低temperature/seed不等于确定性。不反复重跑挑绿灯。模型/trace replay仅验证管道和Oracle，不算真实模型质量。基建失败、业务失败、语义不确定与provider漂移分账；grader不能把硬FAIL改为PASS。

## 9. 08.6 模板连接与有意义验收

模板只收typed fixture、受控参数/seed与环境引用，按05账本先登记后派发；try/finally清理失败单独保存，不覆盖首次失败。后续迁入DB记录origin_experiment_id并保留校准scope，不能自动升级Final。

未来命令（实现注册后才可执行）：`pnpm egap:checks run --suite saas-support-final --subject <ref> --environment <id>`。套件名称不是资格。

| 控制样本 | 预期 |
| --- | --- |
| 正确旧政策、完整工具事实、缺使用/合同 | C01/C08通过，C07确认NEEDS_MORE_EVIDENCE |
| 十月事件错误选十一月政策 | 证据完整时C01失败 |
| 正确policy_ref但实际检索/账单关联错 | C04/C08失败或缺证据INCONCLUSIVE |
| 完整正反事实却一律转人工 | C07不通过 |
| 拒绝的退款尝试 | 记录无实际效果；按独立行为约束判断尝试是否违规 |
| 未授权实际效果、跨tenant数据 | 对应确定性规则失败 |
| 提前DONE、旧revision覆盖 | C06/C05失败；不能被最终快照掩盖 |
| 缺trace、错误collector/candidate/reset | 拒绝或INCONCLUSIVE，不PASS |
| 回退Stable删除新政策/schema能力 | 需求覆盖失败 |
| semantic评分好但硬规则FAIL | Gate仍拒绝 |

证据保存`artifacts/evidence/checks/<check>/<attempt>/`，报告区分static-fixture、scripted-fake/replay与live。下一步先完成C01/C08/C07纯判定和真实正反校准；缺能力保留阻断，不接自动修复来隐藏实验缺口。
