# 20 · 企业来源接入：权限、同步与证据分流

实现状态：`NOT_STARTED`。前置：03–18首期闭环；企业真实数据接入还要求19中的身份、访问和运营能力。可以先在本地合成来源上开发协议；不能把local-dev身份扩展为企业访问权。

## 1. 本章的最小交付

先实现一个受控文件/代码来源Adapter和完整权限、删除、恢复测试，再选择一个真实只读来源，例如指定GitHub仓库或指定文档库。让工程师能看到来源版本、当前权限、更新/删除状态及引用位置；取回材料只能帮助提出假设和合同提案。

无需先搭向量库。小范围全文/结构化检索足以验证同步与ACL；检索质量出现明确瓶颈后再评估embedding。新增来源不复制一套Agent，统一通过Connector协议和现有Graph接入。

## 2. 拟建模块

| 路径 | 责任 |
| --- | --- |
| `packages/connectors/src/contract.ts` | SourceEnvelope、Cursor、ACLDecision、ChangeEvent |
| `packages/connectors/src/local-source.ts` | 可控合成来源，用于重复/乱序/删除/撤权测试 |
| `packages/connectors/src/adapters/` | 明确厂商、API版本和scope的实际Adapter |
| `apps/connector-worker/` | 回填、增量、对账与重试；独立服务身份 |
| `packages/persistence/migrations/` | source_objects、source_acl_versions、sync_cursors、tombstones、index_tasks |
| `packages/knowledge/` | 检索、当前授权过滤和可追溯引用 |
| `tests/qualification/connectors/` | 每来源激活资格测试 |

这些是本章新增的计划包；未进入首期必需依赖。

## 3. 先定义用途和身份

每个ConnectorContract必须包含：connector_id、tenant/project、Owner、允许来源/对象、用途、数据级别、允许操作、凭据引用、身份映射、同步方式、删除规则、最大权限陈旧时间、重试/速率预算与停用流程。真实企业内容进入同步、检索、模型或遥测之前，还须完成[94 数据流转、区域与保留验收](94-data-flow-and-retention.md)的逐流向登记；来源读取权限不自动授予向模型、日志平台或其他区域传输的权限。

凭据引用只能由Connector执行器解析；密钥不进入提示词、索引、SourceEnvelope正文或候选文件。MCP如被采用，只封装工具发现/调用，仍经过同一个服务端权限与参数校验。

## 4. 统一输出与Adapter接口

设计接口示意，尚非可调用SDK：

```ts
interface ReadConnector {
  listSnapshot(scope: ReadScope, cursor?: OpaqueCursor): Promise<SourcePage>;
  fetch(ref: SourceObjectRef, principal: SourcePrincipal): Promise<SourceEnvelope>;
  changes(scope: ReadScope, cursor: OpaqueCursor): Promise<ChangePage>;
  resolveAcl(ref: SourceObjectRef, principal: SourcePrincipal): Promise<AclDecision>;
}
```

`SourceEnvelope` 包括稳定object_id、source_revision、content_digest、acl_digest、observed_at、source_updated_at、classification、allowed_use、content_ref、deleted和trust_status。必需字段缺失时隔离待查，不能由LLM补猜。

revision和cursor首先视为不透明值。每个Adapter声明厂商是否保证顺序、版本可比性和删除通知；不能按字符串大小通用比较版本。没有可靠版本序的源，采用重新fetch当前对象与对账策略。

## 5. 实施步骤

### A. 合成来源与全量回填

创建两租户、三个主体、不同可见范围和版本的合成文档。先以只读Adapter列清单、取对象、解析ACL，再写source_objects与索引任务。索引内容保留对象位置、版本和用途。

### B. 增量同步和恢复

对象变更、ACL版本、索引任务和游标在可恢复事务中登记。处理完成前不得推进不可恢复的水位；崩溃后使用幂等处理重放。Webhook视为待补拉线索，周期对账负责发现遗漏。

厂商API各有分页/delta/cursor语义。实现某个Adapter时再核对该版本官方文档并保存来源；不要把某个厂商deltaLink模型套给所有来源。每次限流/超时保留重试计划和预算。

### C. 删除和撤权

明确删除写有版本屏障的墓碑，防止迟到更新复活。403、扫描中断、缺页和超时先标失效/待核查，不当成永久删除。

撤权先在读取入口阻断，再使索引、缓存和派生摘要失效。每次检索及原文取回重新核验当前主体、租户、用途和源ACL。**超过权限新鲜度预算，或ACL服务不可用，任一条件成立就拒绝受保护访问。** ACL无法确认、主体不匹配或明确DENY也拒绝；不能用尚新鲜的旧ALLOW缓存掩盖服务故障。这与原v3“接入02”的拒绝条件一致。

允许条件必须同时满足 `principal_scope_valid && acl_service_available && acl_fresh && acl_decision == ALLOW`。`acl_service_available`来自本次授权路径的有效服务响应，不能只看上次探活；超时、连接失败、无效响应或无法证明当前可用均按不可用处理。`acl_fresh`按可信时间计算，`age > max_acl_age`为超预算，未来时间戳/时钟不可信时拒绝；边界值测试要与实现一致。返回不同的拒绝原因码供恢复诊断，不把服务故障伪装成源对象删除。

检索过滤、摘要/片段返回、原文下载和进入模型上下文均复用同一授权入口；搜索数量、标题和引用也可能泄露受保护内容，不能仅在点击原文时拦截。服务恢复后重新核验身份、ACL与新鲜度，不能恢复旧缓存ALLOW就直接重放在途发送。

### D. 进入知识路径

输出引用及不确定性，支持Change Impact和ContractProposal。来源内容、注释或工单中的“忽略规则”“批准发布”只按数据展示，不能修改系统权限。源撤权不直接重写历史合同，但触发血缘影响审查和相关后续Gate复核。

### E. 接一个真实只读来源

选定具体项目、账号、允许对象和用途，先通过第94章 `ENTERPRISE_DATA_READY` 门槛，再进行小范围回填及越权测试；确认撤权/删除/游标恢复后再扩大范围。地区、保留或访问者未明确时，只使用合成来源验证。连接成功、数据流向获准与来源业务含义获批分别记录。

## 6. 从资料到运行证据还有哪些条件

普通SourceEnvelope不能直接变成CheckResult。运行观察必须绑定冻结candidate、Bundle、check/template/oracle、环境实例、fixture reset、seed/参数、execution_attempt、lease epoch和collector身份，并证明采集完整与因果关系。

若工单引用了“任务显示成功但结果未持久化”或“执行模型版本与提交绑定不一致”日志，应生成已注册实验提案，在受控环境重新验证。只有历史材料时可以报告“存在风险线索”，不能为当前候选填PASS/FAIL。

## 7. 写回独立建模

初期Connector只读。未来评论、工单状态或文档写回均创建独立ActionIntent，绑定目标、payload摘要、动作、批准、expiry和operation；由17章Action Gateway执行与对账。不能因为Connector拥有token就给Planner写权限。

## 8. 激活测试矩阵

| 测试 | 预期 |
| --- | --- |
| 正确身份读取允许对象 | 内容、revision、ACL及引用位置一致 |
| 跨租户/被撤权主体读取 | 拒绝；缓存、摘要和原文取回同时生效 |
| 缓存ALLOW尚新鲜，但ACL服务超时/不可用 | 拒绝检索结果、摘要、原文及模型上下文；不降级为缓存放行 |
| ACL服务可用，但返回权限版本超出新鲜度预算 | 拒绝；不能以服务在线替代新鲜授权 |
| 当前主体匹配、服务可用、新鲜ACL明确ALLOW | 才允许限定用途读取；记录本次权限证据 |
| ACL明确DENY/无法确认、时间戳异常 | 拒绝并记录原因；不猜测授权，不当永久删除 |
| ACL服务恢复后重放旧读取/外发请求 | 重新授权；旧缓存和队列载荷不继承许可 |
| 内容不变但ACL收紧 | 权限版本更新，不能按content hash忽略 |
| 重复/乱序更新 | 不覆盖新版本；保留去重与对账记录 |
| 删除后收到旧更新 | 墓碑阻止复活 |
| 403/缺页/超时 | 不做全量删除，不填同步成功 |
| 游标失效/索引中途崩溃 | 可回填或补拉，缺口可见 |
| 来源文本指令注入 | 权限与工具allowlist不变 |
| 凭据撤销与限流 | 停止/有界重试，数据不泄露 |

拟建命令：`pnpm connectors:qualify -- --connector <id> --profile synthetic`。必须先创建脚本；真实来源模式另行指定scope和身份，禁止测试自动切live。

## 9. 验收、停用与交接

- [ ] 一个合成Connector通过全部变化/权限测试。
- [ ] 一个真实只读来源通过批准范围的验收，报告真实API/对象版本。
- [ ] 知识和运行证据入口分开；不存在 `verified=true` 升级捷径。
- [ ] 已测撤权传播时间，Owner批准时效和数据保留策略。
- [ ] “权限超龄或ACL不可用即拒绝”覆盖全部内容出口，已测新鲜缓存与服务故障组合。
- [ ] 第94章每条企业数据流的实际区域、保留期限、访问者及删除证据齐全，未知项保持阻断。
- [ ] 停用会停止新同步/读取、处理在途任务并回收凭据，保留必要审计。

原始证据存ArtifactStore，概要保存 `reports/20-connectors/<connector-id>/<qualification-id>/`。连接故障时按来源隔离，不阻断已具有独立证据的原生实验；依赖该来源的动作明确降级或阻断。

下一次可以说：**“按 20 先实现合成文件Connector，验证ACL变化、删除墓碑和游标恢复，再决定第一个真实来源。”**

依据：[外部接入v3](../../EGAP-Order-Approval/docs/external-integration-v3.md)。当前未选择厂商账号、具体API版本或真实数据范围。
