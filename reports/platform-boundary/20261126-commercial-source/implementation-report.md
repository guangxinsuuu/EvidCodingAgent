# 独立商业事实来源接入与当前候选联调

日期：2026-11-26。本轮继续既定第 3 步与第 5 步，未修改 docs 或 README。

## 1. 本轮已确认实现

商业合同、政策、生效事件、申请、付款与用量覆盖账本已在独立来源实例运行。准备、产品读取、观察读取使用不同 HTTP 进程、不同 token、不同 PostgreSQL 角色。产品 API 只挂入来源的产品读取 token，不持准备者或观察者凭据。

复用上一轮创建的 5 个原生对象案例，重新读取独立发布的业务规则，向来源状态机执行 55 条有持久操作 key 的准备命令。随后为固定产品镜像新建部署并实际运行 5 个任务：真实 API → 输入快照 → PostgreSQL/Redis/Worker → Gateway → Chatwoot 私有备注。每个任务均取得来自来源服务观察者的回读，确认产品冻结的商业事实与该来源实例的事实相同。

| 原生案例 | 当前产品实际输出 | 来源与交付观察 |
| --- | --- | --- |
| 客户 A 零用量 | `ELIGIBLE` | 来源、输入版本、同份草稿、私有备注及跨租户拒绝均确认 |
| 客户 A 已使用 7 units | `INELIGIBLE` | 同上 |
| 客户 B 零用量 | `ELIGIBLE` | 同上 |
| 客户 B 已使用 7 units | `INELIGIBLE` | 同上 |
| 已终止历史订阅 | `NEEDS_MORE_EVIDENCE`，缺 `RENEWAL_EVENT` | 同上；没有将终止账单包装成续费正例 |

上表记录的是已知种子的迁移校准结果。本轮没有对这些输出运行适用于新案例的完整独立业务 Oracle，没有把它们标成正式业务 PASS。原有发布 Oracle 的场景目录与这批 `a_zero/a_used/...` 不同，未借改名套用其预期。

## 2. 独立来源的工程边界

独立仓库：`https://github.com/guangxinsuuu/EvidBusinessPlatforms`。

- 源码 commit：`fa9cea9bfbc551d4807a0e35e2cd7c72c8d581d6`。
- 镜像：`sha256:9a763e61da339c15065f6da655ce9b9670600607803da1d55f7f752a007a3dbc`。
- 实例：`evid-commercial-50a320817b83`。
- 构建操作：`600e9a63ca804cf8aa2190fac31ff39f`。
- 数据库为本实例专有 PostgreSQL；三种 API 服务与一次性 init 容器分开。产品网络独立于来源数据库网络，只有来源产品读取服务连接该产品网络。

主要入口与调用链：

`tools/commercial.py` → 固定 Git 文件构建 → `container_runtime.initialize()` → 三个 `create_app()` 实例 → `CommercialStore`。

`Settings.service_role` 明确 `PREPARER / PRODUCT / OBSERVER`。只读服务配置根本没有管理员 token；准备服务不持读角色 token。只读 API 在访问数据库前，核对完整的 tenant/customer/subscription/invoice/account/conversation 元组。知道同租户其他发票或会话 ID 也不获得读取资格。

数据库表归迁移管理员所有。准备者只获账本 DML，不获 DDL；产品、观察者只获 SELECT，且分开角色。服务不会在启动失败时自动升级权限。来源租期为固定 7 天，重启不会自动延长，过期请求由 HTTP 门禁拒绝。

来源拥有自己的 `pyproject.toml`、`uv.lock` 和带 hash 的 `requirements.lock`，不再借产品虚拟环境启动。

## 3. 规则和原生事实如何进入

入口：`tools/platform-boundary/bind_platform.py commercial-scopes`。

此入口重新核验原生平台的实例、镜像和端口，并以已冻结 enrollment/seed 导出 5 个完整对象范围。范围中不含期望资格、模型答案或管理员凭据。

准备入口：`tools/platform-boundary/prepare_commercial.py`。

1. 以平台观察者身份读取原生发票、会话和用量。
2. 使用独立规则平台的 `lab:product-reader` 身份，实时授权读取 `refund-lab-v1`、`refund-lab-v2` 和 `standard-contract` 固定发布版本。
3. 核对 `EvidBusinessRules` commit `3e49cb1559242bea1f93ced0a5cc4aeb9ea674f5` 的准确 blob/content SHA，以及发布者 `lab:business-publisher`；Git 只用于校验，不代替来源服务读取。来源或 ACL 不可用时停止，不回退工作区旧副本。
4. 复用 `BusinessPack.command()` 与 `SourceObjects.prepare_case()`，将固定规则模板和明确实例参数交给来源状态机。准备命令先落账再请求，恢复使用相同操作 key 与相同 payload。
5. 客户的申请时间属于明确标记的本地业务时钟；Chatwoot 消息 ID、实际 created_at 与正文摘要另存。没有修改操作系统或原生平台时间。

`BusinessPack` 新增受信依赖注入：原生种子、平台访问配置、来源配置和原件读取器。旧实验入口保留原文件协议；新入口显式使用独立来源。注入参数不作为模型可选路径或任意 URL 工具暴露。

对已有 used 案例，覆盖账本引用原 seed 的已接受事件回执，随后与当前实际 Lago 聚合核对。此行为标记为 `BACKFILLED_KNOWN_SEED_RECEIPTS_NOT_LATE_EVENT_EXPERIMENT`：没有把已经发生的事件称为本轮预登记/发送，也不能据此证明迟到事件时序实验已完成。真正的迟到事件实验仍须在发送前登记事件并分阶段执行。

## 4. 新候选部署与来源关联

- 产品代码保持 `3d9bfb18275998426571d844f89cc4ed87b5ec6f`，本轮未修改产品业务算法。
- 产品镜像保持 `sha256:d4331273650677c59f478e69b2e7b937a95456d05cf5f46bc8c5e3ac31f893b2`。
- 新部署 `evid-sut-bfc87d2f47f2`。
- 本轮任务实验 `1215838c0f084d21b9848365ffd6f12a`。

`product.up()` → `platform_attachment.attach()` → `commercial_attachment.attach()`。商业来源的原生 scope 集必须引用当前 enrollment；仅 API 加入来源产品网络，Gateway 仍只读产品冻结快照。已冻结部署不允许再次 attach；改变来源应创建新部署。

启动/恢复会检查来源实际容器、镜像、服务角色、只读挂载、非 root 用户、角色配置摘要、网络 ID 和租期。相同镜像不等于相同权限配置；修改挂载配置同样会被拒绝。

`observe_native.run()` 对每个已完成 Job 读取来源观察者 API，把原件与当前 Job 的冻结商业事实直接比较，并核对数据库/网关草稿、私有备注完整正文及目标会话。此观察链不导入产品判断函数，不持来源准备者凭据。

## 5. 真实验证结果与恢复

| 验证 | 结果 |
| --- | --- |
| 商业来源真实 PostgreSQL/HTTP 测试 | 23 项通过，0 跳过；覆盖并发、幂等、不可变版本、覆盖账本与 HTTP 权限 |
| 实际来源角色检查 | 14 项通过：正常读取、错误客户/发票/会话拒绝、读者 POST 拒绝、产品/观察者数据库写拒绝、准备者 DDL 拒绝 |
| EGAP 部署/观察边界测试 | 13 项通过 |
| 准备器协议测试 | 16 项通过 |
| 分阶段种子准备回归测试 | 21 项通过 |
| 原生 + 商业来源任务 | 5/5 DONE、5/5 Gateway COMPLETE、5/5 私有备注准确回读 |
| 商业来源原件对当前冻结快照 | 5/5 相同 |
| 准备器恢复 | 命令仍为 55 条，tenant-a revision 30、tenant-b revision 24；没有新增产品 Job |
| 模型调用 | 0；当前仍是 `SCRIPTED_CALIBRATION` |

真实数据库测试使用新来源 PG 上的一次性专有测试数据库，结束后只删除该测试数据库。来源业务库和其他平台数据没有作为测试清空对象。

部署时发现 Docker 默认地址池已耗尽。先核实本任务旧校准实例 `evid-sut-25dc31b27904` 的 18 个 Job 全部 DONE、无非终态任务，再执行精确 down，保留数据卷和证据，回收其专有网络。之后启动原已登记新实例，没有重新创建候选身份。旧用户 `egap-multisystem-*` 服务未动。

## 6. 事实与后续范围

当前原生 Chatwoot/Lago/Keycloak 为实际运行的服务，客户与数据为合成。商业来源也是实际运行、独立持久化的服务，但合同接受、续费业务时钟和付款仍明确为 LOCAL_LAB 模拟事实；没有真实付款、退款、公开消息或企业政策批准。原生 INITIAL 发票与本地模拟续费期间的明确映射，也不等于证明 Lago 自然续费已发生。

本轮没有修复或复验原来的具体 503 Finding，没有执行未见变化评测，没有取得候选 Final，也没有将旧证据转换为新候选覆盖。

下一阶段继续完成既定目标：

- 将迟到用量等分阶段实验迁移到独立平台，为新业务对象提供有版本、可核对的授权范围。
- 接受已有本地真实模型的受限出口，记录模型身份、实际响应和成本；随后验证其与当前候选及输入的关系。
- 将新部署身份与来源观察接入对应 Runtime 能力，不再依赖旧宿主路径。
- 补 Provider 契约、Project 1 诊断交接与更多来源，保持上游结论作为待核对材料，不能代替当前候选证据。

本轮证据索引为 `evidence-index.json`。原件、凭据与数据库观察保留在 ignored artifacts / `.local`；Git 中只有代码、脱敏汇总和摘要。
