# 08 · ML Oracle：让版本、结果与 PASS 有明确含义

实施状态：`NOT_STARTED`。本章的测试、Oracle、可信结果与最终 Profile 尚未实现或运行；旧草案 PLANNED/NOT_RUN 仍保持原义。

前置：[06.1–06.4 DRAFT Registry](06-contract-registry.md)、[07 Observer/实验屏障 校准](07-observer-and-experiment-barrier.md)。下一章：[09 缺陷样本](09-seeded-defects.md)。本章继续使用05的 file-lab 账本，DRAFT 校准不要求先取得 ACTIVE 合同；07–09的实际结果形成后再回06.5真正审批/激活。先实现纯判定函数和正负事实样本，再运行真实栈；纯样本通过只能证明 evaluator 逻辑，不证明 ML 业务已正确。

## 目标与首期范围

先让 ML-C02 模型绑定与 ML-C01 结果提交顺序具备完整正反校准，再按能力准入 ML-C03/04/05/06。ML-C07 是质量/性能扩展，需独立数据和批准门槛。目录不是当前全部已实现或已激活的mandatory；发布承诺适用的MUST缺能力时必须阻断，不能默默跳过。

Oracle判断明确规则下的事实，不判断“模型觉得代码合理”。预测值可以因合法模型升级而变化；版本/输入/预处理身份正确、批准行为与质量范围才是依据。旧订单Oracle与PASS不转为ML证据。

## 三个组件的边界

模板准备合成输入、执行受控动作和屏障；Observer/Collector独立采集事实并绑定身份；Oracle对冻结事实作确定性判定。Oracle不调用LLM、不修改SUT、不去另一个未绑定环境补事实，也不接收candidate stdout中的PASS。

Coder可以写授权局部测试，但不能修改可信Oracle、隐藏数据/参数、证据存储、签名与最终强制集合。Verifier可以提出实验，不能把建议直接登记为裁决结果。

## 计划文件

| 路径 | 责任 |
| --- | --- |
| `packages/oracles/src/result.ts` | 共享三轴结果构造，不另造枚举 |
| `packages/oracles/src/ml/model-binding.ts` | ML-C02：绑定、实际加载与结果模型身份 |
| `packages/oracles/src/ml/result-commit.ts` | ML-C01：成功前提交与释放后活性 |
| `packages/oracles/src/ml/feature-consistency.ts` | ML-C03：输入快照/预处理/schema身份 |
| `packages/oracles/src/ml/delivery-recovery.ts` | ML-C04/05：重复效果、claim/fencing和崩溃恢复 |
| `packages/oracles/src/ml/schema-compatibility.ts` | ML-C06：批准版本矩阵 |
| `packages/verification/src/templates/async-ml.ts` | 受限实验模板，不接受模型提供的脚本 |
| `registry/drafts/async-ml-inference/verification-profile.json` | 检查、版本、参数与能力闭包 |

这些是后续计划，当前未实现。若将确定性ML判定器实现为Python，也须保持相同的冻结输入、受保护身份和共享结果协议，不能把任意Agent代码当Oracle。

## 检查目录与证据

| ID / 规则 | 实验 | 必须证明 | 范围边界 |
| --- | --- | --- | --- |
| ML-C01 / ML-01 | 精确hold目标结果提交，再释放 | hold期间完整轨迹无提前done；结果达到批准持久性边界后才done；释放后有界完成 | 一次进程恢复不证明宿主掉电零丢失或无限TTL |
| ML-C02 / ML-02 | 提交绑定A→确认hold→别名切B→执行 | 旧job实际模型/预处理A与结果相符；新job按合同使用B；job/input关联正确 | 模型自述版本字符串不足；上游缺能力须先显式扩展 |
| ML-C03 / ML-03 | 使用输入/特征snapshot与v1/v2预处理 | 实际消费内容、schema、顺序与缺失值语义符合冻结合同 | 首期合成图片不等于Feast或完整特征平台 |
| ML-C04 / ML-04 | 精确重复投递同一逻辑job | 允许重复计算，但不能产生冲突结果或重复业务效果；其他job不受污染 | 队列at-least-once不等于API幂等；上游无幂等键 |
| ML-C05 / ML-05 | 在预定阶段kill Worker并恢复 | claim/租约/fencing与结果状态一致，恢复或明确失败在批准界限内，旧Worker不能覆盖新结果 | 只在声明故障模型内成立，不外推掉电/网络分区 |
| ML-C06 / ML-06 | 批准的客户端、任务、Worker、schema组合 | 兼容组合正确读取/转换；不支持组合有明确拒绝，旧任务身份不漂移 | 不自动包含任意滚动升级/回退排列 |
| ML-C07 / ML-07 | 独立评估模型与数据切片、性能负载 | 满足批准指标、置信/样本策略、切片底线及硬件条件 | 目前延期；几个相同预测不是质量或性能证明 |

每项都有明确输入域、前提、参数、有限无污染范围、失败/未知路径和清理。05的有效图片/伪图片/415/404是API smoke，不能替代上表独立证据。

## 发布切片的适用性

V3.4/V4.0是自有服务release；modelA/modelB、preprocess/schema v1/v2分别有内容身份。V4新增模型B、预处理/schema v2并保留批准v1兼容，Worker重构不得破坏已入队旧任务绑定与结果完成顺序。

RequirementDelta驱动ML-C02/03/06的具体组合，ML-C01等适用安全项保持强制。模型B输出可不同于A；既不能要求所有预测逐项相同，也不能以“新模型自然不同”忽略错误输入/身份。没有ML-C07资格时报告明确不评价真实分类质量。

正确新功能版、late-alias变异、schema/预处理错配变异及恢复对照分别校准。整版回滚到Stable即使通过旧检查，也会因V4需求缺失而拒绝。

## 小步实现

### 08.1 先把事实样本与判定接口写清

每项检查输入为冻结 Profile、ObservationBundle 及受控参数；输出共享 CheckResult。execution_status、assertion_status、attribution 分开：

```ts
type CheckResult = {
  check_run_id: string;
  execution_attempt_id: string;
  authority_mode: "file-lab";
  evidence_scope: "calibration-only";
  candidate_or_baseline_ref: string;
  bundle_digest: string;
  check_version: string;
  oracle_version: string;
  environment_instance_id: string;
  observation_bundle_ref: string;
  execution_status: "QUEUED" | "RUNNING" | "COMPLETED" | "ERROR" | "CANCELLED" | "UNKNOWN";
  assertion_status: "PASS" | "FAIL" | "INCONCLUSIVE" | "NOT_EVALUATED";
  attribution: "NONE" | "CODE_DEFECT" | "ENVIRONMENT" | "ACCESS" | "CONTRACT_AMBIGUITY" | "UNKNOWN" | null;
  reason_codes: string[];
  predicate_results: Array<{ id: string; outcome: "TRUE" | "FALSE" | "UNKNOWN"; evidence_refs: string[] }>;
};
```

这是一份早期校准字段投影，不替代共享 DTO 完整定义；实际类型还应包括 tenant/run、参数摘要、seed、fixture/reset、采集身份与完整性字段。check_run/attempt 身份由05的 lab-ledger 先登记，`bundle_digest` 指06.4冻结的 DRAFT 内容，而非 ACTIVE 资格。

三轴统一约定：`attribution=NONE` 只表示执行已确认完成、断言 PASS，当前结果没有故障归因；`UNKNOWN` 只表示失败或不确定结果的原因尚未查明，不能用于正常 PASS。`null` 仅在 assertion_status=NOT_EVALUATED 且尚未进入归因时允许，包括排队/运行、取消未裁决、执行完成但尚未裁决；它不是成功。ERROR+NOT_EVALUATED 若已有环境等诊断可直接记录该归因。FAIL/INCONCLUSIVE 不得使用 null 或 NONE。适用性 `NOT_APPLICABLE` 另存，不扩充此枚举。以下是真值与路由约束，DTO、Oracle 和 Graph 共同遵守：

| execution_status | assertion_status | attribution | 必须采取的处理 |
|---|---|---|---|
| QUEUED / RUNNING | NOT_EVALUATED | null | 等待真实完成，不进入业务裁决。 |
| UNKNOWN | NOT_EVALUATED / INCONCLUSIVE | ENVIRONMENT / ACCESS / UNKNOWN；仅 NOT_EVALUATED 且未归因时可 null | 先对账执行事实，不能因其他字段直接修复或放行。 |
| ERROR | NOT_EVALUATED / INCONCLUSIVE | ENVIRONMENT / ACCESS / CONTRACT_AMBIGUITY / UNKNOWN；仅 NOT_EVALUATED 且未归因时可 null | 先处理执行或证据问题；不能自称代码已验证。 |
| CANCELLED | NOT_EVALUATED | null 或已知问题归因/UNKNOWN | 停止新业务，保留对账与清理；不产生 PASS。 |
| CANCELLED | INCONCLUSIVE | ENVIRONMENT / ACCESS / CONTRACT_AMBIGUITY / UNKNOWN | 保留明确的证据不足原因，不用 null 假装尚未判断。 |
| COMPLETED | PASS | NONE | 经身份与完整性核验后更新 coverage。 |
| COMPLETED | FAIL | CODE_DEFECT | 还需可信 ValidatedFinding 才能请求修复。 |
| COMPLETED | FAIL / INCONCLUSIVE | ENVIRONMENT / ACCESS / CONTRACT_AMBIGUITY / UNKNOWN | 按已知原因处理或调查；不得直接驱动改代码。 |
| COMPLETED | NOT_EVALUATED | null | 尚无断言结果，保持不完整；不能进入 Final PASS。 |
| 任意非 COMPLETED | PASS | 任意值 | 非法组合，Collector/DTO 拒绝。 |
| COMPLETED | PASS | 非 NONE | 非法组合，拒绝而非自动改写为正常通过。 |
| 任意状态 | FAIL / INCONCLUSIVE | null / NONE | 非法组合，必须给出已知或 UNKNOWN 故障归因。 |

路由优先级固定为 execution_status → assertion_status → attribution；先确认执行，再判断断言，最后使用归因选择修复责任。旧记录若以 UNKNOWN 表示 PASS 无故障，不可直接导入为新协议有效结果，须版本化转换并保留原文与转换依据。

先构造小事实集：健康模型绑定；错误模型/预处理；提前done；错误job/result关联；缺屏障回执；缺转换轨迹；环境不健康。每个样本说明它验证 evaluator 的哪一条决策，不能照抄函数分支做没有业务意义的镜像测试。

### 08.2 实现证据有效性检查

进入业务断言前，核对 subject、bundle、环境实例、fixture reset、参数、Oracle/Observer 版本、lease/执行 attempt 与证据摘要。候选 A 的事实不能送给候选 B；同一个 CheckRun 的两次物理 attempt 不能拼成看似完整的一次轨迹。

缺证据不能产生 PASS。执行已确认完成且满足预先批准“最小反例证据”时，可给出 FAIL，并另列不影响该反例成立的观察缺口；不能将所有缺失都自动解释为代码缺陷。执行仍 UNKNOWN/ERROR 时先保留原始违反事实与 predicate，不伪造 COMPLETED；完成执行对账和可信裁决后才形成相应正式 FAIL。若无法证明违反，则 INCONCLUSIVE/NOT_EVALUATED，按原因归环境、访问、合同或未知。

检查点：篡改一个身份字段、移除屏障回执或替换观察摘要，都能阻止通过；错误明确指向该字段或证据。

### 08.3 先实现 ML-C02 模型绑定判定

按因果回执确认：任务提交时记录固定A，精确HOLDING命中该job，随后alias切B，释放后实际加载A，结果关联同一job/input/model/preprocess，且有界完成；另一个新任务使用B。A/B工件hash不同，不能仅换标签使实验成为空操作。

任务绑定、实际加载、结果身份是不同predicate。只比相等的自述字段、只看top1类别、只看模型文件名都不足。模型缓存与lazy加载必须有观察依据；缺实际执行身份证据则INCONCLUSIVE，不能因为预测看起来合理就PASS。

旧job偷偷使用B是目标反例；所有job永久queued不能算修复。记录实验窗口与健康前提，窗口尚未Owner批准时仅是校准参数，不升级成正式业务SLA。

### 08.4 实现 ML-C01 安全与活性

证明arm先于目标动作、实际结果提交HOLDING命中、hold期间状态轨迹完整且无提前done、释放保持原提交摘要、结果达到批准提交边界、done与结果存在正确关联，以及释放/恢复后有界完成。

分别输出safety/liveness predicate：任一FALSE导致FAIL，任一关键UNKNOWN不能PASS。最终结果存在不覆盖曾经的短暂非法done。AOF everysec、Redis返回成功与持久可恢复是不同层级；按合同选择已具备的证据，不能把内存键存在当磁盘flush已确认。

若技术前提未恢复或释放状态UNKNOWN，优先记录环境/对账，不能伪造liveness CODE_DEFECT。无法证明过程完整时也不能声称“从未提前成功”。

### 08.5 分步加入 ML-C03/04/05/06

ML-C03绑定输入字节、feature snapshot、预处理参数/顺序、schema与运行时；数值比较采用冻结容差，NaN/Inf、维度与dtype单独检查。

ML-C04明确逻辑job与物理delivery/attempt，验证重复投递后的最终效果唯一且一致，允许合同明确的重复计算。不要将“结果最后覆盖成一样”当无冲突证明；需要状态/写入历史。没有API幂等键时不得宣称HTTP重试安全。

ML-C05按批准阶段真实中断Worker，观察pending/claim/lease/fencing及恢复结果。旧Worker恢复后覆盖新结果必须可检测；TTL到期、结果已丢失和未执行不是同一状态。ML-C06按批准组合保存旧任务/客户端fixture、实际新旧schema和转换证据，不用一个200响应替代兼容矩阵。

每项先纯样本，再真实健康/变异/恢复对照。没有对应能力则保持UNQUALIFIED；在发布范围承诺该需求时阻断，不能当N/A自动放行。

### 08.6 连接模板和运行器

模板只接收 typed fixture/环境/受控参数，调用第05–07章 adapter；每项使用独立 fixture 或严格验证 reset。按 `try/finally` 设计清理，但清理失败不得覆盖原始业务结果，单独记录 cleanup_status 和未决资源。

execution attempt 先由05的 lab-ledger 登记再派发，结果提交绑定同一 attempt；文件排他锁、追加事件与原子快照保证本机实验记录可恢复，不用一份覆盖写 `result.json` 代表所有运行。第10章引入 DB 事务/多Worker机制时保留 `origin_experiment_id` 和原身份映射，不改写旧记录的 authority_mode，也不将校准 PASS 自动转成正式 Final 证据。

未来命令：实现并注册脚本后才可执行，当前不存在：

```sh
pnpm egap:checks run --suite ml-release-final --subject <frozen_subject_ref> --environment <environment_instance_id>
pnpm egap:checks inspect --check-run <check_run_id>
```

此处 ml-release-final 只是套件选择；只有激活合同、有效环境/平台资格与证据满足时，才具备第14章 Final Gate 的放行意义。实验模式必须在报告中显式标记。

## 有意义的正负测试集

| 控制样本 | 应有结果 |
| --- | --- |
| 健康事实、完整绑定与实际加载身份 | 在批准范围PASS |
| 旧job绑定A，alias切B后实际执行B | ML-C02 FAIL |
| 结果字符串写A但实际加载B | 不能被字段相等骗过；实际证据充分则FAIL，缺则INCONCLUSIVE |
| hold期间短暂done，最终补写了结果 | ML-C01 safety FAIL，不被终态覆盖 |
| 永远queued，没有提前done | 活性前提成立时FAIL，否则INCONCLUSIVE |
| 版本正确但input/preprocess/job关联错 | 相应ML-C02/03 FAIL |
| 重复投递产生冲突结果或旧Worker覆盖新结果 | ML-C04/05 FAIL |
| 删除v1兼容或回滚整个V4 | ML-C06/需求覆盖拒绝 |
| Observer缺权/断段，或另一candidate/environment/reset证据 | ERROR/INCONCLUSIVE或身份拒绝，不PASS |
| COMPLETED + PASS + NONE | 正常coverage路径 |
| COMPLETED + PASS + UNKNOWN，或 UNKNOWN + PASS | 非法组合拒绝，不静默改写 |
| COMPLETED + INCONCLUSIVE + UNKNOWN | 保留证据不足，不请求修代码 |
| UNKNOWN + NOT_EVALUATED + UNKNOWN | 先对账执行 |
| CANCELLED + NOT_EVALUATED + null | 保留未裁决取消 |
| FAIL/INCONCLUSIVE + null/NONE | 非法组合拒绝 |

先纯函数事实集，再真实健康与受控变异。预先固定重复次数/种子，保留全部attempt，不能挑某次绿灯当资格。静态fixture只验证evaluator逻辑，不证明真实ML栈正确。

## 证据、排障与完成清单

计划路径为 `artifacts/evidence/checks/<check_run_id>/<execution_attempt_id>/`，保存 plan、原始事实引用、predicate 结果、CheckResult、环境清单与清理回执。报告明确 static-fixture 或 real-stack 证据等级。

Oracle与基线不一致先查合同、模型/输入身份和观察前提，不修改期望追绿。屏障缺口回07，数据/数值语义回06，构建/运行问题回03/05。Oracle变更产生新版本并重新校准，旧PASS不自动继承。

- [ ] 当前适用检查的predicate、参数、前提与证据闭包明确。
- [ ] ML-C02包含实际加载身份，ML-C01兼顾安全、持久性边界与活性。
- [ ] 每项有正反控制；未实现能力不被当作已覆盖或N/A。
- [ ] PASS使用NONE；执行、断言、归因按顺序路由，非法组合拒绝。
- [ ] 失败attempt与原始身份保留，未把API smoke或fixture结果当真实资格。
- [ ] 模型质量范围准确，没有因版本一致就宣称分类质量和生产就绪。

下一步：先实现ML-C02/01纯判定与反例样本，再接07已获资格的真实观察和屏障；缺实际能力时保留阻断，不接模型自动修复来掩盖缺口。
