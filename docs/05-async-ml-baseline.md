# 05 · 异步 ML 真实基线：先建立可恢复的实验环境

状态：`PARTIAL / ML_RUNTIME_NOT_VERIFIED`。通用 LabLedger、文件锁与 DockerHost 已实现；旧 Eventuate 正式来源副本和构建产物保留历史。新ML来源冻结/重复核验及API适配器mock测试已完成，**当前没有真实 ML 四场景 smoke、独立 Oracle 或 BASELINE_VALIDATED 资格**。准确命令结果见92及对应报告。

前置：[03](03-environment-preflight.md)、[04](04-project-skeleton.md)；后续：[06](06-contract-registry.md)、[07](07-observer-and-experiment-barrier.md)。本章是 API smoke 与环境资格，07–09才建立独立可裁决实验。

## 1. 目标与范围

从固定 FastAPI/Redis Streams/ONNX 上游启动合成图片分类环境，提交图片并观察任务完成、失败和拒绝；登记真实容器、端口、模型与资源身份，停止后可精确清理并重建。`202 queued` 只证明接受，`done` 是上游成功状态词；HTTP 响应本身不证明模型身份或结果持久性满足 ML-01/02。

上游固定 commit 与审计限制见03。`/health` 只探测 Redis，不能单独判定 Worker/ONNX 就绪。上游没有 per-job 模型绑定或 alias 合约，首期先如实观察；后续在自有衍生版本加明确能力，再校准 ML-02，不向上游归罪。

Stable 历史只用于核验对照身份、覆盖及资格。后续 Candidate 每次运行不机械重跑 Stable 全量套件；漂移、资格失效或需要因果对照时重跑相关检查。Stable 结果永远不是当前 Candidate Final 的替代。

## 2. 文件与分工

| 路径 | 责任与状态 |
| --- | --- |
| `tools/environment-host/src/lab-ledger.ts`、`lab-lock.ts` | 已有单机身份、事件恢复与排他写者；不代表分布式 Runtime |
| `tools/environment-host/src/docker-host.ts` | 已有本机受控 Docker 原语；新 ML 拓扑仍须审查和运行 |
| `config/active-business.json` | 当前主业务入口，来源及适配器选择 |
| `sut/ml-inference/upstream.lock.json`、`workspaces/baseline/` | 新 ML 固定来源；旧 `sut/upstream.lock.json` 保留 Eventuate 历史 |
| `packages/ml-inference-adapter/` | 按登记环境调用真实图片/任务 API，记录观察；mock 测试不等于真实栈 |
| `tools/environment-host/src/provision-baseline.ts` | 计划：创建、启动、发现与登记 ML 环境 |
| `tools/environment-host/src/destroy-environment.ts` | 计划：按精确资源身份回收与核对 |
| `artifacts/evidence/baseline/<session_id>/` | 来源、环境、API 观察、smoke 与清理证据 |

`EnvironmentInstance` 使用 `packages/contracts` 的共享协议，adapter/runner 不各造一份不兼容 DTO。

## 3. 05.0 复用实验账本，开启新的业务实验

已实现 file-lab 分配真实 experiment/run/check/attempt、环境 ID、fixture reset 与单调 epoch，追加事件作为恢复依据，快照可重建。锁冲突拒绝第二个写者；残留锁须核对宿主、boot 与进程，不能仅按时间删除。

所有记录保持 `authority_mode=file-lab`、`evidence_scope=calibration-only`。旧实验的授权范围不因业务切换自动扩大；ML使用新账本`artifacts/lab/ml-inference/`创建实验记录，并绑定新来源摘要。当前来源准备已登记3个事件、2次attempt、0环境/0动作，不能解释为已启动SUT。既有事件和资源历史不覆盖、不改名。

动作在派发前登记；已知完成写回执，外部结果不明为 UNKNOWN。恢复先观察真实资源和控制屏障，再对账，不能重复 create 或提交任务猜测成功。epoch 由账本真实分配，环境动作入口核验，不为填 DTO 硬编码。

环境配置先绑定真实 `environment_config_digest`；正式 Profile 尚未建立时 `environment_profile_digest=null`。后续DRAFT/Profile追加绑定，不能把既有校准记录改成 ACTIVE 或 DB 权威运行。

## 4. 05.1 固定正式 ML 副本

按03来源锁从精确对象导出到 `sut/ml-inference/workspaces/baseline/`，保留许可和文件清单，排除 `.git`、缓存、下载产物与秘密。固定来源副本不直接加入新模型版本功能，不混入任务 mutation。

身份至少区分 `upstream_commit`、`build_revision`、`business_revision`。依赖解析/镜像配置修改有独立补丁；新增模型绑定、预处理 schema 或恢复逻辑是业务修订。模型工件、标签映射、输入 schema 和预处理另有摘要，不用一个服务版本字符串代替全部身份。

检查点：重复准备核验所有文件；脏副本拒绝覆盖；旧 Eventuate 副本、锁和证据字节不变；没有模型工件时仍明确 runtime 未就绪。

## 5. 05.2 审核启动拓扑并分离构建

读取固定 Dockerfile/Compose 与 Worker 入口，确认 Redis Streams、消费组、Redis AOF/TTL、模型来源、卷/目录、启动命令和健康探测。不要不经审查直接运行上游 Compose 或给候选 Docker socket。

采用可信 provisioner 创建审核过的拓扑。候选仅输出已核验摘要的应用/模型工件，由宿主加载；模型工件不能运行时跟随可变 URL 下载。每个资源有 environment/epoch 所有权标签、资源上限与清理清单；API 仅映射本机动态端口，不把 Redis 管理口随意暴露。

需要持久性实验时记录 Redis 的实际 AOF 配置、数据路径与故障模型。临时内存/卷、AOF everysec、进程 kill、宿主掉电属于不同试验；不能凭一次重启通过承诺任意故障零丢失。

检查点：全部运行镜像 digest、模型/标签摘要、实际拓扑和权限可核验；无法安全加载则停止在环境准备，不将模型下载错误归为候选缺陷。

## 6. 05.3 发现并登记真实环境

共享环境投影包含 environment ID、配置摘要、source commit、build revision、fixture reset、lease epoch、服务 endpoints、镜像 digest、topology/resource refs、可空 schema/profile ref 与 expires_at。

端口来自实际创建资源的 inspect；adapter 只按登记服务名选择 endpoint，拒绝任意 URL、旧 epoch、过期环境、另一环境或重用端口。注册前核对资源标签与映射。`/health` 是依赖探测之一，还需证明 Worker 活着、模型可加载与端到端任务可执行。

未知服务/队列结构不要从架构图填空。保存最小安全投影，排除完整环境变量、Redis 凭据、用户 Docker 配置等秘密。

## 7. 05.4 四条真实 API smoke

| 场景 | 输入与预期 API 观察 | 不代表什么 |
| --- | --- | --- |
| 有效图片完成 | 合成有效 PNG 经 `POST /predictions`→202 queued；轮询同一 id 最终 done 且结果结构有效 | 分类准确率、实际模型版本、结果耐久性已通过 |
| 伪图片失败 | 声明允许的图片 MIME，但字节不能解码；接受后最终 failed（若实际源码提前拒绝须保留真实结果并核对契约） | 任意错误都能恢复、失败原因均正确 |
| 不支持媒体类型 | 不支持的 MIME 返回415，保留拒绝响应 | 可以证明没有任何存储副作用；这需要07独立观察 |
| 未知任务 | 格式合法且本fixture未创建的 id 查询返回404 | 所有TTL/越权/历史数据语义已覆盖 |

每条记录实际 HTTP 状态、任务 id、业务状态、响应摘要与轮询时间；输入完全合成且有内容摘要。请求超时不盲重发 POST：上游无幂等键，响应丢失可能已入队，必须保留不确定性并对账。

适配器 mock 测试只说明编码、解析和拒绝路径。当前适配器报告使用 `kind=api-smoke`、`evidence_scope=calibration-only`、`business_oracle=not-performed`、`model_identity=not-observed`；真实环境接入后也不得仅凭这些API结果登记 ML-01/02 PASS。

## 8. 05.5 有界观察与故障区分

使用单调时钟的总截止时间、单请求超时和最大响应体。记录中间 `queued/processing` 和终态；观察窗口耗尽为 INCONCLUSIVE，不能改成 SUT 的 failed 或将 timeout 当代码缺陷。轮询不能证明整段时间从未短暂 done，瞬态安全性留07观察轨迹。

Redis断开、模型文件缺失、Worker停止和输入解码失败分开归因。只有批准窗口、健康前提和完整因果成立，才可判活性规则违规。API状态无 `SUCCEEDED`，报告可映射其逻辑成功语义，但必须保留 raw `done`。

## 9. 05.6 精确清理与重建

先保存必要的脱敏证据，再按环境ID、epoch和精确资源ID停止/删除自有容器，最后删除空的自有网络和登记存储。拒绝未登记资源、归属不匹配、旧epoch；不使用全宿主 prune、通配符删除或无范围 `down -v`。

清理失败独立记录，不覆盖原始 smoke 结果。未知外部动作先观察对账；清理后核对无登记残留，再从新 environment/fixture 重建复测。数据持久性实验的卷在证据收集前不能删除。

未来环境命令仍须实现并注册后才能使用：

```sh
pnpm egap:environment create --lab-config <reviewed_ml_config_ref>
pnpm egap:baseline run --environment <environment_instance_id>
pnpm egap:environment inspect --environment <environment_instance_id>
pnpm egap:environment destroy --environment <environment_instance_id>
```

现有 `egap:lab` 属于来源/账本入口，不能因为它运行成功就认为上述环境命令已实现。

## 10. 验收、反例与交接

至少拒绝：旧端口/错误环境/过期实例/旧epoch；健康接口200但Worker未运行；任务仍queued却宣称完成；旧fixture污染；未知POST后直接重试；旧Eventuate记录用于ML健康资格；API done被当成独立ML-01/02通过。

保存 `source-manifest.json`、`environment.json`、`api-observations.jsonl`、`smoke-results.json` 与 `cleanup.json`，每次 attempt 保留。构建失败回03，观察缺口进07，业务语义未决回06，不能靠改断言追绿。

- [ ] 来源、模型、环境、资源、fixture 与 attempt 身份真实。
- [ ] 四条真实 smoke 已执行，或逐项准确 NOT_RUN。
- [ ] 动态发现、健康等待和精确清理可恢复。
- [ ] Redis/Worker/模型/输入失败分类准确，POST未被盲重试。
- [ ] API smoke 与独立业务资格分开，旧证据未被转义。

下一步：先完成受控启动/发现/清理，再跑四条 smoke；随后按06/07建立模型身份和结果提交的独立观察，开始 ML-02/01 校准。
