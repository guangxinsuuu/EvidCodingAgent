# 客服账单争议：真实多系统联调交付

日期：2026-11-09（Australia/Sydney）。资格：`LOCAL_REAL_SYSTEM_INTEGRATION`。

产品侧实现提交：`7d6b5ff`。验证工具、测试及本报告另作提交；原始数据、凭据及生成产物不提交。

## 已确认的结果

已通过真实 API 跑通 **Keycloak 认证 → Chatwoot 会话/客户 → 服务器客户绑定 → Lago 指定发票/订阅/期间 → PostgreSQL Job/outbox → Redis/Worker → 网关/Ollama → Chatwoot 私有备注**。

这些请求实际经过第三方产品并持久化在各自数据库。客户、金额和消息是合成测试数据；没有真实公司团队参与或生产采用证据。没有执行退款或发送对客消息。

外部产品没有改源码。沿用现有 SUT 的 Job、outbox、租约、模型调用账本，增加外部来源与内部备注交付边界，没有重写原异步系统。

## 本次运行身份与环境

- SUT：`environment_5304e7f4-b38b-4038-b50d-ed0a3dcb4832`。
- Seed：`egap20261109023738`。
- 来源：本地 working-tree 文件内容摘要快照，manifest 的 Git 基点为 `2087af9`；新实现的实际内容摘要保存在环境 `source-manifest.json`，不能把旧 Git 基点当作新代码已存在的证明。
- SUT API：`http://127.0.0.1:65321`；模型：`qwen2:7b`，真实 `LIVE_MODEL` 模式。
- 当前 SUT 进程租期到 2026-11-10 13:44:23 Australia/Sydney；过期后不能把这些端点当作仍可用。外部产品栈需用部署工具单独停止。
- 上一轮 SUT 进程已停止，旧数据库卷和证据保留；旧 active 指针归档，没有销毁历史数据。

| 产品 | 固定版本 | 本机入口 | 实际检查 |
|---|---|---|---|
| Chatwoot | v4.19.1 | http://127.0.0.1:58101 | 原生 HTTP、账户认证、会话、消息写入和读回 |
| Lago API | v1.57.0 | http://127.0.0.1:58102 | 原生健康、客户、订阅、发票、事件与用量 |
| Lago UI | v1.57.0 | http://127.0.0.1:58104 | 已部署；业务验收以 API 回执为依据 |
| Keycloak | 26.8.2 | http://127.0.0.1:58103 | 两个真实测试用户签发令牌、实时 introspection |

镜像均绑定 tag 与 digest，端口只发布到 loopback，资源属于独立 `egap-multisystem` project。Keycloak 当前为 start-dev/H2 和测试 password grant，不代表生产 SSO 配置。

## 实际业务对象与交付

通过原生 API 创建 2 个客户、各 2 个活跃订阅、4 张订阅发票，计量事件聚合后当前用量为每客户 0/7。另建 1 个按期末计费订阅，发送 11 units 事件并经原生终止流程关闭期间，生成真实历史发票。共 5 个客服会话。

| 案例 | Chatwoot 会话 | Lago 发票 | SUT Job | 实际交付 |
|---|---:|---|---|---|
| a_zero | 1 | ef5f5449-361f-4992-896b-f67b9b31b387 | 6555247e18964113aee8f2c68867c018 | 私有备注 6 |
| a_used | 2 | 22056d6a-3547-4c41-9765-74f2a70c5fcc | eaaf6b193aca44099d9da1d0a492b690 | 私有备注 7 |
| b_zero | 3 | 77ba529c-d602-4d05-9dc9-d1597e110dd6 | 31f91551a0bf4baaacd0d59307e07c99 | 私有备注 8 |
| a_closed | 5 | b0c0f265-f448-4ca1-b56f-21a60012d7cb | 884f675aed7f4211b8c00e046d09cca4 | 私有备注 9；实际消费 PAST 用量 11 |
| b_used，客户补充资料 | 4 | 5e191f33-3e17-4c57-9ac4-e8f0ff812544 | d57f615c3250434e8d8cc1a4e34c91f2 | Job DONE，交付 STALE，无备注 |

网关账本确认 5 次真实 qwen2:7b 调用，全部 COMPLETE；没有用 scripted_draft 冒充模型。预算为最多 24 次，实际预留 5 次。DONE 仅表示该输入快照的处理完成；最后一项清楚展示 DONE 不意味着允许把过期草稿交付。

## 真实接口暴露的业务问题

1. **发票 ID 匹配也不充分。** 预付订阅的首次发票为整月订阅费，而相同发票 ID 的 past_usage 可能是创建时刻的零长度期间。不能据此将当前 0/7 用量当作争议发票用量。
2. 实现同时核对客户、唯一订阅、发票原生费用期间与用量返回期间。期间不匹配保留 `usage=None` 和 `INVOICE_USAGE_PERIOD_MISMATCH`，仍可生成补证草稿。没有将合法产品语义差异标成 Lago 缺陷。
3. `a_closed` 的 charge fee 区间与 past_usage 精确相符：2026-11-07T00:00:00Z 至 2026-11-09T02:41:17Z。它是**终止关闭账期**，不是已经验证自然月续费流程。
4. 开票日没有映射为续费时间；用量查询成功没有映射为事件已全部到齐。批准政策、合同、续费来源和完整性依据未接入，因此资格为 `NEEDS_MORE_EVIDENCE`，不能拿本次通过作为退款业务验收。

## 验证结果

| 验证层 | 实际结果 | 含义 |
|---|---:|---|
| Python 服务全套 | 149/149，通过；0 跳过 | 包含 22 连接器边界、15 真实 PostgreSQL 集成事务测试、8 编排边界测试，以及原有回归 |
| TypeScript 回归 | 108/108 | 现有协议、适配器、实验账本等回归 |
| 控制面进程测试 | 3/3 | 本机 HTTP/退出与端口生命周期 |
| 部署/seed 准备工具单测 | 9/9 | 写入未知、重启复用、凭据投影、禁止 fixture 回落 |
| 真实多系统端到端 | 39/39 | 原生对象、实际 Job/模型/备注、授权与幂等 |
| 真实依赖故障 | 13/13 | 精确停服、fail-closed、无额外 Job/模型、恢复 |

关键实际断言：

- 同 key 重复提交，以及新 key 重复同一业务输入，均复用 Job；同 key 换发票返回 409。
- 客户 A 提交客户 B 发票返回 403。只读检查产品调用账本，该 B 发票详情读取次数没有增加，不只依据 HTTP 拒绝推断“未读取”。
- 伪造 tenant 返回 403；真实集成模式禁用旧静态 token API，不能绕过 OIDC 读集成 Job。
- 内部备注实际 `private=true`，目标会话及外部消息 ID 读回匹配；重复交付得到同一消息，备注不推进客户输入 revision。
- 旧草稿实验使用明确屏障：核对当前实验 Worker PID/启动签名，SIGSTOP → 接受 Job → 原生 API 追加客户消息 → finally SIGCONT。旧输入最终 STALE，无内部备注，不依赖模型“足够慢”的偶然时序。
- 实际停止 Lago API 后，业务请求返回 503；不创建新 Job，不增加模型调用/预算，不将故障当零用量。
- 实际停止 Keycloak 后，即使 token 刚签发并曾成功访问，也返回 503；没有新增 Chatwoot/Lago 下游查询。
- 停服实验完成后两个依赖均由 finally 恢复，原生健康为 200；SUT 五个角色最后均观察为运行。

首次端到端验收脚本误把原生用量当成顶层 units，保留为 `check-attempt-1.json`。修正验收脚本按指定指标读取 charges_usage 后，复用已存在的四个 Job，没有重新调用模型来掩盖失败。首次沙箱内 TypeScript 测试因监听/进程身份权限受限失败，随后在获授权的本机测试环境完成全量回归。

## 代码阅读顺序与调用关系

```text
integrations/routes.py::submit
  → KeycloakIdentity.authenticate
  → DisputeService.submit
  → 服务器授权 / ChatwootClient / LagoClient.resolve_invoice_scope
  → IntegrationStore.admit（快照 + Job + outbox + 幂等键，同事务）
  → 原有 Relay → Worker → ResolutionGateway
  → ExternalSnapshotTools.collect → external_decision → 本地模型 → Store.complete_job
  → DisputeService.run_writeback / deliver
  → claim_delivery → ChatwootClient.create_private_note → 原生GET读回 → 外部消息ID
```

| 文件 | 本次职责 |
|---|---|
| `deploy/multi-system/compose.yaml` | 固定产品镜像、独立网络/卷及本机端口 |
| `tools/multi-system/deployment.py` | 环境生命周期、组织/身份初始化与原生认证探针 |
| `tools/multi-system/seed.py` | 用真实 API 创建业务对象，记录可恢复写入步骤 |
| `tools/multi-system/sut.py` | 准备私有服务器映射，启动现有异步 SUT；后续准备会剔除部署管理员/测试用户口令 |
| `integrations/http.py`、`chatwoot.py`、`lago.py`、`identity.py` | 服务认证、固定地址、原生对象读取、期间核对、未知写入语义 |
| `integrations/workflow.py`、`routes.py` | 客服请求授权、跨系统对象关联、事实快照与内部备注编排 |
| `integrations/persistence.py`、`migrations/002_integrations.sql` | 原子受理、业务 revision、重复事件别名、不可盲重发的交付账本 |
| `model_gateway.py`、`runtime.py`、`server.py` | 显式接入真实来源，保留既有 Worker/网关流程，禁用旧鉴权旁路 |
| `tools/support-lab.py`、`support-environment.ts` | 外部来源/配置身份、只读快照数据库权限和进程生命周期 |
| `tools/multi-system/check.py`、`fault_checks.py`、新增测试文件 | 独立驱动本地验收，明确区别单测、真实 API、真实故障 |

业务实现内的必要注释说明了调用关系、幂等键与业务输入区别、revision、事务与派发不确定性。既有 docs/README 未修改。

## 本机证据与运行命令

本机原始证据与凭据均在被忽略的 `artifacts/`。密码/token 不应复制进报告、聊天或 Git。

- `artifacts/multi-system/deployment-evidence.json`：真实原生认证。
- `seed.json`、`bindings.json`、`seed-state.json`：原生对象、服务器关联、逐次 API 写入回执。
- `connector-live-read.json`：连接器的真实查询路径、参数和结果摘要。
- `check.json`：39 项联调断言及 Job/备注；`fault-check.json`：13 项真实停服/恢复断言。
- `execution-summary.json`：5 次真实模型回执及 Job 对照；测试日志保存在同目录。
- 环境目录的 `source-manifest.json`、`manifest.json`、`runtime.json`：来源内容、资源与进程身份。

查看现有环境，不产生新业务操作：

```bash
python3 tools/multi-system/deployment.py status
services/genai-support-sut/.venv/bin/python tools/multi-system/sut.py status
```

从没有 active SUT 的新联调环境建立：

```bash
python3 tools/multi-system/deployment.py init
python3 tools/multi-system/deployment.py up
python3 tools/multi-system/deployment.py bootstrap
python3 tools/multi-system/deployment.py verify
python3 tools/multi-system/seed.py
pnpm build
services/genai-support-sut/.venv/bin/python tools/multi-system/sut.py up
services/genai-support-sut/.venv/bin/python tools/multi-system/check.py
services/genai-support-sut/.venv/bin/python tools/multi-system/fault_checks.py
```

seed/check 有实际测试写入；fault_checks 会暂时停止本测试项目的两个依赖。凭据、数据、端口或 source 变化需要新实验身份，不覆盖旧证据。恢复既有来源/身份时用原运行配置，不能把历史通过迁移给新配置。

## 本轮未实现或未证明

- **没有完成 LangGraph Verify Agent、独立受保护 Observer/Oracle、Codex 修复循环、Final Gate、发布批准或可信 CI。** 当前 check 是本地验收驱动。
- 已实现指定发票的多订阅集成路径；尚未正式构造、冻结和比较 I0 基线与 I1 新功能候选。
- 尚无嵌入 Chatwoot 的账单选择 UI、浏览器 OIDC 登录流程或原生 webhook 订阅/自动受理；当前由真实认证 API 请求驱动。重复事件测试指重复业务输入，不能表述成真实 webhook 重投实验。
- 公司批准政策、合同、续费事件、完整性屏障和真实团队验收未接入。测试 mapping 为服务器受控配置，尚无企业动态授权接入与组织维护流程。
- 本机同用户进程与产品日志不是独立防篡改证据；Keycloak 本机开发部署、后台连接器使用管理级实验凭据，不具备生产权限资格。首次运行冻结的私有配置仍有未使用的 Lago 部署字段；准备器后续已缩减投影，不能把后续配置测试称作本轮运行配置已迁移。
- Chatwoot 无跨系统条件写 CAS；派发前核对消息/revision，但客户恰在检查后追加消息仍有竞态。备注包含原 Job/revision，不能宣称端到端原子“最新结果”；UNKNOWN 不盲重发。
- 模型文案未取得独立语义质量验收；有真实调用与私有备注不等于自然语言内容完全正确。
- 未证明自然续费历史账期、真实支付、退款执行、生产容灾、公司多团队采用或 QA/token 节省幅度。

下一阶段应基于这些真实对象与调用收据定义一个可冻结的新版本变更，再建设独立观察、业务裁决与针对性选测。现有公司规则保持外置，模型不能因联调成功自行授予发布或退款资格。
