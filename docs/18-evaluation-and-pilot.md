# 18 · 评估版本验证的收益，并完成限定范围试点

实施状态：`NOT_STARTED`。本章所有数据集、实验、指标与试点报告均为拟建；没有修复成功率、成本、生产资格或统计显著性结果。

前一章：[17 · 发布与 CI](17-publication-and-ci.md)。下一章：[19 · 部署与运营](19-deployment-and-operations.md)属于后续独立增量，不是本章通过后的自动动作。本章使用第 09 章任务集、12–14 章修复与验证、15 章 qualification、17 章发布证据。

## 本章要回答的两个不同问题

第一，面对同一稳定基线、批准的新需求和待发布候选，独立 Verifier 是否在同等资源与测试能力下更快找到真实回归、减少 QA 人工与验收成本，同时保持可接受的漏报/误报水平。第二，当前系统是否已经在声明的业务、环境和权限范围内完成可恢复、可审阅的 native pilot。前者是 Release QA 效果评估，后者是工程验收；单次成功 demo 不能替代任意一种。Finding 后的修复是单列阶段，事故修复模式保留为另一入口。

试点出口允许三种结论：值得扩大；局部场景有收益、限定使用；收益不足、保留简单方案。若真实基线或隔离未达到门槛，可以形成有证据的未通过报告；不能靠增加模型调用、删除失败任务或只展示最好一次来获得绿色结论。

旧 AAA [packages/evaluation/src/index.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/packages/evaluation/src/index.ts>) 已有工具顺序、禁止动作、批准在先、预算和恢复评估思路，可迁移为治理指标。它的 golden payment/Kafka 场景、固定 expectedTools 和历史结果不证明当前 ML 场景的业务正确性。新试点以独立 Oracle 和真实运行 manifest 为依据。

## 拟建资产

| 拟建路径 | 职责 |
|---|---|
| `packages/evaluation/src/dataset.ts` | 任务元数据、版本、split、访问分区。 |
| `packages/evaluation/src/experiment-plan.ts` | 组别、预算、顺序、重复、停止与排除规则。 |
| `packages/evaluation/src/runner.ts` | 提交受限 WorkOrder，跟踪各组真实 Run。 |
| `packages/evaluation/src/metrics.ts` | 从可信结果计算质量、成本、时间和治理指标。 |
| `packages/evaluation/src/report.ts` | 分场景结果、失败分布和推广建议。 |
| `evaluation/plans/` | 已评审实验计划，内容与版本不可事后修改。 |
| `evaluation/datasets/` | 对 Agent 可见的任务元数据，不放隐藏答案。 |
| `tests/integration/evaluation-accounting.spec.ts` | 样本计数、未知用量、失败保留和组别对齐。 |

hidden healthy patch、作为答案的正确 V4 参考实现、注入位置和 holdout 细节由独立评估者存入单独权限域，不能只放在 Agent 可读仓库的另一个目录。授权 stable/candidate 代码与真实 diff 是 Release 任务必要输入，不能因隐藏答案而一起隐藏；正确候选本身仍可读，只是不额外提供 hidden 参考答案。对外报告只展示脱敏任务描述与结果，保留可审计引用。

## 检查点 18.1：冻结任务集和判定标准

主任务集以“稳定版本→有模型/预处理/Worker变化的候选→针对性验证”为单位。首个主展示使用第 09 章自有衍生 Stable V3.4→Candidate V4.0，不是上游真实版本，也不虚构线上运行史。Stable 先补冻结模型/预处理 manifest 与可信观察；V4 增加模型 B、preprocessing/schema v2、批准的 v1 兼容与 Worker 重构。第 06/08 章完成规则批准和正反校准后才能准入真实评测。

正确功能版、late-alias-lookup 错模型 seed、提前成功/结果缺失 seed、预处理/schema 错配 seed分别登记；某种 seed 的检出不能代表其他规则有效。seed 是自有衍生版本的 `SYNTHETIC_MUTATION`，不声称上游自带这些缺陷。任务集还覆盖影响未知、环境失败、权限受限、合同含糊和无需修改控制；事故修复另标 task_mode。重复投递和 Worker 恢复是否参与某个 tier 由已批准 ML-04/05 能力决定，不能一边宣称范围包含恢复、一边删除缺能力检查。

ML-07 模型质量评估与“Verifier发现软件回归的能力”是两个实验。首期只用合成/获准图像数据验证系统协议与版本一致性，不从两张图片、模型输出分数或上游README推出分类准确率。质量评估需单独冻结标注集、切片、泄漏检查、统计方法、性能环境和门槛；不能用旧模型逐值相等要求阻止所有合法模型升级。


每项任务记录 dataset/task revision、source/base、seed provenance、可见 WorkOrder、独立 ground truth 引用、Profile、环境能力、允许路径、任务类别和 split。独立冻结 `task_mode=RELEASE_VERIFICATION|INCIDENT_REPAIR`、`repair_policy=DISABLED|ON_VALIDATED_FINDING`、`completion_mode=FINAL_ONLY|DRAFT_PR_CI`；Release 还需 `stable_release_manifest_ref`、`submitted_candidate_ref`、`release_changeset_ref`、`requirement_delta_ref`、`historical_coverage_refs`。`base_commit_sha/base_input_digest` 固定任务基线，Release 为 stable；后续 Coding 的 `repair_input_candidate_ref` 独立记录，不能改变该比较基线。`synthetic-seed` 明确是人为注入，不指称上游原版存在该缺陷。

先冻结“正确验证”的标准：候选满足批准新需求、适用不变量与兼容约束，才允许认可；真实缺陷应被可信 Finding 揭示并阻止接受；环境/权限/合同不足应正确阻断，不能改业务绕过。在 `DISABLED` 的验证阶段，检出缺陷即正确检测结果，不要求本阶段修复。批准的新行为与 stable 不同不是回归。历史稳定结果只作风险先验，当前候选仍需独立证据；独立结果或人工裁决有争议时保留 `UNRESOLVED`。

`accepted_release` 是本章评估指标：当前候选通过适用独立 Final、任务需求满足、相关未知/清理阻断解决，并与独立 ground truth 一致；它表示声明范围内验收正确，不代表已 Merge/Deploy。外部提交的正确新功能候选相对 stable 为 `CHANGED`，未做修复也可计 accepted_release 和按 mode 成功，必须记 `repair_success=false`，不得计入 NO_CHANGE。

ground truth 先用正确功能版/缺陷版正反控制校准，记录应接受、应检出、应阻断及后续修复目标。`correct_no_change` 只适用于最终完整输入等于任务 base、任务需求满足、独立 Discovery/Final 合格且无未知/清理阻断的真实 UNCHANGED 任务；不能把“没有 Coding”或“修复前后无变化”当作该身份。`repair_success` 只在独立修复阶段判定，要求确有代码缺陷、修复该问题且保留批准新功能、无适用回归并通过当前 Final。仅空 diff、无 Coder 快照或 Run 终态不足以计算任一指标。

应修复却原样返回的修复任务是失败；错误声称 NO_CHANGE 单列 `incorrect_no_change`。Release 验证若漏检并错误接受 CHANGED 候选，则是错误接受/escaped regression，不应硬塞进 no-change 分类。held-out 裁决揭示 Oracle 漏检时，即使运行时曾给出成功/NO_CHANGE，也要记录评估失败和证据缺口，追加纠正记录，不改写历史。对本已正确的新功能候选做无必要改动，另计无效变更，不能算修复收益。

开发集用来改 prompt、模板和代码；held-out 在冻结方案后运行。查看 hidden patch、用评测失败反复调 prompt 后继续把它称为盲测，都会破坏结论；应升 dataset/experiment revision 并明确重新划分用途。

## 检查点 18.2：设计公平的三组对照

以下组别命名为 `EVAL_A/B/C`，与第 13 章 Graph 实验模式 A/B/C 分开：

| 组别 | 策略 | 必须保持的共同条件 |
|---|---|---|
| EVAL_A Fixed Release QA | 按预先批准的固定变更映射/测试计划选检查，再确定性 Final。 | 相同 stable、submitted candidate、批准需求、可读 diff 与历史资料。 |
| EVAL_B Codex + Business Tools | Codex 以只读验证角色分析变化，使用相同业务实验/观察工具选择检查，再 Final。 | 相同模板能力、观察权限、参数边界和 mandatory，不故意削弱基线。 |
| EVAL_C Specialized Verifier | 第 13 章独立 change impact、风险/覆盖映射和自适应实验规划，再 Final。 | 相同总费用、墙钟、检查能力与 mandatory；所有模型、通信、环境、实验/重试计费。 |

主对照统一为 `RELEASE_VERIFICATION + DISABLED + FINAL_ONLY`，CODING 配额为 0。三组在验证阶段始终面对同一冻结候选；B 的 Codex 是只读 Planner，不能走第 12 章写入工作区。B/C 的探索规划模型调用按用途记 DISCOVERY，不能因为进程名是 Codex 就占 CODING。发现 Finding 后保留验证结果和相同起点，不让某组先修复、再用更干净候选继续与其他组比较。确需评估修复时，另建明确授权的 WorkOrder/Run，关联原 Finding 和候选，冻结 `repair_policy=ON_VALIDATED_FINDING`；三组采用相同修复能力/预算规则，成本与时间单列，不回填主验证成绩。

主评估比较角色分工与策略，不把不同模型性能混成架构收益。固定各角色实际 model/config/CLI/Graph/Oracle 版本；若研究异构模型，另列实验因素，并保持可解释的资源约束。

每次 reservation/usage 统一使用 `budget_pool=CODING|DISCOVERY|FINAL|RECONCILIATION|CLEANUP`；仅 FINAL reservation 必填 `final_purpose=MANDATORY|DELIVERY`，其余池不得填。Final 强制验收的 MANDATORY 与交付的 DELIVERY 子用途分别保护，发布/CI 不能挤占验收；对账/清理资源按第 11 章预留。三组总上限、分池/子用途规则和可调整范围在实验计划中固定，不能给 C 隐藏的恢复或额外实验额度。不能让 C 多跑一倍容器却只比较 token，也不能让 A 因不具备必要工具而必然失败。每组使用干净可重建的 fixture 和独立环境实例，避免上一组留下队列pending项、模型缓存、结果记录或故障屏障影响下一组。

只有当前阶段实际必需的探索池耗尽才触发收尾；验证阶段的零 CODING 配额不是停止理由。触发时先原子记录 `exploration_closed=true` 并关闭探索派发；仅在当前候选满足第 14 章 Final readiness（送验条件，非 Final PASS）、没有当前已知代码 Finding/阻断未知、FINAL/MANDATORY 及相关恢复/清理预留和总截止时间足够时，通过 `FINAL_DRAIN_READY` 事件进入受限 FINAL_VERIFYING。可有界确认的未知先按第 11 章对账；决策截止仍不满足则 STOPPED，保留未完成结果。STOPPED 后不恢复 Coding/Discovery/Final，仅对账与清理或另建明确任务。三组采用同一规则，分别报告“探索耗尽后完成验收”“有效缺陷阻止验收”与“其他条件不足而停止”，不能选择性删除后者。

先做少量协议校准，不宣称统计效果。正式 plan 预先规定样本、重复、随机顺序/配对、预算、停止条件、基础设施失败处理和分析方法；不许看到结果后只增加对 C 有利的重复。具体样本量由目标差异、变异和可用预算确定，本文不编造固定次数能证明显著性的承诺。

## 检查点 18.3：把资源和异常也纳入结果

实验 Runner 只提交已批准范围的 WorkOrder，不直接调用模型或绕过 Runtime 发检查。每个 evaluation attempt 关联 run_id、策略 revision、预算 reservation、环境实例和全部 physical attempts。模型调用未知、进程超时、恢复及清理仍计入事实，不能被结果导出器过滤掉。

对于基础设施失败，保留全样本结果，并按预先规则另外报告可评估子集。不能把 C 的失败归“环境不算”，而把 A 的同类失败计入修复失败。若重跑，应保留原始记录、重跑理由和新的 attempt，不覆盖首轮。

评估分两条轨道：多数质量/成本实验在准入时冻结 `completion_mode=FINAL_ONLY`，当前有改动候选合格且相关 UNKNOWN/清理阻断已解决后为 SUCCEEDED，`publication_status` 和 `ci_status` 都为 NOT_REQUESTED，不创建审批或外部发布。独立少量端到端样本使用明确授权的 DRAFT_PR_CI，有改动候选才继续真实 Draft PR/CI。无改动分支优先于 mode：只有最终完整输入等于冻结任务 base 且需求满足的可信 UNCHANGED 候选，独立合格后才为 NO_CHANGE，发布/CI 均 NOT_APPLICABLE，无 Approval 或空 PR。新功能候选未修复仍相对 stable 为 CHANGED，不落入该分支。三组质量对照必须使用相同 completion_mode，不把含外部交付的成本与仅 Final 的成本混算。

发布 simulator 属于单独的协议测试，明确标记模拟 scope，不替代真实交付资格。completion_mode 不允许运行中偷换；以后想发布某个 FINAL_ONLY 候选，须另建有明确授权的 DRAFT_PR_CI WorkOrder/Run，重新准入并按当前规则验证，不能恢复原已完成 Run 后直接发布。统计试验不应无理由批量创建外部 PR。

FINAL_ONLY 与 NO_CHANGE 不执行 DELIVERY；如有原预留，确认未发送才释放 DELIVERY，其他未决/清理义务继续占对应池。端到端 DRAFT_PR_CI 的交付成本以 FINAL/DELIVERY 单列，质量评测使用 FINAL/MANDATORY，不能把前者开销隐藏成免费或挪走后者保护额。

## 检查点 18.4：计算能回答问题的指标

| 指标 | 计算与解释 |
|---|---|
| 首个可信 Finding 时间 | 从候选准入起到首个命中独立 ground truth 的 ValidatedFinding；无缺陷任务单列，有缺陷但截止未检出按未检出/截尾报告，不能填 0 或只算成功样本。 |
| 真实缺陷 / 测试成本 | 按独立缺陷根因去重的检出数除以实际检查成本；同时给出含 Planner/环境/恢复的总验证成本口径，重复发现同一缺陷不增加分子。 |
| 验收判断正确性 | 分别统计正确接受、正确拒绝和未决；accepted_release 必须符合本章独立真值标准，批准新行为不同于 stable 不应被错误拒绝。 |
| 漏报与 escaped regression | 对 ground truth 的真实缺陷计算未检出率，另报错误接受候选的遗漏；分严重性/家族展示，不能用较少测试掩盖漏报。 |
| 误报与错误归因 | 报告无效 Finding/错误拒绝、环境误作代码缺陷及人工核查负担；模型待验证假设与已宣称缺陷分开，不把假设总数直接当误报。 |
| QA 人工分钟 | 分析需求、准备环境、规划实验、复核 Finding、解释未知与审阅验收的分钟数分别记录；采集/计价方法各组一致。 |
| 每个 accepted-release 成本 | 主验证阶段全部实耗（含失败、模型、环境、恢复、清理、人工及预定空闲分摊）/ accepted_release 数；分母为 0 标未定义。单列后续修复+再验证、交付和完整链路成本，不能隐藏失败修复。 |
| 修复阶段质量与成本 | 仅独立修复任务报告 repair_success、修复耗时/费用与新引入回归；保留批准新功能。主验证无修复的新功能合格候选 repair_success=false，不进入有效修复分子。 |
| 真正无变化的正确性 | 适用任务单列 correct_no_change / incorrect_no_change；按任务 base 完整输入和 ground truth 判定，不把“未调用 Coding”算正确无变化。 |
| 治理可靠性 | 重复副作用、旧 epoch 拒绝、证据缺失、越权尝试和清理失败。 |

报告总量与分场景分布，不只给平均值。成本未知不能填 0；观察窗口未结束不能按成功计数。区间估计方法写清楚；重复试验有任务相关性，不能把同一任务的多次运行当成互不相关的新任务来夸大置信度。若任务太少，只作描述性结论，并明确限制。

原始数据表至少含 task/strategy/attempt、task_mode/repair_policy/completion_mode、stable/submitted/current candidate 与需求/diff/历史引用、change_kind、base_input_digest/candidate_input_digest、ground truth revision、accepted_release/correct_no_change/repair_success 与评估裁决、选择/省略理由、业务 outcome、三轴结果、validated finding、Bundle/环境摘要、模型与执行模式、分验证/修复/交付阶段及 budget_pool/final_purpose 的成本/时间、人工裁决、排除原因。三轴合法性以第 08 章为准，正常 PASS 为 COMPLETED+PASS+NONE；UNKNOWN 执行和 INCONCLUSIVE 断言不计成功，先按执行→断言→归因处理。JSON/CSV 从同一数据源导出；中文报告不得另手工改一套结果。

## 检查点 18.5：做一次可审查 native pilot

首次主展示用第 09 章 release slice 证明新版本验证；原提前批准 seed 是底层校准基础。按以下顺序形成可审查证据，每一步都有失败出口：

1. 从固定 manifest 重建示例 stable 和外部提交的 candidate，展示真实 diff、批准的模型B/预处理v2新需求、保持不变的任务身份与成功时序规则、历史证据的版本/环境边界。历史覆盖仅作为先验，不宣称线上运行史，不重跑全部 stable 历史测试来冒充候选验证。
2. 展示需求/风险/覆盖映射、检查选择和可选省略理由，Observer/Experiment Barrier 自检通过。正确功能版、late-alias-lookup、提前成功/结果缺失和预处理错配seed已按本次范围分别校准；运行时只提供获授权输入，不额外向 Agent 暴露 hidden 参考实现或 seed 答案。
3. 主验证阶段固定 RELEASE_VERIFICATION、DISABLED、FINAL_ONLY，展示“正确 CHANGED 候选无修复也可验收”和“有缺陷候选产生可信 Finding 并阻止验收”两个出口。mandatory 及变化触发检查仍执行；影响未知则扩大检查或阻断。
4. 若展示修复，另建明确授权的修复 WorkOrder/Run，引用 Finding 和 `repair_input_candidate_ref`，保留原批准新需求。live Coding 产生新冻结候选，重新映射/检查并通过当前 Final；fake 记录不替代 live 修复，费用、时间、失败和所有尝试单列。
5. 若声明完整 native pilot 外部交付，使用另行明确授权、准入时冻结为 DRAFT_PR_CI 的样本。它可以接收合格的新功能候选，并按当前规则重新验收；不得把已完成 FINAL_ONLY Run 改模式。人工查看精确对象，结合有效平台 qualification 批准 `CREATE_DRAFT_PR`。
6. 第 17 章 Gateway 在自有 Fork 创建真实 Draft PR，读回目标/head，记录真实 CI；故障演示另展示 UNKNOWN 对账。仅有 FINAL_ONLY 证据时如实声明验证 pilot，不能声称已完成交付链。
7. 清理隔离环境、归档证据，给出剩余问题、各阶段成本和评测结论。Merge/Deploy 未执行就明确写未执行。

pilot 报告分层声明：`Native Pilot` 只涵盖限定原生业务和环境；`Enterprise Pilot` 另需适用强化、L2/L3、安全和运维；`Production` 需真实企业身份、数据、部署和灾备验收。不能因为使用真实模型、真实 Redis/ONNX 或真实 PR 就跳到 Production。

## 未来命令、验收和回退

未来命令：本章实现脚本后才可执行。正式实验先评审 plan 的资源/模型调用范围，不在本轮运行。

```sh
pnpm run evaluation:validate-plan -- --plan <ExperimentPlan-ID>
pnpm run evaluation:run -- --plan <已批准ExperimentPlan-ID>
pnpm run evaluation:report -- --experiment <Experiment-ID>
pnpm run pilot:assemble-report -- --run <NativePilot-Run-ID>
```

| 验收场景 | 预期 |
|---|---|
| 正确模型B/预处理v2新功能候选，三组 DISABLED/CODING=0 | 直接验证且保持同一候选；合格为 CHANGED 成功，计 accepted_release、repair_success=false，不计 NO_CHANGE。 |
| 批准新行为与 stable 不同 | 依据 requirement_delta 验证，不误报为回归；原不变量仍检查。 |
| 提前批准/引用字段丢失 seed | 分别经独立正反控制检出，不能用一个规则校准替代另一个。 |
| 只有历史 PASS 或 UNKNOWN_IMPACT | 不跨候选计 PASS；未知扩大检查或阻断，不让模型删 mandatory。 |
| 某组在主验证中修复或换候选 | 判为比较协议违规，修复移到独立授权阶段并重新记账。 |
| fake 与 live 混入同一结果集 | 验证器拒绝或强制分组，不能合算模型修复率。 |
| 隐藏答案被 Agent 访问 | 数据泄漏事件，受影响评测失去盲测资格并保留记录。 |
| 某组工具/预算不同 | plan 不通过，或作为另一个明确实验因素报告。 |
| 失败后覆盖首轮记录 | 存储/汇总拒绝；首次失败可追溯。 |
| 成本缺失/成功数为零 | 显示未知/未定义，不伪造低成本。 |
| C 没有收益 | 可输出保持 A/B 的决定，不能硬写“独立 Verifier 更优”。 |
| 只跑了模拟发布 | 报告明确 simulator，真实发布资格不填已验证。 |
| CHANGED 的 FINAL_ONLY 完成或拟追加发布 | 发布/CI 均 NOT_REQUESTED；追加发布另建明确授权的任务，拒绝原 Run 改模式。 |
| 真正无需修改的基线任务，Coder 无快照提议无改动 | Collector 封存可信输入并证明等于任务 base，独立验收且需求满足后才能计 correct_no_change；Release 修复输入不退回 stable。 |
| 修复任务错误保持原输入，或 Release 漏检 | 前者按适用情况计 incorrect_no_change；后者计错误接受/escaped regression，均不计 repair_success。 |
| 任一 mode 的合格 UNCHANGED 候选 | NO_CHANGE，发布/CI 均 NOT_APPLICABLE，无审批和空 PR。 |
| 当前阶段必需探索额度耗尽而 Final readiness 成立 | FINAL_DRAIN_READY 使用预留 FINAL，仍受全局限额；零而未使用的 CODING 池不触发停止。 |
| 已 STOPPED 后仍有 FINAL 额度 | 不恢复业务，只保留受限对账/清理；三组一致处理。 |
| 完成 native pilot | 范围、版本、证据、失败/缺口、Owner 决定完整，不自动部署。 |

拟建结果路径 `reports/evaluation/<experiment_id>/` 保存冻结 plan、run 索引、原始指标、汇总和中文报告；`reports/pilot/<pilot_id>/` 保存范围声明、证据索引、验收签收和后续决策。hidden answers 不复制到普通导出。

若效果不足，先根据场景判断是 Oracle/观察不可靠、任务不适配、Planner 误报还是角色通信成本过高。修方法形成新版本和新评测；不得回填旧实验。若平台可靠性失败，暂停对应外部动作，按第 11/15/17 章恢复，质量实验不能替它兜底。

完成检查：

- [ ] 数据集、split、ground truth、plan 和版本被冻结。
- [ ] Release 主场景的真实 stable/candidate diff、批准新需求与正反校准就绪。
- [ ] 三组固定同一候选、相同变化/测试能力/总预算/mandatory，主验证均 DISABLED、CODING=0。
- [ ] fake/live、原生/强化、模拟/真实发布分别报告。
- [ ] 三组质量评测同为 FINAL_ONLY；DRAFT_PR_CI 端到端交付单列，模式在准入时冻结。
- [ ] accepted_release、correct_no_change 与 repair_success 由独立 ground truth 校准；新功能无修复通过不算 NO_CHANGE 或修复成功。
- [ ] 首个可信 Finding 时间、缺陷/成本、漏报误报、人工分钟和 accepted-release 成本完整；修复与交付分账。
- [ ] 五个 budget_pool、FINAL_DRAIN_READY 与 STOPPED 语义一致，失败与剩余额度不被隐藏。
- [ ] 全部失败、未知、成本和人工负担保留。
- [ ] 结论来自可信结果与独立裁决，允许无收益。
- [ ] native pilot 证据完整，后续生产工作另列未完成事项。

下一会话可复制：

> 请按第 18 章先实现 Release QA 实验计划校验和结果汇总，不启动付费模型或创建 PR。核对第 06/08/09 章 ML release slice的需求、适用ML-C01…06正反校准和 hidden ground truth 隔离；三组固定同一候选、RELEASE_VERIFICATION、DISABLED、FINAL_ONLY、CODING=0。用明确标记的协议 fixture 验证公平预算、首个可信 Finding 时间、漏报误报、人工分钟、accepted-release 成本及零分母/未知处理，并证明无修复的新功能候选不算 NO_CHANGE 或 repair_success。建立真实 Run 导入和独立修复阶段分账接口，保留首次失败。报告哪些只是管道测试、哪些已有真实证据，以及正式 live 对照仍需的条件。
