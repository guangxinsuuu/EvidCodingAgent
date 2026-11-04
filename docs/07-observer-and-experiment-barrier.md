# 07 · 独立观察与实验屏障：证明任务使用了什么、何时完成

状态：`NOT_STARTED`。尚无可信 ML Observer、模型切换/结果写入屏障或真实校准证据。接口规格不代表能力已经支持。

前置：[05 真实 ML smoke 与 file-lab](05-async-ml-baseline.md)、[06.1–06.4 冻结草案](06-contract-registry.md)。本章仍为 `authority_mode=file-lab / evidence_scope=calibration-only`，不要求先有ACTIVE，不提前依赖第10章数据库 Runtime。后续：[08](08-native-oracles.md)。

## 1. 两个独立实验目标

**ML-02 模型绑定：**任务在提交时绑定模型A/预处理A；确认任务在受控边界等待后，将可变选择器切到模型B，再让任务执行。独立事实必须证明旧任务实际使用A，新提交任务按合同使用B；两者都能完成。只有任务结果返回一个 `model_version=A` 字符串不够，候选可能没有真正加载A。

**ML-01 完成顺序：**在任务的结果提交边界设置可信屏障，实际命中后观察状态不能提前 `done`；释放后结果提交与最终成功有可验证因果。最终结果存在不排除曾短暂出现“done但结果不存在”，所以只轮询终态不足以证明安全性。

上游无每任务模型绑定与alias管理，本章能力依赖自有业务扩展和可信环境控制。不能把暂停Worker、延迟网络、排队屏障、hold结果写入四者都称为同一屏障，也不能把候选函数中的sleep当独立可信控制。

## 2. 先调查真实物理事实

固定上游的 Redis Streams、任务/结果键、consumer group、ack、claim、Lua fencing、TTL与AOF是源码线索，运行时仍须核对。模型文件、标签映射、预处理与ONNX session由哪个进程加载，何时缓存，结果在哪里保存、成功状态如何写入，都需要源码和运行对应。

| 拟建路径 | 责任 |
| --- | --- |
| `tools/environment-host/spikes/ml-experiment-barrier/` | 模型绑定、加载身份、结果提交控制点的可行性研究 |
| `packages/ml-inference-adapter/src/schema-map.ts` | 实际任务/队列/结果结构与版本映射 |
| `packages/ml-inference-adapter/src/observer.ts` | 独立读取事实，不返回PASS |
| `packages/ml-inference-adapter/src/experiment-control.ts` | 受限arm/status/release/close协议 |
| `apps/check-runner/src/observation-collector.ts` | 核验身份、完整性并登记不可变观察 |
| `tests/integration/ml-observer-calibration.spec.ts` | 真实正反观察与非干扰校准 |

这些均为计划文件。Coder、Verifier Planner、SUT运行身份不能写Observer记录或屏障控制；候选不能拥有Redis管理员凭据来删除受保护轨迹。单纯新增一个“只读类”不能代替角色/网络/存储权限验证。

## 3. 07.1 映射任务、模型与结果

输出实际 `schema-map.json`，至少涵盖：job ID、raw status、请求/input digest、入队消息ID、claim/attempt/fencing、任务绑定模型/预处理身份、实际模型加载证据、结果摘要/位置、结果提交与状态转换序列、TTL与清理作用域。

尚不存在的模型绑定字段标为 `MISSING_CAPABILITY`，不能从结果预测反推版本。模型A/B必须内容不同且身份清楚；ONNX session复用、lazy load和文件替换要纳入观察。若运行时允许换字节但沿用旧标签，资格不成立。

Redis单次读取、多个独立GET、Stream历史、Lua原子写、磁盘flush是不同观察范围。先记录能证明的事实，再确定ML-01持久性层级；不要从API成功推断已fsync。TTL到期后对象消失不自动等于写入未发生。

## 4. 07.2 评审屏障实现点

| 方案 | 可证明能力 | 限制 |
| --- | --- | --- |
| 可信队列/派发控制器精准持有目标任务 | 在执行前建立模型选择器切换窗口 | 必须证明Worker不能旁路、重试/claim不会偷跑 |
| 可信模型选择代理/只读工件挂载 | 绑定不可变工件与实际加载身份 | 不可把代理返回标签当实际ONNX加载证据 |
| 可信结果存储代理/提交控制 | 只阻塞目标结果提交，支持ML-01时序 | 要处理同Redis连接/事务与状态写入；阻塞全库可能掩盖缺陷 |
| 暂停整个Worker | 依赖故障/恢复实验 | 未必是精确排队或结果提交屏障，不能直接授予前两项资格 |
| 修改候选代码sleep/输出测试标记 | 开发诊断 | 候选可删改，不能单独作可信Oracle依据 |

首选方案须实测后决定，不假定存在现成hook。无法在不破坏事务与正常语义的情况下精确控制时，记录 `BLOCKED_OBSERVABILITY`，保留API smoke成果，停止对应安全性资格；不能扩大选择器或删除规则推进Final。

## 5. 07.3 受限控制协议

```ts
interface ExperimentControl {
  arm(input: {
    environment_instance_id: string;
    fixture_reset_id: string;
    execution_attempt_id: string;
    selector_ref: string;
    capability: "HOLD_JOB_DISPATCH" | "HOLD_RESULT_COMMIT";
    max_hold_ms: number;
  }): Promise<{ barrier_id: string; armed_receipt_ref: string }>;
  status(barrier_id: string): Promise<BarrierStatus>;
  releaseOriginal(barrier_id: string): Promise<ReleaseReceipt>;
  close(barrier_id: string): Promise<CleanupReceipt>;
}
```

这是拟议协议，不是当前工具。selector由可信准备器登记，绑定专属环境、fixture、job/消息和attempt，不接受任意Redis key、SQL、正则、脚本或跨租户选择器。提交前job ID未产生时使用专属fixture与预登记关联，实际返回后核对绑定；失败不得升级为“暂停全部任务”。

状态区分 `ARMED / HOLDING / RELEASED / EXPIRED / ERROR / CLOSED`。ARMED仅说明已安装，HOLDING才说明实际命中；释放必须保留原任务/提交数据摘要，禁止偷偷改成“正确答案”。模型别名切换另由受限模型目录动作产生回执，不能让Planner直接改任意文件。

## 6. 07.4 独立采集与完整性

ObservationBundle至少绑定 subject/source/model/input/preprocess、bundle、run/check/attempt、environment/reset/epoch、schema-map、Observer版本、collector身份、事实引用、屏障回执、起止与完整性。ID来自LabLedger，摘要真实计算；没有字段不能填示例值。

原始事实来自独立只读存储、受保护状态轨迹与可信执行观察。应用日志用于辅助解释，不能独自证明实际工件或提交顺序。不同进程墙钟先后不能替代因果；用事务/提交序列、消息与屏障身份构造可解释切面。

要断言“hold期间从未done”，需要完整受保护轨迹或等价覆盖。采集丢失/权限缺失为 GAPPED/UNKNOWN，并阻止PASS；若已捕获满足批准最小反例的违规，可保存反例，按08执行/裁决规则处理，不能伪造完整性。

## 7. 07.5 三路非干扰校准

同一健康业务revision、新fixture比较无控制器、控制器旁路、实际HOLDING后释放。正常任务结果、模型身份、合法失败与有限恢复应一致；控制器不应改变目标输入、执行次数或错误映射。

再使用QA控制的模型延迟解析变异和提前done变异：健康版通过，变异在目标规则失败，恢复对照重新通过。变异显著标 `SYNTHETIC_MUTATION`，不作为上游bug声明，也不泄漏到Coding工作区。

ML-02证据链：arm→提交并记录固定A→实际hold→别名切B回执→释放→加载A事实→结果A与完成→新任务使用B。ML-01证据链：arm→目标result commit命中→hold期间状态轨迹→原提交释放→提交证据→done与结果关联。

## 8. 07.6 中断、撤销与清理

同一barrier/operation ID恢复时先读真实状态及账本，不重复arm或重复提交。release响应丢失是UNKNOWN，必须对账是否已释放；不能通过换ID消除不确定性。重复release应幂等，不额外制造任务；重复投递测试另有ML-C04模板。

取消后停止新动作，记录仍持有的任务/结果、已切换的别名、在途操作。只允许有界对账与精确清理；清理后核验规则、挂载、消息、别名和权限没有残留，才允许环境复用。无法证明则隔离/销毁并保留资源记录，不用“下次重启”掩盖未知。

## 9. 退出门槛与负例

仅当真实hook、精确命中、因果/完整性、非干扰、正反对照、不可绕过与恢复清理都通过，才登记对应单项capability `SUPPORTED`。一个屏障通过不代表另一项自动支持。

负例至少：错job/环境/reset/epoch；未命中却release；旧模型文件被替换；伪造版本字符串；缺状态轨迹；collector身份错误；跨fixture读取；重复release；result提交未知；Worker能绕过控制器。分别拒绝、ERROR或INCONCLUSIVE，不能产生PASS。

证据保存 `artifacts/evidence/observer/<calibration_id>/`：实际映射、权限、hook来源、三路对照、全部回执、原始事实、缺口和清理。新hook/权限/模型加载方式产生新revision，旧结果不自动继承。

下一步只做窄spike：查明固定上游的任务与ONNX加载路径，设计可独立证明的模型绑定观察和精准排队控制；没有证据前不搭完整Agent，也不把API smoke改名ML-02通过。
