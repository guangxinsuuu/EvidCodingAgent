# 21 · ML 业务强化、第二个系统与平台扩展

实现状态：`NOT_STARTED`。本章区分第09章首期 ML 发布验证切片与后续完整 MLOps 能力。当前主线是 **Async ML Inference Release Verification**：以真实图片分类异步服务为来源，建立自有可观察基线，再验证有模型、预处理和 Worker 变化的候选。MLflow、Feast、训练流水线和生产推广都不是首期硬依赖。

首期前置是05–08实验能力、06/08批准的规则和检查，以及10–14相应执行链；其余扩展在[18试点评估](18-evaluation-and-pilot.md)后按证据选择。真实企业身份、图像/特征/预测数据另需19/20/94资格，每项扩展都独立定义scope、Profile、退出条件和回退策略。

## 1. 先选择扩展要回答的问题

| 需求 | 应扩哪一层 | 首个可验证产物 |
| --- | --- | --- |
| 稳定模型服务到新功能候选的发布验证 | Release入口、完整制品manifest、RequirementDelta、ML Oracle | 09主任务组先验证，有效Finding才Coding；新模型/预处理需求与旧不变量同时满足 |
| 同一HTTP请求重复提交 | 请求幂等契约、SUT存储与Oracle | key/payload冲突和保留策略明确；不能把Streams重投去重当HTTP幂等 |
| Worker竞争、重领与崩溃 | ML-04/05故障模板、fencing与结果观察 | 真实队列多Worker下无冲突结果、旧attempt不可覆写、任务可恢复 |
| 模型质量、切片或尾延迟回归 | ML-07评估Profile与数据治理 | 冻结标注集、切片、统计方法、运行环境与批准阈值的可复算报告 |
| 模型/特征注册与生产推广 | MLflow/Feast等独立Connector与部署动作 | 不可变model/feature引用、ACL、来源血缘、推广/回退和在途任务处理 |
| 接入另一套应用 | Service/Environment Adapter与领域Oracle | 同一平台协议下第二系统的健康/缺陷对照 |
| 多Run、多租户或新执行引擎 | 调度、配额、隔离、Provider | 竞争/跨租户否定测试与相同Final下的可比结果 |

新业务要求先成为版本化需求和Profile，不能事后偷偷加进上游兼容验收。

## 2. 首期发布切片：模型 B、预处理/schema v2 与 Worker 重构

V3.4/V4.0是自有实验标签，不代表上游发行版或真实线上运行史。固定上游是图片分类服务；它没有逐Job模型身份字段和HTTP幂等键。先在自有衍生Stable中建立提交时冻结model/preprocessing manifest及可信观察，再由V4引入模型B、preprocessing/schema v2与已批准v1兼容，并重构Worker。不能把本项目新增约束说成上游已经承诺。

1. 第06章冻结新功能语义、输入/结果/队列schema及兼容矩阵；第07章确认排队、模型装载、结果写入与完成状态的可信观察/屏障；第08章为适用ML-01…06建立正反校准。
2. 第09章独立准备正确候选和late-alias-lookup等变异候选。模型B/预处理v2实际新增行为应可验证；只给旧源码换版本号不是增量发布。正确参考实现、seed标签和私有Git历史与Coder/Planner隔离。
3. 准入冻结 `task_mode=RELEASE_VERIFICATION`、`repair_policy=DISABLED|ON_VALIDATED_FINDING` 和 `completion_mode`。已有候选先验证，只有可信代码Finding和授权才Coding；修复从当前candidate开始，最终change_kind仍比较冻结stable完整输入。
4. Final同时覆盖新功能和批准的不变量/兼容要求。全部回退stable、删模型B或删schema v2必须失败；正确V4零Coding也可成功，但相对stable有变化，不能标NO_CHANGE。
5. G3交付可核验切片，G5比较漏报、误改、成本和人工工作量。DRAFT_PR_CI是另选完成模式，满足15–17资格；“发布验证”不自动授权生产alias推广或流量切换。

API烟测只证明当次连接与受理/终态行为，不是ML-01的持久性或ML-02的身份Oracle。ML-07仍待批准数据与阈值，不随模型B自动宣称通过。

## 3. 扩展A：幂等、并发、恢复与持久结果

先characterization，记录固定来源面对相同Stream entry重投、重新投递相同Job、重复HTTP请求、旧Worker迟到写入分别做什么。上游的Redis Streams、XAUTOCLAIM和Lua fencing是待实测的机制，不是完整业务保证。

由Owner明确：同key同payload复用还是拒绝、不同payload冲突、是否包含model/feature身份、失败结果能否复用、保留与重试上限。HTTP幂等可采用 `tenant + endpoint + idempotency_key` 绑定规范化请求、冻结模型/预处理身份及逻辑Job；超时先查持久映射，不能换key重发。输入、Job、队列与结果跨边界时须有可恢复协议，不能以进程内Map或“用了Redis事务”代替。

并发测试至少有两个Worker竞争同一逻辑Job、租约/claim变更后旧attempt回写、结果写入后确认前退出，以及旁观Job。允许重复计算并不允许冲突结果或任意旧attempt覆盖；“只出现一条结果”仍需证明其他必要状态一致。

持久性必须给出明确失败模型：原模板的AOF everysec和TTL不能保证所有已受理任务在宿主故障后零丢失，也不是长期结果库。若要求更强语义，另行选择持久结果存储/确认策略、记录开销并测试重启、恢复和保留到期；不在报告里把机制名称变成承诺。

拟建 `registry/profiles/ml-hardening/`、`packages/oracles/src/ml-hardening/`、`tests/integration/ml-hardening/`；业务补丁在受控衍生版本，原始来源保持冻结。每一项都有批准语义、真实多进程/崩溃证据、健康/缺陷对照和兼容报告。

## 4. 扩展B：模型质量、数据切片与性能

ML-07单独冻结：数据集和标签revision、来源/许可、预处理、样本/切片规则、模型摘要、指标、置信区间或其他预注册统计方法、容许变化及硬门槛。开发集可调试，holdout由独立评估者控制；不能反复看holdout后仍称盲测。

新模型预测可以合法不同于旧模型。任务准确率、业务成本、校准、切片最差表现或安全约束按具体业务选择；不得把无依据的通用阈值写进合同。首期公开或合成图像只用于协议校准，不能外推真实信用评分或其他受监管决策质量。

性能分别量化排队、模型加载、预处理、推理、结果写入和端到端延迟；固定硬件、并发、输入尺寸、热身、缓存与失败处理。对同一冻结release进行公平champion/challenger比较，代码/模型/数据变化都进入因果说明。

## 5. 扩展C：模型注册、特征与生产推广

首期本地不可变manifest足够表达模型身份，不为了一个alias竞态先引入完整MLflow/Feast。需要跨团队注册、审计、特征物化和生产运营时，再按第20章接入指定版本/账号的只读Connector，并单独授权写入。

```mermaid
flowchart LR
  E[冻结候选与独立验收] --> A[精确推广批准]
  A --> P[切换新任务的模型引用或流量]
  P --> O[观察新任务与在途旧任务]
  O -->|明确回退条件| R[停止新流量并恢复批准引用]
  O -->|外部结果未知| Q[对账与有界人工移交]
```

推广不应改变已经绑定旧模型的排队Job。回退可以影响未来任务，但无法撤销已返回的预测或下游业务动作；迟到结果、取消太晚、部分流量切换与未知调用必须分别定义。Feast接入还要明确feature snapshot、event time、freshness与training-serving skew，不能靠“用了Feature Store”自动获得一致性。

后续训练流水线/Flyte等引擎属于另一个业务Profile：训练、评估、注册、推广和失败恢复分别留证，不把工作流retry视为业务补偿。

## 6. 扩展D：第二系统与执行引擎

目标是验证业务Adapter与公共治理的分离，不复制第二套Agent图。

1. 选择有明确Owner、可重建环境与可观察事实的小系统。
2. 建立Service/Environment Profile，列支持、需扩展、未知能力。
3. 实现prepare/health/act/observe/cleanup，并注册受限实验模板。
4. 对健康版本和单一缺陷做双向校准，再复用Runtime、CodingProvider、Final与审批/发布。
5. 当前ML Profile回归仍通过，不能为了第二系统改掉原合同答案；历史Eventuate若恢复实施，也必须重新验证，旧编译记录不等于业务通过。

接入成本包括Adapter、Oracle、权限和业务决策时间。两个案例成功不表示任意企业系统零改造接入。替换CodingProvider时输入仍是受限WorkOrder，输出仍须可信封存与独立验证，session/checkpoint不能改变批准语义。

## 7. 多Run、升级与退役

从两个独立环境并行开始，验证fixture/reset、端口、Stream/consumer group、模型缓存、证据、预算和cleanup互不干扰。逐步增加并发并测CPU/RAM/磁盘、模型加载、队列pending和尾延迟；配额在权威事务生效。企业多租户另执行19的身份、对象授权、网络、依赖/模型缓存隔离及跨租户负例。

上游源码、依赖、模型、预处理、Prompt、Oracle或Runner变化都产生新revision。只对受影响资格和必要基线做有理由的复核；不能覆盖历史证据，也不为每次候选机械重跑全部stable。

退役顺序：关闭新准入/推广 → 排空或明确停止在途Run/Job → 对账外部效果 → 归档规范/证据 → 撤销凭据 → 按批准期限清理。UNKNOWN对象移交责任人，不能删账本制造“完成”。

## 8. 每个扩展的完成标准

- [ ] 需求、Owner、版本化Profile和不适用范围明确。
- [ ] 有真实Adapter/Oracle与健康、缺陷、坏观察器对照。
- [ ] 受影响旧业务/平台资格已重验，新权限与成本可解释。
- [ ] 有实际指标、残余风险、继续/停止决定及回退策略。

证据放 `reports/21-extensions/<extension-id>/<run-id>/`；命令在对应工作包实现后注册，不预建无边界run-all。后续可说：“按21只做队列重复与Worker恢复，先核对已批准语义及实际实现，再完成一条故障窗口；本轮不自行修改MD。”

历史治理来源：[v3 第31、35–36、45节](../../EGAP-Order-Approval/docs/eventuate-production-plan-v3.md)。其订单业务要求保留在93的历史映射中，不能直接替代ML契约。
