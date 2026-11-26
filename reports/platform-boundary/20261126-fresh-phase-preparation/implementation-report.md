# 新分阶段实验的独立访问与候选绑定

日期：2026-11-26。用户已选择虚拟 Jira/规则接入。本轮继续在已运行的虚拟来源和独立原生平台上迁移实验准备；没有修改 docs 或 README，也没有声称已接通真实 Atlassian 或公司批准流程。

本轮完成：创建两个新业务对象 → 独立实验访问入口 → 精确对象授权 → 专有商业来源 → OPEN 初态独立读回 → 独立候选部署 → 实际镜像/对象/来源绑定。当前候选中没有提交业务 Job，也没有调用模型。这不是迟到用量业务已经通过，更不是 Agent 自主执行或 Final。

## 1. 实际对象和部署

| 对象 | 固定身份 | 本轮观察 |
| --- | --- | --- |
| 实验 | `evidphase20261126a1` | 两组新对象，已知场景；未复用历史 seed |
| 原生平台 | `evid-business-4b276a796f10` | 继续复用独立 Chatwoot、Lago、Keycloak |
| 新访问入口 | `evid-access-d4313e97c96b` | 独立 PRODUCT/OBSERVER/OPERATOR 口令、范围数据库和内部产品网络 |
| 新商业来源 | `evid-commercial-d181eff24baf` | 独立 PostgreSQL 和 PREPARER/PRODUCT/OBSERVER 服务；18 条准备命令完成 |
| 新产品部署 | `evid-sut-da3b086a786e` | LIVE_MODEL 配置；真实运行身份已核验，未提交 Job |
| SUT 版本 | `76076dd17368a8cf5627e45758db25300b2a2bd2` | 与上一轮固定产品代码相同；本轮新增部署与对象，不冒充新业务版本 |
| SUT 镜像 | `sha256:fa5ac89e8c77bfa8356d01b2102ee266ce8326c7a063d2504ed57562209830cb` | 构建回执、Git tree、镜像和实际产品容器一致 |
| 模型 | 已安装的 `qwen2:7b` | 固定权重摘要，8 次调用上限；本轮该部署调用数 0 |
| 规则来源 | `evid-sources-6b1f4995a5dc` | 通过独立 HTTP 来源读取，共 8 次，保留当次 ACL 回执 |
| 规则版本 | `3e49cb1559242bea1f93ced0a5cc4aeb9ea674f5` | 独立规则仓库和发布原件摘要一致 |

独立平台新增代码已提交为 `a9ef2b50d7a50524adc6bae1286b62e25ab20f01`。访问入口镜像为 `sha256:a18f64b7ebea4fce19ae0802c242a49624194001e5850498b2b4d78c4d71f893`，来自独立平台的已提交 Git 对象。商业来源继续使用前一轮固定镜像 `sha256:9a763e61da339c15065f6da655ce9b9670600607803da1d55f7f752a007a3dbc`。

## 2. 两组实验当前停在哪里

两组均属于 tenant-b，拥有各自的新订阅、发票和会话；共享本轮新建的合成客户。

| 场景 | 预登记的业务时刻 | 当前实际状态 | 后续要区分的业务关系 |
| --- | --- | --- | --- |
| `late_usage` | 原申请之前发生，但稍后送达 | 主指标 0；覆盖 OPEN；事件待处理且未发送 | 迟到事实到齐后，应按原申请截止点重新判断 |
| `post_claim_usage` | 原申请之后发生 | 主指标 0；覆盖 OPEN；事件待处理且未发送 | 整账期有用量，不等于申请之前有用量 |

每组 9 项初态检查：指定原生指标为零、覆盖 OPEN、事件已预登记、待处理数量准确、尚未收发、未核对/封存、申请与原消息固定、原生发送账本没有该事件、本轮新对象。18 项均成立。

业务时钟是本地模拟时钟，申请时刻为账期开始后两天；真实 Lago 发票仍记录为 INITIAL。本轮没有把它描述为真实支付或自然续费事件。事件的业务时刻和原生投递时刻继续分别保存。

访问边界验收写入了一条明确标记为实验验收的私有备注；它不是客服模型产出的草稿。J1/J2 和两条主指标事件均未执行。

## 3. 代码职责与调用关系

| 文件 | 已落实行为 |
| --- | --- |
| 独立平台 `tools/access_scope.py` | 从已提交代码构建访问镜像；固定父平台、seed 摘要、网络 ID 和租期；创建独立角色口令。旧入口的 grant 不扩张 |
| 独立平台 `platform_access.py` | 角色与对象范围检查增加租期；修正 SQLite 连接生命周期；保持未知写不自动重发 |
| `tools/platform-boundary/phase_experiment.py` | `prepare-native` 固定实验输入并调用 NativeObjects；`prepare-source` 装配准备者/观察者并读回初态；`bind-candidate` 核验实物部署与计划版本、只读账号和空任务状态 |
| `tools/platform-boundary/bind_platform.py` | 原生对象归属核验、不可变 grant 登记、跨实验负例；读取真实容器/镜像/配置摘要/网络与端口 |
| `tools/multi-system/business_source.py` | 原生写入仍使用准备者 runtime；查询可以显式注入独立观察者客户端，避免混用管理员读权限 |
| `tools/multi-system/release_seed.py` | 初态商业 GET 使用 OBSERVER 服务；不把查询发给 PREPARER-only 服务；保留原生发送账本检查 |
| `tools/product-boundary/platform_attachment.py` | 新产品仅取得本实验 PRODUCT 口令和对象映射；分别连接 Keycloak 所在网络与本实验业务入口网络 |
| `tools/product-boundary/commercial_attachment.py` | 新来源的 seed/enrollment/实验入口必须与产品一致，才能挂入只读产品配置 |
| `tools/product-boundary/product.py` | 新增 `--platform-access-id`；实际部署继续要求固定受信构建回执，不能凭自报 commit 启动任意镜像 |
| `tools/product-boundary/observe_native.py` | 已有观察入口可选择当前部署绑定的实验 OBSERVER；不回退旧入口或管理员账号 |
| `tools/product-boundary/model_attachment.py` | 与商业来源、访问入口统一处理 Docker Desktop 的 `/host_mnt` 路径映射，仍核对准确挂载或配置摘要 |

主要链路为：操作员 CLI → 固定输入/来源绑定 → 原生准备与来源命令账本 → 独立角色 HTTP 读回 → 候选部署身份预检。当前没有将这些准备写权限开放给规划 Agent。

新初态恢复逻辑把“写命令复用”和“当前观察”分开：18 条已完成商业命令复用原回执，不再 POST；原生与来源初态重新 GET。第一次资格文件保留，后续观察另存；如果当前不再 READY，不能借旧文件继续。已经进入事件派发阶段时，初态准备入口拒绝重走。

## 4. 实际验证

| 验证 | 结果 |
| --- | --- |
| 新旧实验 HTTP 权限 | 19 项通过，包括双角色读取、未知对象拒绝、旧 token 进入新入口 401、旧入口读取新对象 404、新 token 进入旧入口 401 |
| 写权限 | OBSERVER 写备注 403、PRODUCT 发公开消息/写计费 403；允许写入本实验私有备注且独立读回一致 |
| 原生身份 | 两个普通客服身份的 Keycloak introspection 核验通过 |
| 初态 | 两组共 18 项成立；主事件未发送、覆盖未封存 |
| 准备恢复 | 商业命令仍为 18，实际 POST 回执仍为 18；规则读回从 4 次增加为 8 次；没有新增主事件 |
| 候选绑定 | 实际 commit/tree/image/build、平台、商业来源、实验入口、对象摘要一致；重新调用返回原绑定已核验 |
| 产品空态 | 当前产品库 Job 0、交付 0，Gateway 模型调用 0；观察角色关闭默认只读事务后仍无 UPDATE 权限 |
| 主仓库针对性测试 | 72 项通过：产品边界 19、阶段准备 4、业务来源 17、阶段端口 11、release seed 21 |
| 独立平台测试 | 8 项通过；其中本机 HTTP 测试须允许 loopback 监听，首次沙箱运行因监听受限，授权环境重跑通过 |

没有新增模型调用，因此没有新的草稿语义结论。测试数量是本轮相关测试合计，不代表整个项目全量测试。

## 5. 运行问题和精确清理

发现并修复 Docker Desktop 返回 `/host_mnt/Users/...` 时，主机把它直接当 macOS 路径读取而失败的问题。只有 macOS 的固定虚拟机前缀被映射；不搜索同名文件，不忽略摘要和软链接校验。

新来源第一次启动遇到 Docker 地址池耗尽。先通过旧产品只读数据库核对 5 个 Job DONE、5 个交付 COMPLETE、5 个模型调用 COMPLETE，然后清理本任务旧部署：

- `evid-sut-db8bc9fc36c3`：容器和网络已移除，3 个卷保留。
- `evid-commercial-50a320817b83`：容器和网络已移除，1 个卷保留。

没有清理用户旧 `egap-multisystem-*` 资源，没有执行全局 prune。旧运行证据保留，其“曾运行成功”与“当前仍在线”分开记录。

## 6. 当前边界与下一步

- 这些是新对象上的已知主实验/对照，不是未见样本，也不能计算未知问题发现率。
- 准备/来源维护者、观察者是本地模拟身份；Docker 和本机操作员仍是信任根。Lago 原生 key 的只读标记未形成可靠强制边界，访问代理仍是必须的权限边界。
- 本次只冻结了部署前提。未完成这套独立部署下的 J1 → 事件发送/观察/封存 → 原请求重试 → J2 → 旧快照读回；没有 Verify Graph 自主调用的新业务证据。
- 已有旧分阶段执行脚本仍依赖原工作区与旧配置，不应直接用它启动新实验。下一步应迁移执行与只读观察装配，使用已有 PostgreSQL 操作账本，逐步核验本轮实际候选绑定，处理 UNKNOWN 原操作对账，并验证中断恢复不重复发事件或调用模型。
- 之后再把可用阶段能力接回主图决策/准入；Provider Contract、Project 1 handoff、Codex 修复与独立复验发布仍属后续范围。

原始 seed、HTTP 回执、发布原件、角色配置和数据库连接只保存在 ignored 私有目录。`evidence-index.json` 提供证据路径与摘要，未上传原件或凭据。
