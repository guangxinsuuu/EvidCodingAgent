# Async ML Inference Release Verification 迁移报告

日期：2026-11-04。用户明确授权将当前业务从 Eventuate 订单改为异步 ML 推理发布验证，并同步本轮相关 docs 与实现；此授权不改变以后修改 docs 仍须许可的约定。

## 实际完成范围

默认业务为 `async-ml-inference`，任务主线仍是 `RELEASE_VERIFICATION`：稳定版本、已提交的新功能候选与需求变化 → 影响分析和针对性验证 → 可信 Finding 后按授权修复 → 独立 Final 和按需交付。Coding 与验证的独立性来自规则、执行身份、观察器和证据权限，不能以另一个 LLM 自查代替。

首期采用真实图片分类服务（FastAPI + Redis Streams + ONNX MobileNetV2），只使用合成图片。稳定 V3.4 / 候选 V4.0 是后续自有实验的服务发布标签，不是上游版本或已证实的线上运行史。ML-01…07 是拟建规则目录，不代表已批准或已验证。

### 代码与配置

| 路径 | 本次行为 |
|---|---|
| `config/active-business.json` | 指明当前ML业务、来源锁、新账本与适配器；明确旧资格不得迁移 |
| `config/ml-source-policy.json` | 固定公开仓库的commit/tree与27个文件的blob、SHA-256、size和mode |
| `tools/environment-host/src/ml-source.ts` | 生产固定策略入口，策略JSON自身摘要须一致 |
| `tools/environment-host/src/ml-source-core.ts` | 校验Git对象并原子落位；既有副本只核验；拒绝脏树、额外文件、链接、mode漂移；不执行上游代码 |
| `tools/environment-host/src/business-profile.ts` | 默认业务路由与账本来源检查；误复制旧账本也不能继续使用旧来源 |
| `tools/environment-host/src/cli.ts` | `egap:lab prepare-source/status/new-attempt/recover-lock`切到独立ML账本 |
| `tools/environment-host/src/ml-source.test.ts` | 来源冻结、拒绝篡改、恢复边界、业务路由等8项测试 |
| `packages/ml-inference-adapter/src/api.ts` | 实际multipart `/predictions`协议、ID/epoch/过期检查、逐请求guard、响应大小/时间限制、脱敏证据、POST不重试 |
| `packages/ml-inference-adapter/src/fixtures.ts` | 合成合法PNG、伪PNG及不支持MIME；证据只留摘要 |
| `packages/ml-inference-adapter/src/smoke.ts` | 合法图片done、伪图片failed、415、未知Job404四场景；未确定时停止新增副作用 |
| `packages/ml-inference-adapter/src/api.test.ts` | 14项mock HTTP测试，明确不是真实推理 |
| `tsconfig*.json`、环境宿主包配置、`pnpm-lock.yaml` | 默认编译/类型检查与依赖图切换到新包，复用原有第三方依赖 |
| `legacy/eventuate/` | 归档旧业务适配器、来源导出器和运行计划，退出默认构建/测试 |

主要调用关系：

```text
egap:lab prepare-source
  → loadActiveBusiness / assertActiveSourceBindings
  → LabLedger独占锁与新ML实验身份
  → prepareMlBaselineSource
  → 固定策略摘要、Git blob和文件校验
  → sut/ml-inference原子落位
  → SOURCE_BOUND事件

未来真实环境登记后的SMOKE入口
  → runBaselineSmoke
  → MlInferenceApi
  → 环境guard
  → 实际HTTP
  → evidenceSink
```

第二条目前只有适配器与mock HTTP测试；实际provisioner/guard、真实服务启动与SMOKE编排尚未接通。

### 来源与账本实测

- 上游：<https://github.com/kevin-sparkflow/ml-inference-service>，MIT许可，已保留LICENSE。
- commit：`c200bcb4721c4428a9bfe808ca8a61648a2b16f6`。
- tree：`8c5d7895328087acfa988769db80db7d45aa2cdb`。
- 新基线：`sut/ml-inference/workspaces/baseline`，27个文件，业务内容未修改。
- 来源锁：`sut/ml-inference/upstream.lock.json`，SHA-256 `8ac054f6a0ed27d842c04a5f55a551dc65d99f8a5cc8ccd7a457ed15b388c91f`。
- 新账本：`artifacts/lab/ml-inference`；experiment `experiment_66f037a9-bc35-4f96-acf6-ce1a6e60a159`。
- 首次返回CREATED，第二次同experiment返回VERIFIED_EXISTING；2个attempt、3个journal事件、0环境、0外部动作。
- 明确资格：`SOURCE_ONLY_NOT_RUNTIME_VALIDATED`；构建revision `ml-upstream-unbuilt-v1`。

### 源码审计发现

实际上游API是POST `/predictions` multipart图片→202/queued；GET使用queued/processing/done/failed。`/health`只检查Redis。任务和结果TTL为3600秒；Redis Streams配合XAUTOCLAIM及Lua所有者检查。推理可能执行多次，HTTP重复提交也没有幂等键。

上游没有逐任务模型/预处理身份字段，没有可变模型alias注册表。ML-02的“提交时绑定模型、执行/结果一致”必须先在自有衍生基线中实现并由独立观察校准，不能把新要求直接称为上游bug。Lua原子可见性不等同宿主故障下零丢失；AOF everysec的持久性范围必须由后续合同明确。未下载ONNX工件，只有源码中的预期摘要，不能将其写成已观察模型摘要。

静态事实详见证据 `upstream-audit.json`。主机观察为Python 3.14.5、未发现uv；上游构建镜像使用Python3.12可变tag。尚未安装依赖、锁镜像digest或验证这些运行条件。

## 验证结果

| 检查 | 实际结果 | 范围 |
|---|---|---|
| pnpm typecheck / build | 通过 | 当前TypeScript代码与依赖图 |
| pnpm test | 77/77通过 | 通用协议、账本/锁、Docker适配器mock、来源8项、ML mock HTTP 14项等 |
| pnpm test:integration | 3/3通过 | 控制面真实本机HTTP、进程退出与端口释放；不是ML端到端 |
| 新源码冻结与二次复核 | 通过 | 固定27文件及同实验新attempt |
| 旧资料保护复核 | 339文件摘要均不变 | 原来源、历史证据及AGENTS |
| 带EGAP归属标签的Docker容器/网络盘点 | 均为空 | 无需清理；未干扰其他Docker资源 |
| 真实Redis/ONNX启动与四条smoke | NOT_RUN | 下一阶段 |
| ML-01…07独立Oracle、Agent、Final/发布 | NOT_RUN | 后续对应章节 |

完整命令、退出码和输出摘要位于 `artifacts/evidence/migration/20261104T074012Z-ml-migration/`。`final-tests.output.txt`是最终77项输出；此前76项记录保留，没有覆盖失败/旧尝试历史。本轮测试均通过，没有新业务生产资格。

## 文档同步

主目录、00–22以及90–94共30份Markdown同步新ML场景与真实进度。最终结构检查覆盖258处本地链接、围栏配对和当前主线旧业务引用，均无问题；93保留56项历史ID，94有24条数据流向和15项验收条目。05重命名为 `05-async-ml-baseline.md`，07为 `07-observer-and-experiment-barrier.md`。Python LangGraph为后续独立规划进程；TypeScript控制面继续拥有预算、任务、检查登记和Final权威，两边使用版本化JSON Schema。

保留精确批准/派发前撤销、NONE与UNKNOWN分离、完整输入NO_CHANGE、受保护预算池、ACL失败关闭等原约束。93保留历史v3完整56项并解释ML映射；94增加模型、输入/特征、预测、评估数据的流转约束。后续代码实施不能自行修改这些文档。

## 下一步

按03/05完成ML专属预检：固定Python/uv构建镜像与Redis digest，隔离解析uv.lock，下载并实测校验ONNX工件；随后实现登记环境的真实启动、动态端口发现、四条API smoke和精确清理。再进入07，先证明独立模型/结果观察与屏障可行，才能建设ML-01/ML-02 Oracle。MLflow、Feast、质量切片、性能与企业真实数据按后续阶段分别准入。
