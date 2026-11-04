# 09 · ML 校准与发布任务集：验证真实变化，发现可信缺陷再修复

状态：`NOT_STARTED`。没有自有stable模型绑定扩展、V4功能候选、人工mutation或真实评测。上游来源准备与API adapter不表示这些业务已实现。

前置：[05](05-async-ml-baseline.md)、[06.1–06.4](06-contract-registry.md)、[07](07-observer-and-experiment-barrier.md)、[08](08-native-oracles.md)。先校准09.1–09.3，再回06.5审批激活；正式Release入口先验证已有候选，只有可信代码Finding和修复权限都成立才进入12 Coding。第10章Runtime与第13章规划器不是早期校准前提。

## 1. 区分来源、健康对照与发布价值

固定上游是MobileNetV2图片分类模板，没有per-job模型版本绑定或alias合同。先构造自有衍生健康版本，显式加入不可变模型/预处理manifest与任务绑定，再通过独立观察校准。不能把上游缺少新需求的事实直接标为缺陷，也不能把mock结果或版本标签当健康基线。

第一个开发校准样本 `ML-SEED-01` 是**延迟解析可变模型别名**：健康版提交时绑定A；变异版在Worker执行时才解析已切到B的别名，导致旧任务错误使用B。它验证ML-C02和治理闭环。随后ML-SEED-02提前done验证ML-C01。

主展示为Stable V3.4→有真实功能变化Candidate V4.0：模型B、preprocess/schema v2、批准v1兼容和Worker重构。正确新功能版无需Coding即可Final；含回归版先被独立发现，再按授权处理。不要人为规定每次必须“找一个bug修好”。

V3.4/V4.0仅服务实验标签；modelA/B有独立内容摘要，预处理/schema也各自版本化。没有真实线上证据就标“实验室稳定对照”。全部人工变异在QA校准/事后报告显著标 `SYNTHETIC_MUTATION`，不得称为上游bug或真实生产事故。

## 2. 文件与可见性

| 计划路径 | 读写角色 | 内容 |
| --- | --- | --- |
| `datasets/ml-release-pilot/catalog.json` | 平台/评估器 | 任务与适用Profile、公开描述、资格状态 |
| `tools/evaluation-host/seed-definitions/` | QA私有 | mutation、精确patch、目标规则、恢复答案 |
| `tools/evaluation-host/release-scenarios/` | QA私有 | 正确V4、seed/no-seed、独立ground truth |
| `tools/evaluation-host/src/materialize-task.ts` | 可信准备器 | 只导出授权stable/candidate对象闭包 |
| `datasets/ml-release-pilot/releases/` | 按scope读取 | StableManifest、ChangeSet、RequirementDelta与历史覆盖投影 |
| `packages/contracts/src/work-order.ts` | 公共协议 | 来源、候选、规则、预算和权限引用 |
| `artifacts/evidence/seeds/<calibration_id>/` | 受控证据读者 | 全部attempt、原始事实、mutation摘要与结果 |
| `artifacts/task-inputs/<task_id>/` | 当前Coding/Verifier | 授权源码、公开规则、差异与有效Finding |

目录叫hidden或.gitignore不能隔离答案。QA私有Git对象、正确V4、逆补丁、隐藏参数和ground truth不得出现在Coding/Verifier挂载、工具搜索或缓存中。Agent可读授权stable/candidate差异，以便做真实变更分析；不得因此获得QA完整仓库或本章的私有实现配方。

## 3. 09.1 建立自有健康对照

固定上游源码与模型/输入/预处理工件，记录显式业务扩展patch及原因。健康业务至少能将提交时选择的模型/预处理不可变身份写入任务，并确保Worker使用该身份；结果可独立关联实际执行工件。07的可信观察和屏障位于候选权限外。

分别记录 upstream、derived stable、build、business、model、preprocess/schema、environment、Oracle/Observer/barrier与fixture。05 smoke和07/08适用独立检查通过后才登记 `healthy_baseline_ref`；仍为file-lab/calibration-only，不自动获得发布资格。

基线失败先查环境、观察和业务承诺，不能继续植入一个难以归因的mutation。缺功能必须先作为显式业务建设处理，不能让Coder根据验证失败随意设计合同。

## 4. 09.2 一个最小、可观察的模型版本mutation

ML-SEED-01只改变模型选择时点：从提交时冻结不可变身份，改为Worker执行时读取可变alias。保持输入、模型工件、预处理、队列与Oracle不变；若mutation只引起import/构建失败，它是构建样本，不是版本一致性反例。

实验步骤：提交绑定A的job→可信控制确认目标等待→alias切B→释放→观察实际加载与结果。必须证明A/B内容不同、切换确实发生、目标路径确实执行；仅修改版本字符串不能构成有效样本。

QA私有记录父基线、patch/hash、目标ML-02、预期观察点、非目标约束与所需能力。给Coder的输入是批准规则、可验证反例和允许路径，不包含逆补丁或“把哪行改回去”的答案。

## 5. 09.3 三组校准与敏感性

| 对象 | 目标事实 |
| --- | --- |
| 健康衍生基线 | 旧job实际使用A，新job使用B，二者完成且身份正确 |
| ML-SEED-01 | 旧job实际使用B，与提交绑定违背 |
| 可信恢复对照 | 同一实验条件消除反例，其他功能仍成立 |

预先固定重复计划、窗口、故障位置和seed，全部物理attempt由LabLedger登记并绑定真实epoch、fixture、DRAFT bundle。任何失败不能被最后一次成功覆盖。复现不稳定先处理屏障、模型缓存/加载身份或fixture；不满足资格则UNQUALIFIED。

增加ML-SEED-02时同样跑健康/提前done/恢复三组，必须捕获结果未提交时的非法done轨迹。Oracle变更要独立审核、新revision和重新校准，不能为了让mutation“失败得正确”临时修改期望。

## 6. 09.4 正式WorkOrder与任务输入

校准完成后回06.5确认模型绑定、持久性、时间、重试和兼容语义及真实Owner决定，激活对应Bundle。DRAFT可先保存和校准，不能伪造ACTIVE或签名。正式准入由10冻结，历史校准不自动升级为当前Final。

```ts
type WorkOrderCommon = {
  work_order_id: string;
  tenant_id: string;
  repository_id: string;
  base_commit_sha: string;
  base_input_digest: string;
  contract_bundle_ref: string;
  allowed_paths: string[];
  prohibited_asset_refs: string[];
  budget_profile_ref: string;
  repair_policy: "DISABLED" | "ON_VALIDATED_FINDING";
  completion_mode: "FINAL_ONLY" | "DRAFT_PR_CI";
};
type ReleaseWorkOrder = WorkOrderCommon & {
  task_mode: "RELEASE_VERIFICATION";
  stable_release_manifest_ref: string;
  submitted_candidate_ref: string;
  release_changeset_ref: string;
  requirement_delta_ref: string;
  historical_coverage_refs: string[];
};
type IncidentRepairWorkOrder = WorkOrderCommon & {
  task_mode: "INCIDENT_REPAIR";
  source_artifact_ref: string;
  symptom: string;
  baseline_reproduction_refs: string[];
};
```

实际类型与contracts统一，上例不是已实现DTO。完整输入摘要还绑定代码/依赖/配置/模型/标签/预处理/schema/数据集与运行时；不能只固定Git commit后让模型文件漂移。

Release的base是冻结stable；submitted candidate含真实新增功能。CodingAttempt另记 `repair_input_candidate_ref`，从触发Finding的当前候选继续，不从stable重建丢掉功能。Incident的base是fault输入而非健康答案。Git直接parent、任务比较base和候选修复谱系分别核验。

Release先验证；`DISABLED`禁止任何Coding，`ON_VALIDATED_FINDING`仍需当前独立代码缺陷及scope/预算。环境、权限、合同不清转对应责任方，不靠改业务绕过。默认FINAL_ONLY不产生远端写入；DRAFT_PR_CI须明确仓库、动作与批准并满足15–17资格。

首个mutation修复只授权模型选择/任务绑定的窄业务路径及必要开发测试；trusted Oracle、隐藏答案、权限策略、依赖安装脚本默认禁止。V4涉及schema与预处理的允许路径另行明确，不能沿用窄scope偷偷扩大。

## 7. 09.5 修复成功与NO_CHANGE

成功必须在当前冻结候选上满足批准需求与全部适用mandatory，模型/输入/结果身份和证据闭包完整。永久queued、全部failed、关闭Worker、伪造版本字符串、删除新功能、修改Oracle都不是修复。

合法的局部回退可以修复缺陷，但不能回滚整个V4到V3.4或删模型B/schema v2来逃避新功能验收。无缺陷V4可以不运行Coder，仍相对stable为CHANGED；独立验收成功不计为AI修复成功。

NO_CHANGE只有最终**完整输入**与冻结任务base相同且任务全部满足时成立。即便Coder无修改，也须可信封存现有Candidate、独立Final与明确终态；不能用“模型说没必要改”替代验收。有缺陷而不修不能计repair_success。

## 8. 09.6 扩展样本目录

| 样本 | 目标 | 需要补齐 |
| --- | --- | --- |
| ML-SEED-01 延迟alias解析 | ML-02 | 实际模型身份、精确排队控制、正常活性 |
| ML-SEED-02 提前done | ML-01 | 结果提交屏障、完整状态轨迹、持久性边界 |
| ML-SEED-03 错preprocess/schema | ML-03/06 | 输入snapshot、批准转换与兼容矩阵 |
| ML-SEED-04 重投冲突/旧Worker覆盖 | ML-04/05 | 真实重复、崩溃、租约/fencing和写入历史 |
| 无缺陷新功能候选 | 正确无需修复 | 同样Final与新需求覆盖 |
| 环境故障/合同歧义 | 准确停止和归因 | 不产生伪代码Finding或无权修改 |

目录项在能力和正反校准通过前保持PLANNED/UNQUALIFIED，不按样本数量声明覆盖。

## 9. 09.7 公平评测边界

按缺陷家族拆分开发、演示和held-out；相近patch、相同alias/schema变体属于潜在泄漏。公开演示记忆不能直接带进held-out。公开上游可能已被模型见过，报告说明限制。

18章三组使用同stable/candidate、规则、历史覆盖、工具、预算和Final。主对照固定Release+DISABLED+FINAL_ONLY；修复另建授权任务，不让某组先改候选再继续与其他组比发现率。衡量漏报/误报、首次有效Finding、测试成本、人工时间与有效覆盖，而不是要求多跑测试。

将来seed/materialize/export命令须实现注册后才可运行；QA生成入口不向Coder开放。公开manifest与私有答案分离，正式导出校验ACTIVE Bundle和completion_mode。

## 10. 09.8 主展示：模型与预处理升级发布

| 变更 | 本次目标 | 进入正式验收前必须明确 |
| --- | --- | --- |
| 模型A→新增可选择的模型B | 新提交任务能使用获准B；旧任务保持提交绑定 | 模型字节/许可/输入输出、选择器更新与不可变绑定语义 |
| preprocess/schema v2 | 新输入处理链真实发生变化，保留批准v1兼容 | schema版本、转换/拒绝、缺省与历史任务、输入输出容差 |
| Worker重构 | 调度/加载/写入组织变化但保持旧不变量 | 允许路径、缓存、结果先提交、claim/fencing与恢复范围 |
| 低风险文案对照 | 验证影响分析能区分非业务变化 | 不因低风险文案跳过强制安全项，也不无理由启动高成本故障矩阵 |

实施顺序：

1. 固定derived stable完整输入与历史覆盖，准备真实含新增功能的candidate和RequirementDelta；代码变化、模型字节变化与语义假设分别记录。
2. 回06/08补适用ML-C02/03/06及ML-C01安全项，明确支持/不支持组合；ML-C04/05若被发布承诺触发必须补齐，ML-C07没有资格就明确不评价模型质量。
3. QA准备无seed正确候选、late-alias回归候选、preprocess/schema错配候选及恢复对照；每个回归单独证明目标违规，不把单缺陷旧版本改个标签充数。
4. Release入口可信封存后，Verifier分析影响、选择注册模板/批准参数。历史覆盖只辅助排序；未知影响扩大检查或QA复核，强制项不可删。
5. 有效Finding且授权才Coding，修复从当前candidate继续；重新冻结完整输入与谱系，再验证新功能未丢失。
6. 当前候选Final通过后按change_kind/completion_mode结束或进入精确人审。Draft PR/CI不等于自动合并、上线或真实业务生产资格。

证据包括源/模型/预处理摘要、ReleaseChangeSet、RequirementDelta、历史覆盖、风险→实验选择、原始观察、Finding、全部attempt与修复谱系。主展示完成必须靠这些事实，不能靠流程图或模型复述预设答案。

## 11. 负例与交接

| 负例 | 处理 |
| --- | --- |
| mutation未命中或只导致构建失败 | 拒绝对应业务样本资格 |
| 健康基线同样失败 | 停止，排查环境/规则/观察，不派发自动修复 |
| A/B只换标签、实际模型字节相同 | 实验不具区分性，重新准备 |
| 只有版本自述字段，没有实际加载证据 | INCONCLUSIVE，不能ML-C02 PASS |
| QA答案/Git对象经路径、缓存泄漏 | 评测无效，重建可见性 |
| 无可信Finding即Coding或DISABLED改代码 | 拒绝派发 |
| 回滚V4删除新功能 | 新需求/兼容验收拒绝 |
| 无修复V4被标NO_CHANGE | 按完整base/candidate摘要纠正分类 |
| Oracle/环境变更仍复用旧PASS | 身份拒绝，重新校准 |
| 只保留最后一次成功 | 证据不完整，资格拒绝 |

保持失败历史，新patch/环境/Oracle均新revision。下一步应先完成真实ML环境与自有健康能力；在07/08可裁决之前，本章样本和正式Agent修复均保持未开始。
