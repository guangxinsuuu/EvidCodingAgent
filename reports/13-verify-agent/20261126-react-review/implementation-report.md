# Test Agent：变化审查 ReAct 循环实施报告

日期：2026-11-26（Australia/Sydney）。本轮范围：正式本地审查委托、变化影响假设、受控证据调查、主图更新方向、实验草案编译与主图接受。保留现有 Runtime、PostgreSQL 和预算/恢复协议。

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
| Runtime + 共享协议（真实专用 PostgreSQL；模型为固定 Provider stub） | 338/338 通过，0 skipped |
| Python Graph / 协议 / 恢复测试 | 38/38 通过 |
| 正式请求、新旧装配及离线评估器定向测试 | 37/37 通过 |
| Registry 全套 | 52/53 通过；一项既有来源漂移失败，见下文 |
| diff whitespace | 通过 |

338 项中的固定 Provider 响应验证工程协议，不证明真实模型会选测。覆盖主图接受前保持 ACTIVE、缺项/冲突、未读依据、草案失效、revision 冲突、重启、并发重复提交和有限修正等边界。

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

## 当前未交付范围

- 企业 PR/CI/Jira 的自动入口、真实部署基线解析、企业 SSO/来源 ACL、公司规则批准；现阶段通过本地操作员请求与固定 Git/目录接入。
- 通用企业 MCP 来源发现和 Project 1 实际交接；既有归档 MCP 与补证流程保持原义。
- Planner/Evidence/Diagnosis 各自独立模型子图、完整异常诊断分支。
- Graph 驱动真实 B/C 业务执行、任意候选隔离、独立 Final、Codex 修复与发布。
- 模型稳定选测能力与总体准确率。少量本地样本不能用于这些声明；原先 0/6 的历史评测仍保留。
