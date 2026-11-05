# 本地真实模型客服业务运行报告

日期：2026-11-05。范围：把自有合成 SaaS 客服被测系统搭起来并实际跑通。没有修改 `docs/**` 或项目 README；没有改合成事实、公开参考答案、上游来源锁或旧业务证据。本报告记录本轮实现、实测结果与已知问题，不取代已冻结实施手册。

**结论：客服异步处理链路已运行，运行资格为 `LOCAL_CALIBRATION_ONLY`。四项 API smoke、九个公开场景的结构化结果通过；真实模型回复仍有两项已观察到的语义问题，不能宣称客服质量或发布验收合格。** 机器摘要见 `summary.json`，已知问题及实际原文见 `known-findings.json`。

## 1. 实际业务能力与调用链

合成企业的订阅续费退款争议：接收指定工单及 revision，查同租户订阅、账单、用量、合同与适用政策，计算初步退款资格，由本地模型起草客户回复，持久保存供人复核。缺少资料会返回补充资料建议。系统没有执行退款、发送客户消息或关闭工单的功能。

```text
pnpm support:up
  → tools/support-lab.py（持久启动身份、角色凭据、真实端口）
  → support-environment.ts → LabLedger / DockerHost（专属 PostgreSQL、Redis、卷）
  → genai_support.runtime（各角色独立本机进程）

POST /support-resolution-jobs
  → server.create_app：bearer映射tenant、校验请求及工单
  → Store.create_job：同一PostgreSQL事务提交Job + outbox + ACCEPTED
  → Relay.run_once：outbox → Redis Stream，只发送job_id
  → Worker.run_once：数据库claim + 租约fencing
  → GatewayClient：先查询已有回执，确认为404才请求resolve
  → ModelGateway.resolve：只读核对持久Job身份 → GatewayStore.begin去重与预留
  → BusinessTools.collect（6个事实工具）→ evaluate_refund_policy（第7个业务工具）
  → call_local_model：真实Ollama qwen2:7b，生成待审核回复
  → GatewayStore.finish：先持久化工具/模型回执与草稿
  → Store.complete_job：校验租约及工单revision，草稿与DONE同事务提交
  → Worker确认Redis消息
GET /support-resolution-jobs/{job_id}
  → tenant范围查询持久结果；其他租户返回404
```

Job 的 `DONE` 只表示这一轮草稿已保存。它不等于 Ticket RESOLVED、退款批准、EGAP PASS 或代码发布批准。业务判定函数属于可修改的 SUT 产品实现，后续仍须独立 Oracle 验证。

## 2. 文件、职责与取舍

| 文件/模块 | 本轮变化与原因 |
| --- | --- |
| `services/genai-support-sut/migrations/001.sql` | Job、outbox、attempt、事件、心跳与fixture绑定；用真实事务保存受理和完成结果 |
| `.../store.py` | 短连接/短事务、租约与fencing、revision检查、UNKNOWN保留、同租户查询；旧Worker不能提交覆盖新尝试 |
| `.../server.py` | 真实202受理和持久查询；无存储时仍保持准确的骨架503；readiness检查实际依赖 |
| `.../relay.py` | 先通知Redis后标记outbox；回执不明容许同Job重复通知；定期扫描DB修复通知丢失 |
| `.../worker.py` | 消费组与pending reclaim、租约续约、先查网关回执、提交后ack；总HTTP时限避免慢分块拖住处理 |
| `.../business_tools.py` | 读取版本化合成业务事实；v2按实际政策参数计算时间窗口/用量/合同条件；不读参考答案 |
| `.../model_gateway.py` | 固定模型端点、身份校验、一次有界真实调用、严格JSON回复Schema；模型只能输出回复文案 |
| `.../gateway_store.py` | 独立数据库记录调用身份、工具事件、模型回执和预算；STARTED/UNKNOWN不盲目重发 |
| `.../runtime.py` | API、relay、Worker、gateway、model五个角色的原生进程入口，实际socket回执与24小时进程期限 |
| `tools/support-lab.py` | 统一up/status/stop/start/down/test/smoke；私有凭据、可恢复启动记录、PID+启动签名清理；检查Ollama孤儿子进程 |
| `tools/environment-host/src/support-environment.ts` | 自有SUT运行账本与参考源码账本分开；文件内容摘要、新实验身份、实测端口、资源登记与对账 |
| `tools/environment-host/src/docker-host.ts` | 增加有归属标签的local named volume；只准指定数据挂载点，拒绝host mount/他人资源/广泛删除 |
| `tools/support-api-smoke.mjs` | 复用已有TS适配器测试真实HTTP，绑定environment/epoch并保存脱敏观察 |
| `tools/support-business-smoke.py` | 宿主读取公开期望，一次提交后仅轮询原Job，比较结构字段、持久结果和租户拒绝；不把它称为独立Oracle |
| `pyproject.toml`、`uv.lock` | 增加锁定的psycopg[binary] 3.3.8、redis 8.1.0；已有HTTP/Schema依赖保持锁定 |
| `config/active-business.json`、`business-profile.ts` | 状态更新为LOCAL_RUNTIME_IMPLEMENTED / LOCAL_CALIBRATION_ONLY；严格配对运行账本，仍拒绝虚构PASS/READY |
| 对应Python及TS测试 | 真实数据库、Redis交付恢复、网关预算/重复请求、宿主恢复和卷归属边界 |

为什么采用这条链：PostgreSQL是Job事实来源，Redis负责通知，消息重复不等于重复业务执行；独立网关账本让Worker丢失回执后能够查询原结果。事务无法涵盖外部模型，故请求无回执时保留UNKNOWN及预算预留，不能为了“尽快成功”重发。当前UNKNOWN需后续显式对账，未实现完整EGAP恢复运行时。

凭据按角色分配：SUT应用数据库、gateway数据库分开；gateway的Job reader只获四个身份字段SELECT，不能读草稿或更新Job。管理员凭据仅由宿主初始化持有。API两个本地会话对应两个租户。当前进程仍同一操作系统用户运行，这些是逻辑与数据库权限边界，并非候选代码的物理隔离。

## 3. 两轮真实模型实验与保留的失败

### 第一轮 v1：链路成功，业务决定有误

环境：`environment_493b663a-02f6-4f33-b297-88ec94a45d40`。

- 真实模型10次；4/4 API smoke；9个业务Job均DONE。
- 仅5/9结构化业务场景符合参考。用量非零、7天加1秒、新政策8天、另一租户已使用等4例被模型误判为ELIGIBLE。
- 原始回执、结果、数据库快照与v1源码摘要保留在原环境证据目录，未覆盖成成功；该环境已精确清理并验证重复清理幂等。

### 第二轮 v2：明确调整产品职责

新环境：`environment_38db0dd8-6c13-4515-b767-1c3f3f6d8e4f`。
工作流/提示词版本：`support-policy-decision-v2`。

v1把结构化资格交给模型判断。v2在调用前由业务工具读取版本化政策并精确计算资格、建议动作、政策与证据引用，模型的输入包含决定及事实，响应只允许 `customer_reply_draft`。这是一项显式产品逻辑修改，没有在模型作答后偷偷替换错误字段，也没有改参考答案。业务工具仍是被测对象，不是Verifier的裁判。

| 公开合成场景 | v2结构结果 |
| --- | --- |
| 缺用量和合同 | NEEDS_MORE_EVIDENCE；REQUEST_INFORMATION |
| 完整、零用量、窗口内 | ELIGIBLE；HUMAN_REVIEW |
| 已有用量 | INELIGIBLE；NO_ACTION |
| 新政策窗口恰好7天 | ELIGIBLE |
| 7天加1秒 | INELIGIBLE |
| 旧政策生效期最后一秒续费 | 旧14天政策；ELIGIBLE |
| 新政策生效期首秒续费，申请在8天后 | 新7天政策；INELIGIBLE |
| 申请恰好处于窗口起点 | ELIGIBLE |
| 另一租户的相同工单ID，已有用量 | 独立事实；INELIGIBLE |

**未解决的回复语义问题：**

- `REPLY-001`：CS-1044的申请在续费后8天，属于14天窗口；实际拒绝原因是用量1超过上限0。模型却写“申请已超出退款窗口”。结构字段正确仍无法保证回复正确。
- `REPLY-002`：CS-1046实际超过续费后7天窗口1秒。模型写“工单收到后的7天内提出”，改变了起算点。

这些是对公开合成事实的人工式检查观察，不是独立SUP Oracle已实现的自动Finding。已保存实际文本、Job ID与响应摘要。没有给所有回复算语义PASS率，也没有自动修正或发送这些草稿。

## 4. 实测环境与身份

- 现有Ollama 0.12.3，现有模型 `qwen2:7b`，没有下载模型或调用云端API。
- 模型API实际观察的manifest digest：`dd314f039b9d54d5553002c906ce50c9fe7242f73f0680abd04f01c8ecbd2755`。
- PostgreSQL镜像：`postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`。
- Redis镜像：`redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499`。
- v2来源文件清单摘要：`d806ba157c394992d414fa996582fa1968226dfb6d6f15ba494fbe612f7c5f06`。
- 来源记录为 `WORKING_TREE_CONTENT_SNAPSHOT`：基于当时HEAD `702b11ac778b157b996141ba9c8fab4c1c22bd32` 加本轮实际SUT文件摘要；不虚称运行的是该旧commit的原始内容。提交后SUT内容仍与此摘要一致，宿主工具不在该SUT摘要范围内。
- 合成事实SHA256仍为 `806ead4ec1e6f8a2d801181dd99ec4134e0c80d0a163268ac222263e439f2152`。
- v2实际端点：API `http://127.0.0.1:54025`，gateway `http://127.0.0.1:54012`，Ollama `http://127.0.0.1:54010`。gateway/业务API需要各自会话；health不触发推理。
- 进程租期到 `2026-11-06T05:44:20.045Z`（悉尼16:44）。到期进程停止；数据库容器和卷仍需显式down清理，不能宣称自动清理全环境。

专属Docker bridge使用loopback发布与认证。初次internal网络在当前Docker Desktop没有可用宿主发布端口，失败已记录并清理后调整拓扑；当前不能宣称网络出口隔离。模型进程使用独立本地HOME并读取已有模型文件，不读取云模型密钥。

## 5. 验证结果与证据

| 检查 | 实际结果 |
| --- | --- |
| Python完整服务套件 | 104/104，无跳过；含14个真实Store数据库测试、3个真实网关数据库测试、4个真实PG+Redis恢复测试、13个宿主边界测试 |
| TypeScript现有及扩展测试 | 108/108；随后新增状态配对检查的genai-source套件6/6通过 |
| 原控制平面进程集成测试 | 3/3 |
| foundation工具测试 | 8/8 |
| typecheck、共享协议漂移检查、构建 | 通过 |
| uv锁离线一致性、已安装依赖兼容性 | 通过 |
| v2真实HTTP API smoke | 4/4 |
| v2公开业务结构校准 | 9/9；语义质量另有已知问题 |
| 当前数据库角色实测 | reader能查四个身份字段，不能读草稿/更新Job；reader与app均不能连接gateway数据库，5/5 |
| 真实服务stop→start | API、gateway、relay、Worker、model重新启动，端点、10个Job结果及持久回执一致；新增模型调用0 |

真实恢复测试覆盖：重复通知只完成一次、旧Worker失联后的claim回收、网关回执丢失后读取缓存不重复提交、Redis通知丢失后从DB重建通知。该测试使用独立测试数据库及Redis DB2，网关为显式SCRIPTED_CALIBRATION；不能把恢复测试的替身说成真实模型故障实验。上述另一次stop→start则使用LIVE环境已完成的真实结果。未做Docker/宿主断电或模型真实请求中断的资格实验。

v2数据库实际记录：10个Job、10条outbox、10次attempt、30条Job事件、10条COMPLETE网关调用、70条工具事件。9个业务场景加API smoke的一次合法业务提交，合计10次真实模型调用。预算上限12次，每次最大输出1024 token；预留10次/10240输出token。实际模型回执共输入11941、输出775、总计12716 token；这是本地服务返回的usage，不是独立计费证明。两轮真实模型共20次，各自独立预算；没有云API费用。

证据位置：

- 本轮测试日志：`artifacts/evidence/genai-live/20261105/`。
- v1失败：`artifacts/evidence/genai-live/environment_493b663a-02f6-4f33-b297-88ec94a45d40/`。
- v2 smoke：`artifacts/evidence/genai-live/environment_38db0dd8-6c13-4515-b767-1c3f3f6d8e4f/action_e4b2c26c-4cc0-48a5-84aa-cc72a556131d/`。
- 同v2环境证据目录：`before-restart.json`、`after-restart.json`、`restart-verification.json`。
- 独立运行账本及私有进程回执：`artifacts/lab/genai-support-runtime/`。

秘密及原始实验文件保留本机忽略目录，私有配置0600，不推送GitHub；提交代码、依赖锁和本目录脱敏报告。Job请求不接受自报tenant，Redis消息不提供授权身份，工具事件来自实际工具调用，模型不能自造这些观察记录。

## 6. 当前使用与下一步

在项目根运行：

```sh
pnpm support:status       # 查看当前环境及进程、端点
pnpm support:stop         # 停止本实验进程，保留数据库和卷
pnpm support:start        # 相同来源与模型下恢复，复用端点和预算
pnpm test:support:live    # 使用独立测试数据库运行完整套件
pnpm support:down         # 精确停止并销毁本实验容器/卷；实验数据库会删除
```

当前环境保持运行。可查看 `http://127.0.0.1:54025/health/ready`：返回真实依赖就绪，且明确 `business_verification: not_performed`。模型名称/端口为本轮实测值，新环境可能不同。`support:smoke`已经执行，本环境再次调用会拒绝，以免重复提交并消耗预算；应读取现有Job和证据。精确清理后，新实验使用 `pnpm support:up --mode live --model qwen2:7b`；运行会绑定当次文件内容摘要。`pnpm dev:support`仍是原骨架入口，不负责本轮完整部署。

下一步可围绕06–08补外置业务契约、可信观察和独立Oracle，再接Verifier的风险规划与测试选择。优先把已发现的“结构正确但解释错误”变成可重复的语义一致性验收，和政策生效/窗口/租户/缺失证据这些确定性检查分开记录。当前业务事实与参考仍为DRAFT公开合成材料，没有企业业务Owner批准；LangGraph Verifier、变更影响选择、隐藏测例、候选执行隔离、Final证据门禁、人审和发布资格均未在本轮实现。

本轮没有调整模型直到所有自然语言样本“看起来通过”，也没有把通过公开校准当成系统已达到生产标准。保留可观察失败，是后续构建独立验证系统的实际输入。
