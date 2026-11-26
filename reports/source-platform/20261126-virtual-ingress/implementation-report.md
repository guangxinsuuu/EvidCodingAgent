# 独立产品与虚拟企业来源接入：实施记录

日期：2026-11-26。用户明确选择“先虚拟接入”。本记录描述实际代码和验证结果，不修改冻结 docs 或 README 中的设计约定。

本轮已跑通：独立来源发布 → HTTP 读取 → Jira 任务规范化 → 独立产品 B/C 装配 → PostgreSQL Runtime 准入和来源权限复核。独立 QA 发布的九例验收资产也已用于产品 HTTP/原生数据库复验。这里的来源、维护身份、合同和批准状态均为本地模拟；没有接入真实 Atlassian 账号或取得公司验收批准。

## 1. 仓库与运行边界

| 仓库 | 本轮实际状态 | 当前版本 |
| --- | --- | --- |
| EvidCodingAgent | 验证控制面、可信产品部署主机、来源客户端、任务装配和 Runtime | 本报告所在提交；前置产品迁移提交 a0dd701、ab0f67f |
| EvidSupportSUT | 独立产品源码、镜像构建与部署工程；不依赖 EGAP Python 环境启动 | ff94cc35dd7c3698ceac1e5057ac8b0e483c7ae0 |
| EvidBusinessRules | 从原业务规则包保留 Git 历史拆分；9 个文件记录原 commit/path/blob/SHA256 | 3e49cb1559242bea1f93ced0a5cc4aeb9ea674f5 |
| EvidAcceptanceAssets | 从固定 EGAP commit 导出 fixture、能力契约、Oracle 和测试，逐文件保留来源 | 9ad29c96d2f43426fe808a0fa86c7a2b483cd60e |
| EvidEnterpriseSources | 独立虚拟来源 API、SQLite 数据卷、发布与对象 ACL | 3fee03abaa3df1c9c8da90bb187e64879e6b4eec |

独立仓库均为私有。拆分保留历史来源，不把导出行为伪装成新的公司批准。EGAP 中的历史产品副本仍用于旧实验回放、部分能力绑定和旧多系统脚本，尚未删除所有历史耦合。

当前主路径产品部署为 `evid-sut-25dc31b27904`，镜像 `sha256:dcffbc1f4c8dcfb6303a1ac3d2dff96a19f83073e91adf2949b9d1b918fd8770`。产品使用独立数据库/Redis、API、Relay、Worker 和 Gateway；受信主机从准确 Git 对象构建，不运行候选提供的部署脚本。产品容器只挂载各自角色配置，不挂载 EGAP 工作区或验收答案。运行资格是 `SCRIPTED_CALIBRATION`，尚不是本轮真实模型/商业多系统发布验证。

虚拟来源实例为 `evid-sources-6b1f4995a5dc`，镜像 `sha256:931bac90b3f13e13b2e7a04dd6f7ed04dbf183f852f862e030455bbd60e286fe`。API 容器仅挂载自己的数据卷，不挂载客户端/发布者口令文件。来源数据库记录版本、发布幂等回执、权限和发布/权限变更审计；目前不提供完整的逐次读取服务端审计日志。

## 2. 发布的十份材料与身份

| 来源 | 固定对象 | 发布身份 | 允许读取者 |
| --- | --- | --- | --- |
| 业务规则 | refund-rules、refund-lab-v1、refund-lab-v2 | lab:business-publisher | planner-reader / oracle-reader / product-reader |
| 合同 | standard-contract、special-contract、incomplete-contract | lab:business-publisher | 同上 |
| QA 基准 | support-synthetic-v1 | lab:qa-publisher | 仅 oracle-reader |
| 能力接口 | commercial-decision-v1 | lab:qa-publisher | planner-reader / oracle-reader |
| 独立 Oracle | commercial-release-oracle-v1 | lab:qa-publisher | 仅 oracle-reader |
| 模拟 Jira | EGAPLAB-100，revision 1 | lab:requirements-publisher | planner-reader / oracle-reader |

业务规则与 QA 资产固定为 `lab-v1-extracted`。ACL 按身份、租户、namespace 和准确 object_id 检查，不能因登录成功就读取整个平台。不同发布者不能相互替换规则或答案；已发布版本不可覆盖。`product-reader` 的来源角色与产品业务调用者并非同一个账号，本轮也没有把 QA reader 配置挂载给产品。

版本原件、摘要、发布人、publication_id、Git commit/blob/path 同时保存。Git 来源由授权的模拟发布者声明并由本地主机核对；没有声称远端签名认证。来源平台不兼容 Atlassian REST API，也不是已完成的通用 Jira Connector。

## 3. 主调用链及代码阅读顺序

1. `tools/source-platform/host.py`：操作员从三个独立仓库读取准确 Git 原件，用三个发布身份调用服务。发布前落账；未知回执只按原 publication_id 查询，不能盲目补发。容器构建超时保留 UNKNOWN。
2. `tools/source-platform/client.py`：固定本地端点、有界 HTTP、不自动重试；核对实例/租户/版本/内容摘要。私有配置必须为普通文件且权限不对其他用户开放。
3. `tools/source-platform/assemble.py`：读取模拟 Jira 和规则版本，将 NEW/PRESERVE 要求规范化成 ReviewRequest。B/C 由操作员指定，Jira 不得自行换候选。所有需求保留 PENDING，不生成公司批准。
4. `tools/registry/release_assembler.py`：用独立产品仓库的真实 B/C 和 diff 生成材料。规则原文从来源 HTTP 取得，片段保留准确字节区间；源读取失败时不回退仓库内旧规则。
5. `packages/verify-runtime/src/source-authority.ts`：固定来源登记、检查绑定，并在使用时重新读取来源。复核任务文本没有被装配器改写，规则片段确实来自固定原件。
6. `packages/verify-runtime/src/runtime.ts`：在准入、任务投影、模型派发、提案提交、READ 和操作结果读取处检查来源权限。READ 回执保留当次来源授权信息。模型派发前来源失败不消耗模型调用额度；派发后的未知仍保留预算，不重试。
7. `tools/product-boundary/observe_product.py`：Oracle reader 从独立 QA 来源取得固定验收资产，产品 API 提交/轮询，独立只读数据库观察，再比对受信参考。每个执行单元重新检查来源，不回退旧 fixture。

Runtime 启动配置新增 `source_reader_config`，只放本机私有配置路径。Bundle、模型上下文和公共报告不携带 token。来源撤权后仍允许取消任务，但只返回最小停止状态，不能借取消接口返回历史原件。

本轮是来源 HTTP 适配器；没有把它冒充为新增 MCP Server。Agent 继续走现有 Graph → RuntimePort → 受控 READ 路径，来源读取在 Runtime 可信组件内完成。

## 4. 实际任务与验证结果

模拟任务 `EGAPLAB-100`：客服草稿服务迁移到独立产品部署。

- B：`800fd247b6818d6951461b44a2b1e5605979d9da`；C：`ff94cc35dd7c3698ceac1e5057ac8b0e483c7ae0`。实际差异为产品部署文件变化，不伪装成未知商业缺陷。
- 新要求 DEPLOY-1：产品不得挂载 EGAP 或独立验收答案。
- 新要求 DEPLOY-2：观察端点和结果必须绑定到准确候选镜像/部署。
- 保留要求 R5：保持失败语义与私有草稿边界。
- DEPLOY-1/2 尚未映射到当前商业检查能力，保留 `REVIEW_REQUIREMENT_NOT_REGISTERED` 缺项；即使 Jira 原件可读，也不自动获得可执行检查或覆盖资格。

| 验证 | 实际结果与边界 |
| --- | --- |
| 来源服务部署与发布 | 独立容器真实启动，10 份原件发布成功 |
| 实际 HTTP 权限 | 产品/规划器读取 QA 答案被拒；跨租户被拒；错误发布者/读者写规则被拒；正确发布者覆盖旧版本也被拒 |
| 撤权及故障 | 刚读过的原件在 ACL 不可用时仍拒绝；撤销一个对象不影响另一对象；检查后恢复权限 |
| 来源 → Runtime | 实际部署服务 + PostgreSQL 导入成功；Run `bf29aed4-7b2d-4f6e-8694-11d3cee804bb` 保存三项要求与两份来源；实际 ACL 故障阻止投影和模型派发；模型调用数 0，结束为 CANCELLED |
| Runtime 回归 | 最终 215 项通过，0 失败，0 跳过；包含真实 PostgreSQL、合成 HTTP Provider 的方向更新 → READ → 来源回执 → 缓存撤权测试 |
| Python 回归 | 36 项任务装配/独立产品身份测试、3 项部署主机测试、3 项来源客户端/任务规范化测试通过 |
| 独立服务与 QA | 来源服务 5 项测试通过；独立 QA Oracle 14 项测试通过，均为本轮较早完成的验证 |
| 产品九例复验 | 从独立 QA 发布取得答案；9 个新 Job 的 API 与原生数据库结果匹配，租户越界读取被拒、观察者写库被拒；不是 Final 或未见问题发现率 |

九例观察 Run 为 `696fc1b4201441aeaabdb698a58dcb1b`，独立 QA publication_id 为 `56355700036d4b6d9f6b6ebcdd4a221d`。原始数据、来源口令及 DB 连接只在 ignored artifacts；本目录 `evidence-index.json` 记录证据路径和文件摘要，不上传原件。

## 5. 尚未完成的范围

- 真实 Jira/Confluence、真实公司维护者/批准、企业 IAM、跨机器或云侧隔离尚未接入。当前 Docker 与本机操作员仍是信任根。
- 当前来源读取为精确对象入口；没有通用搜索、分页、Webhook、增量同步、多任务版本规范化和完整读取审计。首期任务固定为 EGAPLAB-100 revision 1，不能把任意 Jira 内容塞入此接口。
- 权限检查与远端撤销不是分布式原子事务。已经发送给进程/模型的字节无法追回；后续调用重新检查。缓存快照保留审计用途不等于继续有使用权限。
- Keycloak、Chatwoot、Lago 和商业来源服务的独立部署/凭据迁移仍需继续，尤其是准备者、产品调用者、原生只读观察者的跨系统权限。
- 默认独立产品九例 smoke 已跑通；新商业候选的多阶段真实实验尚未迁移到这套部署身份，本轮不新增业务 Final。
- 独立发布的商业 capability/Oracle 已可读取，但旧能力绑定仍部分依赖原项目内的冻结参考，需要后续逐条迁移。
- Project 1 诊断/OpenSpec handoff、通用 provider contract、Codex 修复与独立复验/发布闭环仍未由本轮完成。

下一切片应继续迁移真实商业平台身份与观察接口，让同一次商业实验绑定这套独立产品镜像、各平台业务对象和原生回执，再交给 Agent 选择与执行。不能仅凭模拟来源已接通，就认定六步整体目标完成。
