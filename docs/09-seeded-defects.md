# 09 · 客服发布任务集：正确新版本、单缺陷校准与隐藏评估

状态：`NOT_STARTED`。没有自有健康Stable、正确V4、人工变异或真实客服评测。来源参考冻结和客户端mock不表示这些业务已实现。前置[05](05-genai-support-baseline.md)、[06 DRAFT](06-contract-registry.md)、[07](07-observer-and-experiment-barrier.md)、[08](08-native-oracles.md)。校准后返回06.5激活，再接10–14正式执行。

## 1. 来源、派生基线与候选分开

固定OpenAI航空Demo仅提供工作流参考；SaaS工单、账单、历史政策、持久Job/草稿和可信网关是自有新增业务。先建立健康基线并正反校准，不把上游缺少我方需求称为bug。

Stable V3.4与Candidate V4.0是实验室发布标签，没有真实线上史。主展示为V4新增十一月政策和Tool Schema v2，同时保留十月事件适用性；第一轮固定LLM/Worker/无关Prompt，不同时变更六项。正确V4零Coding也可通过；有可信反例且获准修复才进入Coder。

首个校准变异`SUP-SEED-01 latest-policy-only`错误丢弃历史政策；第二个`SUP-SEED-02 ticket-date-as-renewal-date`误传日期。两者分别植入，标`SYNTHETIC_MUTATION`，不暗示真实生产事故。

## 2. 目录与可见性

| 拟建位置 | 读者 | 内容 |
| --- | --- | --- |
| `datasets/support-release-pilot/catalog.json` | 平台/评估器 | 任务、公开描述、适用Profile与资格 |
| `datasets/support-release-pilot/releases/` | 按scope | Stable/Candidate manifest、真实ChangeSet、批准RequirementDelta |
| `tools/evaluation-host/seed-definitions/` | QA私有 | 变异patch、答案、目标规则与校准证据 |
| `tools/evaluation-host/release-scenarios/` | QA私有 | 正确V4、hidden案例、独立ground truth |
| `artifacts/task-inputs/<task_id>/` | 当前Coder/Verifier | 获准候选、公开规则、差异与可信Finding |
| `artifacts/evidence/seeds/<calibration_id>/` | 受控证据读者 | 全部attempt、原始事实、变异身份与结果 |

hidden目录名或.gitignore不构成隔离。QA完整Git对象、正确参考实现、逆补丁和隐藏fixture不能挂载给Planner/Coder或进入其搜索/缓存。第一条公开演示只算校准；正式价值评估使用另建隐藏组合，不能声称已公开答案仍是盲测。

## 3. 09.1 健康基线与最小业务矩阵

先锁代码/依赖、Prompt、provider/model配置、语料/索引/检索、工具Schema/adapter、输出schema、workflow、fixture和clock；记录可取得的真实模型返回身份，不造权重hash。

至少覆盖：十月事件适用十月政策；十一月事件适用新政策；证据充分的ELIGIBLE/INELIGIBLE；CS-1042缺使用/合同事实为NEEDS_MORE_EVIDENCE；合同冲突；A/B tenant隔离；未批准敏感效果；draft与DONE关联。范围未承诺的能力明确延期，不能偷偷删除已承诺规则。

05真实smoke和07/08适用Oracle校准通过后才能登记healthy_baseline_ref，早期仍为file-lab/calibration-only。基线失败先查环境、观察和规则，不叠加mutation制造不可归因失败。

## 4. 09.2 单缺陷与因果对照

SUP-SEED-01仅改变检索过滤/索引，使历史事件得到latest政策；账单事件、工具参数、LLM配置、规则和Oracle不变。证明目标路径确实运行、错误政策实际被返回/采用；如果只导致构建失败，不是政策语义反例。

SUP-SEED-02仅改变日期参数映射；检索器在给定日期下保持正确。观察实际工具参数以区分映射与检索根因。若只看到错误答案而缺trace，不能宣布是哪一类缺陷。

QA私有记录parent、patch/digest、业务revision、目标规则、非目标约束、最小证据与恢复方式。候选允许修改路径中可以包含Prompt/检索配置/工具适配器，但不允许改业务标准、Oracle或隐藏答案。

## 5. 09.3 正反、恢复与坏观察器

每个样本运行健康→单变异→撤销变异后的恢复对照，使用独立fixture/reset和冻结重复策略。还要运行坏观察器/缺权限/断trace；它们必须拒绝通过而非补造事实。不得用重跑到绿替换首次失败。

模型输出不逐字比较。硬规则反例与语义报告分开，完整证据才能判定；环境/provider失败不算成功检测业务缺陷。固定样本数/重复/预算和停止条件，保留失败与未知费用。

## 6. 09.4 正式WorkOrder与任务冻结

WorkOrder冻结`task_mode=RELEASE_VERIFICATION|INCIDENT_REPAIR`、`repair_policy=DISABLED|ON_VALIDATED_FINDING`、`completion_mode=FINAL_ONLY|DRAFT_PR_CI`，以及tenant/repo/scope、候选、规则、预算和允许修改资产。Release输入至少含stable_release_manifest_ref、submitted_candidate_ref、release_changeset_ref、requirement_delta_ref、historical_coverage_refs。

base_commit_sha/base_input_digest是稳定比较基线；CodingAttempt.repair_input_candidate_ref是当前待修候选，两者不能混用。外部功能候选先可信封存，不要求先出现CodingAttempt。修复后保留submitted候选谱系，重新计算Stable→当前候选差异；不通过替换checkpoint引用冒充同一次验证。

early file-lab身份、正式Run和评估任务分别登记来源映射。DRAFT校准不能被命名替换为正式Final；隐私/费用/权限准入按15/94及实际通道分别核验。

## 7. 09.5 成功、修复与NO_CHANGE

正确CHANGED V4在repair DISABLED/CODING=0下可直接独立Final；这是候选验收成功，不是repair_success或NO_CHANGE。CANDIDATE_DEFECT还需可信Finding、允许asset_kind/path和授权预算才能修复。规则模糊交Owner，provider不可用处理环境，不自动改Prompt追绿。

Coder无修改提案须封存真实输入候选；不能丢掉提交时已有新功能，也不能空snapshot跳验收。仅最终完整输入等于任务base且需求满足时才可NO_CHANGE。两种completion mode下NO_CHANGE均无空PR。修复成功需缺陷被可信关闭、批准新功能与兼容要求仍满足；回退Stable删除十一月/schema v2能力必失败。

## 8. 09.6 扩展样本与禁止混算

| 后续样本 | 目标 | 前提 |
| --- | --- | --- |
| Prompt遗漏合同例外 | SUP-04/07 | 可信事实+独立关键claim判定 |
| 票据/RAG中的Prompt Injection | SUP-02/03/08 | 网关权限和完整实际效果证据 |
| 旧revision迟到提交 | SUP-05 | 精确claim/commit屏障、DB条件写 |
| 提前DONE或写失败 | SUP-06 | 完整提交轨迹与非干扰屏障 |
| Handoff上下文缺失 | 新批准需求 | handoff schema/规则/观察已定义 |
| 换LLM/embedding/reranker | 质量/选测变化 | 版本、数据、重复、成本与评分资格 |

不要把所有样本混入首个发布。每次扩展重新确定适用检查和未知范围，逐项校准。模型随机失败不是可以随意归因的固定种子缺陷。

## 9. 09.7 公平评估边界

18主对照固定QA、同工具只读Planner、专用Verifier使用同候选、业务输入、能力、预算和mandatory；主验证关闭修复，修复收益另账。SUT LLM、Verifier、grader、Coder与环境/重试全部计费。无业务上下文/有上下文固定策略/有上下文自适应作为独立消融，不混称同一三组。

先预注册split、样本、重复/随机顺序、异常处理、错误接受/遗漏/误报、首个可信Finding时间和人工分钟。公开校准不能证明隐藏任务泛化；一次漂亮Demo不证明统计收益。

## 10. 09.8 主展示步骤

1. 展示Stable与提交V4的完整manifest、真实差异和批准新需求，标实验室标签。
2. Verifier读取获准业务上下文，提取检索/工具变化，形成风险假设；未知影响明确保留。
3. 选择政策日期边界、工具参数对照、缺证据与适用权限检查；说明成本与可选省略。
4. 从实际工具记录区分错误参数与错误检索；无法区分则补证，不能提前指定根因。
5. 正确V4独立通过；有变异V4产生可信反例并阻断。修复只在另有授权时执行。
6. 修复后重新冻结候选、验证新功能与旧语义，再Final；按任务模式结束或精确审批Draft PR/CI。

## 11. 负例与交接

- [ ] 用工单日期代替续费日期、仅保留latest政策均可被独立区分。
- [ ] 缺使用/合同信息不误判ELIGIBLE；完整信息不靠永远转人工通过。
- [ ] 未发生的模型调用、工具效果或持久提交不能被日志伪装。
- [ ] 删除新功能、改政策答案、改Oracle或读hidden答案均拒绝。
- [ ] 合法新行为不因不同于Stable被误报回归。
- [ ] 身份/环境/Oracle漂移、provider失败和坏观察器不能成为业务PASS。

校准后先返回06激活，再接10–14；不在09偷偷实现发布或将公开seed当正式效果评估。
