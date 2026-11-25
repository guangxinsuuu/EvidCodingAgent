# Test Agent：变化审查 ReAct 循环实施报告

日期：2026-11-26（Australia/Sydney）。本轮范围：正式本地审查委托、变化影响假设、受控证据调查、主图更新方向、实验草案编译与主图接受。保留现有 Runtime、PostgreSQL 和预算/恢复协议。

**交付判断：工程循环已经实现；真实模型选测验收尚未通过。** 初轮两个隔离样本没有进入调查；通用策略修正后的两个新样本均能读取材料、提出草案，但没有接受计划。两轮的失败和成本全部保留，不能据此接入业务执行并宣称 Agent 已能选择高价值实验。

## 交付边界

已实现的是一个共享模型决策者的可恢复循环。四个新 LangGraph 分支是可追踪的职责交接节点，**不是四个独立模型 Agent**。模型调用仍由 Runtime 登记、预留并派发；分支不直接调用来源、执行代码或签发 PASS。

```text
ReviewRequest v1（可信本地操作员）
 → assemble_review_request：准确 B/C、真实 diff、固定规则和来源目录
 → ReleaseBundle v2 → Runtime.acceptRun：冻结请求、材料、预算
 → decide_next_action：未决问题 + 本轮动作 + 条件结果分支
   ├─ UPDATE_DIRECTION → analyze_change_impact / update_review_direction
   ├─ READ → investigate_evidence → Runtime 原有受控材料读取
   ├─ PLAN_DRAFT → plan_experiment → compilePlanDraft
   │    ├─ NEEDS_REVISION：列出草案字段缺项，有限修正
   │    └─ BOUND_PLAN：保持 ACTIVE，返回主图继续决定
   └─ ACCEPT_PLAN → 精确草案及依据核对 → SCOPED_PLAN_ACCEPTED
```

`ACCEPT_PLAN` 只结束本项规划切片；不表示独立语义判定、候选执行、全要求覆盖或发布资格。其他要求通过 `remaining_requirement_refs` 明确保留。当前 `current_candidate_coverage=[]`、Final 不合格。

## 代码入口与职责

| 文件 | 主要职责 |
| --- | --- |
| `protocols/review-request/v1/` | 请求协议及跨语言正负例；准确版本、新增/保留要求、本地来源状态、缺项、前驱身份 |
| `packages/contracts/src/review-request.ts` | 请求严格解析；不接受公司批准或执行授权字段，不隐式补默认规则 |
| `tools/registry/review_request.py` | 有界普通文件读取；拒绝符号链接、FIFO、重复 JSON 键及非有限数 |
| `tools/registry/release_assembler.py::assemble_review_request` | 复用 Git 读取和固定目录；空/未接入要求保留缺项，新入口不默认为 R1–R6 |
| `protocols/verify-agent/v3/`、`review-decision.ts` | 独立 v3 动作协议；v1 规划和 v2 补证协议保留原含义 |
| `packages/verify-runtime/src/review-profile.ts` | 上下文、动态输出约束、领域证据准则及独立动作语义准入 |
| `packages/verify-runtime/src/review-planning.ts` | `assessReviewBasis` 局部缺项评估；`compilePlanDraft` 机械编译并复用原质量/能力绑定器 |
| `packages/verify-runtime/src/runtime.ts` | 事务登记、请求 revision 冲突检查、工作产物、草案修正、主图接受及成本留账 |
| `services/verify-agent/src/verify_agent/graph.py` | 同一 ReAct 图上的影响、调查、规划、方向更新分支；重建 Runtime 投影恢复 |
| `tools/verify-agent-review-eval.py` | 独立新样本准备、冻结、接受链核对及局部函数离线见证 |

必要中文注释说明了入口、调用关系、状态、预算与失败语义。没有修改 `docs/**` 或项目 README。

## 核心决策与守门

1. `decision_frame` 保存简短审查工作产物：未决问题及证据不足之处、选定问题、至少两个可区分的结果分支。不是隐藏推理记录。Runtime 检查问题身份、已观察引用和动作范围；业务推理正确性仍需要独立评价。
2. Impact 必须留下引用实际 diff 的假设。源码/需求文字是资料，不能覆盖系统指令。假设更新始终标作模型提案。
3. 材料目录、读窗口、输入字段与输出字段由真实合同派生，减少猜工具名和字段；模型仍自行选择资料、业务输入、对照及预期。
4. `CONFIRMED_LOCAL` 只表示本地实验声明，不升级为公司规则确认。冲突、未确认和材料错配只阻止相关要求；无要求或全局缺项阻止检查规划，仍允许阅读/澄清。
5. 编译器只补能力、阶段、来源、观察 ID 等机械字段；不得替模型填入业务值或标准答案。本版草案采用一个观察、一个要求和因素对照；B/C 预测与规范断言分开。
6. 新草案经原有 `assessCheckQuality` 和 `assessCheckBinding` 检查。通过只产生 `BOUND_PLAN`，不自动结束；主图可以补读、修订或接受。
7. 新证据或假设变化使草案依据失效，旧 ref/digest 不能直接接受。同请求 revision 的完整冻结包发生改变会拒绝；补材料使用新 revision/关联后继 Run，旧进度不继承。
8. 草案修正池包含在总调用预算内；有效/无效输出、修正中继续 READ 都计费。原提案、反馈与操作回执保留。UNKNOWN 不盲目重发。

## 工程验证

| 检查 | 结果 |
| --- | --- |
| TypeScript build / typecheck | 通过 |
| Runtime + 共享协议（真实专用 PostgreSQL；模型为固定 Provider stub） | 修正后 348/348 通过，0 skipped |
| Python Graph / 协议 / 恢复测试 | 38/38 通过 |
| 正式请求与新旧装配定向测试 | 32/32 通过 |
| 离线评估器测试 | 修正后 9/9 通过 |
| Registry 全套 | 52/53 通过；一项既有来源漂移失败，见下文 |
| diff whitespace | 通过 |

348 项中的固定 Provider 响应验证工程协议，不证明真实模型会选测。覆盖主图接受前保持 ACTIVE、缺项/冲突、未读依据、草案失效、revision 冲突、重启、并发重复提交和有限修正等边界。

Registry 的既有 `test_real_catalog_matches_pinned_sources_and_history` 报 `SOURCE_CONTENT_DRIFT`：`tools/multi-system/release_seed.py` 和 `test_release_seed.py` 在本轮前的 HEAD 已不匹配固定 provenance。本轮没有改这两个文件，也没有修改冻结来源目录掩盖失败。

## 真实模型与独立验收

初轮真实调用、原始响应、成本、提案及工具回执保留在私有 `artifacts/verify-agent/20261126-review-*` 中，不提交原始实验数据或凭据。

初轮结果：

| 运行 | 真实模型 | 动作与停止原因 | 计划结果 |
| --- | --- | --- | --- |
| 公开开发样本 | qwen2.5:14b | 2次方向更新、4次READ；DISCOVERY_BUDGET_EXHAUSTED | 无草案、无接受；输入49066/输出3717 tokens |
| 新隔离 review_a | qwen2.5:7b | UPDATE_DIRECTION × 3；NO_PROGRESS_LIMIT | 0 READ、0草案、0接受；独立见证NOT_RUN |
| 新隔离 review_b | qwen2.5:7b | UPDATE_DIRECTION × 3；NO_PROGRESS_LIMIT | 0 READ、0草案、0接受；独立见证NOT_RUN |

7B 两例合计输入35015、输出3036 tokens。所有模型调用COMPLETED，没有解析或动作准入错误；系统正确截停重复方向更新，但模型尚未完成 Evidence→Planner 迁移，**有效选测能力未达标**。三次运行均一次执行，未用参考输入补齐、未失败后换模型。14B开发模型与7B隔离模型在隔离调用前按本机资源情况分别确定，不作前后准确率比较。公开汇总见 `model-evaluation.json`。

隔离评估使用两个新本地变化。参考实验在模型运行前预登记，标准答案不进入模型输入；实现、Prompt、Graph、装配器与构建模块全部冻结。只有经过实际 `ACCEPT_PLAN` 的草案才可评估，不能挑选未接受草案或用参考输入替换模型输入。

离线见证仅执行固定受审合成源码的局部纯函数，不是通用候选 Runner，也不回填 Runtime 覆盖/Final。新样本在本轮结束后成为已公开回归材料，不能再次当成未见任务。

## 针对初轮失败的通用策略修正

初轮实现和失败证据已作为 `c8d2c4d` 保存，后续修正不覆盖原结果。

- 新增 `reviewObservationBasis()`，只把真实 READ 回执及确定性编译反馈视为可以重新更新方向的依据。换措辞、重写假设和增加调用次数不能重新开放 UPDATE_DIRECTION；Provider grammar 和 Runtime 接收端同时守门。
- 总预算分为 DISCOVERY、CHECK_REPAIR 和 REVIEW_CLOSE。最后一池固定预留一次，只能在探索耗尽且已有当前依据的绑定草案时开启；不能用于继续读取、更新方向或新建草案。
- 最后探索轮已经读完适用规则时，保留 PLAN_DRAFT／CLARIFY／STOP／NOVEL_CHECK 出口，关闭继续 READ／UPDATE。缺材料时可以明确退出，不由系统补齐业务答案。
- 预留次数和已结算次数分开。预留修正或主图审阅不会改变本次调用的 context_ref。审阅输出 INVALID、UNKNOWN 或越界也保留预算与原件，不自动补发。
- 精确验证总额5次能够容纳3探索＋1修正＋1审阅；真实PG测试还覆盖审阅越界READ、无效输出及未知结果恢复。

初轮a/b现已成为回归材料。修正后的独立验收改用新的 followup suite（review_c/review_d），保持相同7B模型、seed19、8次总调用、4次READ和2次修正；其中主图审阅1次来自总额度，未额外增加调用。新套件在模型运行前冻结代码、Prompt、输入与参考见证，结果另存，不与初轮样本混为同一准确率。

### 修正后的真实结果

| 项目 | review_c | review_d |
| --- | --- | --- |
| 实际动作 | UPDATE_DIRECTION → READ → UPDATE_DIRECTION → READ → PLAN_DRAFT → READ → READ | 相同 |
| 模型 / 读取 | 7/8 次模型调用，4/4 次读取 | 相同 |
| 分池 | 探索5/5；修正2/2，均用于READ；主图审阅0/1 | 相同 |
| 草案实际准入反馈 | DRAFT_REQUIREMENT_SCOPE_MISMATCH，`/payload/draft/requirement_ref` | 相同 |
| 停止 / 接受 | CHECK_REPAIR_LIMIT；0个接受计划 | 相同 |
| 独立离线见证 | NOT_RUN / NO_ACCEPTED_PLAN | 相同 |

两份草案的外层 `requirement_refs` 都带了R1–R6，内部却分别只选R6、R4。Runtime按单要求草案协议拒绝，模型没有按反馈修订草案，而是消耗两次修正调用继续READ。预留的主图审阅只能接受已有、有效、当前依据的BOUND_PLAN；本轮没有这样的计划，所以不能借用最后一池继续修正。

另外，两次运行结束后的**只读代码/草案复核**发现：C没有设置diff涉及的业务因素，主实验与因素对照完全相同；D在同一主实验输入中重复填写同一字段的两个值；两例都声明SHOULD_PRESERVE，却给出不同的B/C预测。这些是事后检查记录，**不是Runtime已经返回的质量错误，也不是候选执行结果**。本轮编译器在范围错误处返回，尚未继续质量/绑定检查；不能把失败归结为“只差一个枚举或引用”。

因此，本轮能确认模型开始按真实读取回执推进；不能确认其已经理解变化、设计出有区分力的实验，或具备稳定修正能力。公开脱敏账本摘要、模型身份、token和冻结摘要见 `model-evaluation-followup.json`。修正后的两个样本从此仅作为开发回归材料，不能再次作为未见任务。

### 后续应聚焦的验收缺口

优先改进同一Agent循环内的草案修正，而非扩大组件数量：让模型正确区分任务的全部义务与当前草案的一项要求；在反馈后真正修改实验；同时核对变化因素、主/对照输入和“保留行为”预测的一致性。编译器应继续只做机械绑定，不补标准业务取值。计划被实际接受后，再原样执行独立见证；稳定形成有效计划之前，不将本轮接成真实业务Runner或Final门禁。

## 当前未交付范围

- 企业 PR/CI/Jira 的自动入口、真实部署基线解析、企业 SSO/来源 ACL、公司规则批准；现阶段通过本地操作员请求与固定 Git/目录接入。
- 通用企业 MCP 来源发现和 Project 1 实际交接；既有归档 MCP 与补证流程保持原义。
- Planner/Evidence/Diagnosis 各自独立模型子图、完整异常诊断分支。
- Graph 驱动真实 B/C 业务执行、任意候选隔离、独立 Final、Codex 修复与发布。
- 模型稳定选测能力与总体准确率。少量本地样本不能用于这些声明；原先 0/6 的历史评测仍保留。
