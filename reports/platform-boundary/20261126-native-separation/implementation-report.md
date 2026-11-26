# 独立业务平台与产品接入：本轮实现记录

日期：2026-11-26。范围：既定拆分任务的第 3 步，以及第 5 步的原生身份关联基础。

本轮确认：独立 Chatwoot、Lago、Keycloak 已实际部署；使用新原生客户、订阅、发票和会话，完成了产品读取、私有备注写入和观察者只读访问。独立客服候选的 5 个任务经过真实 API、数据库、队列、Worker、Gateway，最终在 Chatwoot 留下对应私有备注。

这轮是 `SCRIPTED_CALIBRATION` 的传输校准。5 个草稿均为 `NEEDS_MORE_EVIDENCE`。商业合同/政策/覆盖账本没有接入此新部署，模型调用数为 0；不代表退款规则验收、旧问题修复、未知样本评测或候选 Final。Jira 与规则来源沿用前一轮的独立模拟服务，尚未接入真实企业 Jira。

## 1. 实际版本与部署

| 对象 | 已确认身份 |
| --- | --- |
| 独立业务平台仓库 | `https://github.com/guangxinsuuu/EvidBusinessPlatforms`，私有 |
| 平台部署代码 | `36748c71cfe20eb73874d2b12f89a3e607f4d158` |
| 平台后续普通调用身份导出代码 | `832c69cce5143e1db8bc23f31910d7ad4268cbba`；此变更不替换已运行访问边界镜像 |
| 平台实例 | `evid-business-4b276a796f10` |
| 访问边界镜像 | `sha256:5d094a4389680ca6bcf0218e3c14026c7bbd0f3c37286a1ec7210aa3d8e94d97` |
| 独立产品仓库 | `https://github.com/guangxinsuuu/EvidSupportSUT`，私有 |
| 本轮产品代码 | `3d9bfb18275998426571d844f89cc4ed87b5ec6f` |
| 产品镜像 | `sha256:d4331273650677c59f478e69b2e7b937a95456d05cf5f46bc8c5e3ac31f893b2` |
| 新产品实例 | `evid-sut-bd15fa5ea3e8` |
| 构建操作 | `b00c6c0da1de4beba32e384ead8465ad` |
| 真实传输实验 | `81b358de93fd4e818e1e6cd141d405e2` |

平台使用固定原生镜像：Chatwoot 4.19.1、Lago 1.57.0、Keycloak 26.8.2。准确 image ID、container ID、端口和部署来源位于私有原生回执，摘要登记在 `evidence-index.json`。未修改旧 `egap-multisystem-*` 服务，也未把旧对象或旧 PASS 搬到新实验。

平台仓库保留原 `services/commercial-source` 的抽取历史，新增独立原生部署与访问边界。商业来源代码已经保留在该仓库，但本轮没有把商业来源服务部署并接入新候选。

## 2. 三种身份如何分开

| 身份 | 实际权限及用途 | 产品能否获得其凭据 |
| --- | --- | --- |
| 实验准备者 | 通过原生 API 创建合成客户、订阅、发票、事件和会话 | 否 |
| 平台操作员 | 初始化独立身份；登记精确业务对象范围 | 否 |
| 产品调用者 | 获准客户/会话/发票/订阅读取；仅写内部私有备注 | 仅产品 API 角色获得边界 token |
| 只读观察者 | 从平台边界读取原件，从产品数据库 SELECT | 否；分别保留专有凭据 |
| 普通客服调用身份 | 取得实验 OIDC token，提交产品争议处理请求 | 验收驱动持有自身实验登录身份，不持平台管理员 key |

Chatwoot 的准备者为 administrator，产品与观察者分别为独立 agent，并加入本次测试 inbox。Lago 分别创建原生 key，但实际核实当前安装的 `api_permissions_enabled?` 为 false。因此不能声称 key 上的只读权限已经由 Lago 执行；强制的方法、对象和写入限制在平台侧 `platform_access.py` 执行，原生 key 不交给产品。

访问边界不会裁定退款资格，也不会过滤掉范围外记录后冒充完整原生结果。无法证明归属时返回失败，调用者必须保留缺项。写操作先记持久审计，再发原生请求；结果未知时保留 UNKNOWN，不自动重发。

## 3. 本轮具体代码与调用关系

### 独立平台

- `tools/deploy.py`：固定原生镜像、随机新凭据、独立数据卷、身份初始化、访问边界构建与精确实例管理；`export-caller` 单独导出业务调用身份，避免为补一个文件重做原生初始化。
- `platform_access.py`：角色鉴权 → 对象/方法准入 → 原生 API → 来源归属检查 → 持久操作回执。
- `Dockerfile.access`：固定基础镜像、非 root 用户、持久边界账本；构建仅使用提交中的文件。
- `deployment/native-template.json`、`provenance.json`：原生平台固定部署模板与迁移来源。

### EGAP

入口链：`product.py up` → `deployment.prepare()` → `platform_attachment.attach()` → `bind_platform.binding()` → 产品最小角色配置 → Compose 首次启动。

- `config/external-platforms.v1.json`：登记独立平台仓库与平台身份。
- `tools/platform-boundary/bind_platform.py`：核对 Docker 实际镜像、实例、端口；回读 seed 中的会话、发票和订阅归属；原生 inbox 成员对账；向平台登记不可变对象范围；执行三角色验收。
- `tools/product-boundary/platform_attachment.py`：逐项匹配客户、会话、发票、订阅和 OIDC subject，白名单投影产品配置。不会复制准备者口令、观察者口令或校准答案。仅 API 连接平台受限网络；Gateway 使用产品已冻结的外部快照。
- `tools/product-boundary/product.py`：新增显式平台接入参数；保存角色配置摘要；启动/恢复时检查配置与原平台容器、网络身份。
- `tools/product-boundary/observe_native.py`：普通客服提交 → 只读数据库对账 → 异步任务观察 → 平台原件回读 → 任务/输入/正文/会话关联。未知提交保持原 key，恢复不会生成新 key 重发。
- 对应测试覆盖配置凭据隔离、对象漂移、网络替换、错误正文、旧输入快照和其他 Job 的误关联。

### 独立产品

`container_runtime.run(api)` → `DisputeService` → `KeycloakIdentity`、`ChatwootClient`、`LagoClient` → `IntegrationStore.admit()` → 原有 Relay/Worker/Gateway → `DisputeService.run_writeback()`。

产品增加容器内部来源的显式允许列表，并在容器 API 启用现有集成路由。Gateway 的业务来源改由角色配置固定。没有改退款判定、订阅解析、用量筛选或旧 503 相关业务算法；没有因缺真实来源而回退 fixture。

## 4. 实际验证结果

| 检查 | 实际结果 |
| --- | --- |
| 新原生数据准备 | 2 个客户，4 个活动订阅和 1 个已终止历史样本，5 个会话案例；原生 API 准备完成 |
| 对象登记 | 2 个租户、5 个案例，原生归属回读通过 |
| 角色验收 | 13 项通过，含读原件、拒绝未登记客户、拒绝观察者写、拒绝公开消息、拒绝计费写、私有备注回读、两个实际 OIDC 身份 |
| 产品任务 | 5/5 受理并完成，私有备注实际写入并回读 |
| 任务/网关/草稿 | 5/5 当前任务、当前输入版本与同份草稿相符 |
| 原生来源收据 | 分别为 8、8、8、8、7 条；每个任务 2 条网关工具事件 |
| 跨租户读取 | 5/5 被产品拒绝 |
| 产品数据库观察角色 | 即使关闭默认只读事务，UPDATE 仍被数据库权限拒绝 |
| 私有备注精确正文补查 | 5/5 与已保存草稿正文一致；针对同一批原始证据，没有算成新实验 |
| 产品测试 | 173 项：129 通过、44 跳过；跳过项需各自指定真实数据库/交付测试环境，未声称全套数据库测试通过 |
| EGAP 部署/观察边界测试 | 10 项通过 |
| 平台访问边界测试 | 5 项通过 |

最初一次受 sandbox 影响的产品测试无法监听本机端口；在允许 loopback 的执行环境重跑后得到上述结果。联调过程修正了两个观察适配问题：Chatwoot inbox 成员响应的 `payload` 包装，以及产品交付结果的 `delivery_state` 字段。修正后沿用原账本恢复；没有用重复提交掩盖观察失败。

本轮所有草稿为 `NEEDS_MORE_EVIDENCE`，缺少 `RENEWAL_EVENT`、`APPROVED_POLICY`、`CONTRACT_RECORD`、`USAGE_COMPLETENESS`。这是新部署尚未接入商业事实来源的实际边界，不能解释为业务验证成功。

## 5. 证据与后续未完成范围

`evidence-index.json` 只提交身份、摘要和结果统计。原始数据库行、业务正文、调用回执和私有配置留在 ignored artifacts / 独立仓库 `.local`，没有进入 Git。平台部署、产品部署和观察回执分别保存，不把产品自报 commit 当独立证明。

仍需完成：

1. 将商业合同、政策、生效事件与 coverage 来源独立部署，分开准备、产品读取和观察身份，接入此产品边界。
2. 将现有迟到用量等分阶段实验迁移到独立平台，并绑定新的原生对象、候选镜像和当前实验；旧样本结果不自动填充。
3. 接入已有本地真实模型的受限出口，重新记录模型/提示词身份、成本与实际文案；当前没有模型调用。
4. 将独立部署绑定接入相应 Runtime 实验适配器，验证其仍遵守候选、输入、观察阶段和权限约束。
5. 延续既定计划，补 Provider 契约与 Project 1 输入边界；本轮没有声称已完成这些接入。

当前产品网络、角色 token、Docker 管理员信任域都属于本机合成实验。没有实现企业 IAM、远程构建签名、生产网络沙盒、多租户数据库 RLS 或企业发布资格。登记范围目前按租户冻结，扩展更多客户/实验对象需要显式范围版本机制，不能直接改已绑定实验。

未修改 `docs/**` 或项目 `README.md`。
