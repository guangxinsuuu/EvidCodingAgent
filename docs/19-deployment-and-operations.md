# 19 · 部署、企业身份与运营恢复

实现状态：`NOT_STARTED`。前置：[15 安全资格](15-security-and-qualification.md)、[17 发布与CI](17-publication-and-ci.md)、[18 试点评估](18-evaluation-and-pilot.md)。本章分本地可恢复部署、staging、生产准入三个检查点，不能一次命令跨过全部阶段。

## 1. 本章可演示结果

服务重启后能找回任务、正确处理在途操作；从备份恢复后能核对证据和远端事实；staging 使用准确已验证制品，并由实际身份控制权限。生产部署只在目标账号、数据范围、责任人及 Gate 明确后执行。

首期参考拓扑保持不变：控制面/DB/队列/证据存储与专属测试宿主分开。云产品只作为可替换基础设施，不能把上游内存客服Demo原样部署后假设持久任务、租户与工具权限已具备。生产适用检查按[93 v3映射](93-v3-acceptance-coverage.md)逐项签收；数据、日志/trace、导出和备份按[94 数据流转与保留](94-data-flow-and-retention.md)登记。目标环境或真实期限未明确时保持阻断，不能从本地成功推导企业生产就绪。

## 2. 拟建文件

| 路径 | 内容 |
| --- | --- |
| `deploy/local/compose.yml` | 控制服务、Postgres、Redis、每角色网络与卷；不挂共享秘密给候选 |
| `deploy/staging/` | 锁定镜像/配置、身份引用、资源与网络规则 |
| `packages/policy/src/identity.ts` | Principal、tenant/project、角色与策略适配 |
| `apps/control-plane/src/auth/` | OIDC验证、会话、CSRF/Origin约束、撤销与审计 |
| `packages/persistence/migrations/` | 最小DB角色、租户约束、逐步数据迁移 |
| `scripts/ops/` | 备份、恢复演练、孤儿清理、对账报告入口 |
| `docs/runbooks/` | 运行、告警、恢复、升级、退役操作手册 |

文件在本章逐步创建；当前均为规划路径。

## 3. 检查点 A：让本地部署可以恢复

1. 为每个组件建立不可变镜像和配置摘要；记录进程角色、端口、卷、网络和启动依赖。
2. 将本地 secrets 从仓库文件移到受控开发配置；每角色只收到必要引用。候选构建没有平台 DB、签名或发布凭据。
3. 配置健康/就绪/优雅停止；停止新claim后等待或登记在途任务，不能把SIGTERM当作副作用撤销。
4. 为实验资源设置租约、TTL、归属标签；清理器先核对所有者和活动状态，再清理，不能遍历删除全部Docker资源。
5. 重启控制面、队列、Worker，验证从 DB/outbox 恢复；队列清空不丢业务记录，重投不重复逻辑动作。

未来命令应提供 dry-run/目标范围以及报告目录。本章实现后再运行，例如：

```bash
# 拟建命令：先实现脚本，仅用于分配的本地隔离环境
pnpm ops:inventory
pnpm ops:recovery-drill -- --profile local-isolated
```

验收不是“容器重新变绿”，而是 Run、Check、预算、审批、外部操作和证据引用一致，未决事项保留 UNKNOWN。

## 4. 检查点 B：接入真实身份并做租户隔离

先选定身份提供方及应用注册，验证 issuer、audience、签名、有效期和 nonce/会话上下文；角色映射由服务端配置，不能接受浏览器提交的 `actor_role=admin`。

| 权限 | 独立检查 |
| --- | --- |
| 提交任务 | tenant/project/repo范围、预算和允许环境 |
| 查看代码/证据 | 当前主体、数据级别、来源ACL和运行scope |
| 批准业务规范 | Owner角色及对应服务范围 |
| 批准候选/发布 | 准确对象、动作、有效期，按组织规则检查职责分离 |
| 执行实验 | 服务身份、环境租约、模板和参数范围 |

单租户试点已经预留 tenant_id；扩展前所有主键/外键/唯一键和查询都检查tenant范围。如采用 PostgreSQL RLS，连接池必须在事务内设置正确租户上下文，并验证连接复用不会泄露前一个租户的状态；RLS不能替代应用授权和对象存储ACL。

否定测试：跨租户ID猜测、被撤销用户、旧会话、错误audience、审批人角色变更、越权下载证据、缓存残留。身份系统不可用时，受保护动作拒绝并保留诊断，不回退匿名管理员。

## 5. 检查点 C：staging 与发布制品

合并提交可能不同于PR head。记录 `reviewed_head`、`merge_commit`、构建来源、OCI及GenAI资产摘要；对实际发布对象完成规定的验证。不能把PR上的旧CI直接贴给不同的合并产物。

1. 创建 ReleaseManifest：代码、制品、依赖/SBOM、配置、合同/资格、数据库和消息兼容矩阵。
2. 制定迁移方案：先兼容新增字段/消息，再切流，最后移除旧路径；每个不可逆动作写明后果和恢复方式。
3. 在staging部署，执行合成客服工单并对账；核对Ticket revision、Job、工具参数/授权、实际Prompt/model配置、政策/索引版本及持久草稿。
4. 根据批准范围灰度。流量比例、观察窗和停止阈值来自业务/环境测量，手册不虚构默认生产数值。
5. 扩量、停止、回退或前向修复各自留记录；回退前确认旧代码能处理当前工单/结果/tool schema、队列载荷、已绑定release及在途Job。

代码、Prompt、索引或模型配置回滚不能撤销已发送的回复、退款或已发生工具效果。异常进入独立业务对账，先阻断受影响新任务；在途Job按冻结release与明确取消/回退协议处理，不删除记录伪造恢复。

生产GenAI升级须独立取得目标用途的质量与动作资格：冻结语义rubric、评估数据、业务切片、统计/重复策略、模型/provider配置及阈值，验证关键政策/权限断言。首期合成草稿、API smoke或Draft PR/CI不能替代自动对客发送/真实退款资格。未建立质量或业务批准时仅运行已授权隔离范围，不能静默标N/A后推广。

## 6. 备份恢复演练必须跨对象类型

备份范围包括 PostgreSQL、证据大对象、Registry批准/撤销、配置与工具版本。密钥采用单独恢复/轮换机制，不把旧短期能力随数据库重新激活。

演练场景：PR已经创建后，将控制面恢复到创建之前的时间点。

1. 冻结新副作用，隔离旧Worker资格；保留只读诊断及有界清理。
2. 恢复DB与证据对象，校验摘要和引用完整性。
3. 按operation、分支、内容与目标查远端；补登记已发生效果，冲突保持未决。
4. 获取最新合同/身份/源ACL撤销事实和删除墓碑，按当前保留期限先处理过期数据，再开放读取；签发新epoch/短期能力，不复用备份里的过期批准。
5. Owner确认恢复范围，限速开放任务；补业务回归和根因记录。

演练输出实际数据丢失区间、实际恢复时间、孤儿资源、对账成功/失败与未决清单。PDF中的RPO/RTO仅是待批准目标，本章不把它们写成已达标承诺。

## 7. 可观测性与值班动作

| 指标/事件 | 用途 | 应对动作 |
| --- | --- | --- |
| 排队、首次反馈、Coding、Discovery、Final、人审耗时 | 找瓶颈，区分系统与人工等待 | 调整容量或任务范围，不能删除验收步骤 |
| stale epoch拒绝、孤儿任务、outbox积压 | 发现恢复/派发问题 | 检查lease、DB与Relay，按范围重新协调 |
| UNKNOWN年龄与次数 | 外部效果不明 | observe-first，超限转人工，不自动换key |
| Oracle缺证据、屏障失效 | 验收可信性 | 暂停对应模板资格，修观察链 |
| 政策/模型身份错配、草稿丢失或revision冲突 | 业务安全 | 停止相关发布/扩量，核查真实业务影响 |
| 成本与清理失败 | 资源控制 | 停止新探索，保留Final/清理预算，回收租约资源 |

trace关联tenant/run/candidate/check/attempt/operation；SUT关联tenant/ticket/revision/job/tool_call/provider_request。记录时间与单调耗时，因果判断依赖独立调用/提交证据；日志默认不复制工单、账单或Prompt正文。

## 8. 完成条件和回退

- [ ] 本地实际恢复演练通过，报告保留失败窗口。
- [ ] 企业身份和跨租户否定测试通过后，才开放对应访问范围。
- [ ] staging验证准确GenAI manifest，迁移/回退与在途Job、Ticket revision、Prompt/政策/工具版本处理可执行。
- [ ] 生产目标、部署授权、值班Owner、SLO/DR及风险接受明确。
- [ ] 第93章生产适用项逐项有证据；第94章实际区域、期限、访问者及备份恢复删除/撤权测试已通过。
- [ ] 未完成的生产项明确标 `NOT_RUN/BLOCKED`，不以Native Pilot通过替代。

证据索引：`reports/19-operations/<drill-id>/`；原始对象进入ArtifactStore。失败时保留禁写、恢复到已验证配置或执行批准的前向修复，不删除操作账本。

下一次可以说：**“按 19 先只实现本地可恢复部署和一次数据库回退对账演练，不接云账号或生产环境。”**

依据：[v3 第39–45及50节](../../EGAP-Order-Approval/docs/eventuate-production-plan-v3.md)、[旧恢复手册](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/docs/upgrade-backup-restore-runbook.md>)；旧手册是参考，其演练结果不计入本项目。
