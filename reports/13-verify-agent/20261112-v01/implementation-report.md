# Verify Agent V0.1 实现与真实校准报告

本轮已实现可运行的 **真实模型 → 主动读取 → PostgreSQL 动作准入 → 新材料反馈 → 检查提案** 闭环。Python 使用真实 LangGraph，TS 是唯一权威 Runtime。已复用固定版本六份 YAML；没有修改 `docs/**`、README、原业务系统或 YAML 的派发权限。

**交付范围是本地发布任务的阅读与规划。** 两份最终校准均完成提案登记；实验具体性和选测质量仍需改进。没有执行业务实验，没有产生当前候选业务覆盖、可信 Finding 或 Final PASS。

结构化结果与全部调试运行索引见 [implementation-summary.json](implementation-summary.json)。该报告只记录已实现和已观察事实，不替代冻结设计文档。

## 1. 已接通的调用链

```text
可信操作员给定 baseline/candidate 完整 Git SHA
  → release_assembler：真实 diff、固定规则、精简目录、受控材料
  → Runtime.acceptRun：固定任务、身份、模型与读取上限
  → Python LangGraph.load_and_reconcile
  → Runtime.decide：预算预留、模型调用/outbox 登记、单次本地推理
  → DecisionProposal：影响假设、目的、引用、READ/CHECK 等动作
  → Runtime.submit：版本/范围/已观察证据/规则读取/预算校验
  → READ 回执或明确拒绝
  → LangGraph.ingest_and_update：加载新依据，继续决策
  → CHECK_INTENT 存为 PROPOSED_ONLY；或有界停止/阻塞
```

模型不能提供任意文件路径、URL、SQL 或 shell 命令。它只能提出登记材料引用和动作参数。规则正文、原业务断言和发布权限不由模型改写。

## 2. 代码阅读顺序与文件职责

| 文件 | 实际职责 |
|---|---|
| `tools/registry/release_assembler.py` | 从指定 Git 对象读取 B/C，提取真实差异；复用固定目录和 provenance；提供规则/源码的不可变材料包；历史答案不进入发布上下文。 |
| `protocols/verify-agent/v1/decision-proposal.schema.json` | 七种外层动作的严格协议；不把 CHECK、FINDING 或 REQUEST_FINAL 当作执行批准。 |
| `protocols/verify-agent/v1/conformance.json` | TS/Python 共用的 61 份合法/非法样本。 |
| `packages/contracts/src/verify.ts` | 严格解析、拒绝隐式转换与未知字段、返回冻结副本；内部纠错只提供安全的校验位置和规则名。 |
| `packages/verify-runtime/src/runtime.ts` | `acceptRun → decide → submit → projection` 主链；行锁、版本依据、预算预留、去重、撤销、UNKNOWN、假设及结果登记。 |
| `packages/verify-runtime/src/store.ts` | 独立 PostgreSQL schema：runs、calls、operations、outbox、events；同一连接内事务。 |
| `packages/verify-runtime/src/context.ts` | 有界上下文、已观察引用、完整规则读取判断；从同一权威 Schema 派生适合本地 Provider 的动作分支与当前有效参数。 |
| `packages/verify-runtime/src/model.ts` | 仅回环 Ollama、模型摘要核对、单次 POST、超时/响应大小边界；不隐式重试。 |
| `packages/verify-runtime/src/server.ts`、`main.ts` | 私有配置启动；每个 Run 的 token 隔离；没有导入任意材料、伪造执行回执、改预算的模型接口。原 control-plane 骨架仍保留自己的健康语义。 |
| `services/verify-agent/src/verify_agent/graph.py` | 真正的 LangGraph：加载/恢复 → 决策 → 登记 → 摄取反馈 → 确定性路由。 |
| `services/verify-agent/src/verify_agent/runtime_port.py` | 受限本机 HTTP 端口；无 POST 重试、代理继承或重定向；Python 不持有数据库凭据。 |
| `services/verify-agent/src/verify_agent/__main__.py` | CLI；报告限定 artifacts/tmp，拒绝覆盖和符号链接逃逸；token 必须是 0600 普通文件。 |
| `services/verify-agent/planner-prompt.txt` | 通用调查指令：从变化形成暂定假设，读取实际规则，提出有证据的下一动作；不硬编码某个业务缺陷或指定测试答案。 |
| `tools/verify-agent-calibration.py` | 从固定真实源码生成隔离的合成 Git 版本对；不修改原 SUT。 |
| `tools/verify-agent-live.py` | 启动/借用本地模型、启动 Runtime、运行 Graph、导出本 Run 私有账本、回收自己启动的进程。 |

关键跨模块调用、事务不变量、失败与恢复语义已写入中文注释。

## 3. YAML 如何进入 Agent

仍然使用固定目录提交 `6b1d4815b4178c070f4442edfc2b5606069a3f11`。来源、责任、流转关系、能力和场景先经过登记校验与白名单投影。规则正文按 provenance 从固定 Git 对象取得。规划上下文先提供材料/规则导航，读取后再展开选测目录。

`known_finding_codes`、历史归因、Oracle 答案和历史报告正文没有作为当前发布任务的提示。材料内容始终是不可信分析数据，不能变成执行指令。旧 `PLAN_ONLY` 装配器与 `model_egress_allowed=false` 没有被放宽；新的可信本地启动入口只服务本次明确授权的本地合成规划范围。

## 4. 已验证的控制边界

- `event_seq` 记录审计，`decision_basis_revision` 表示决策依据；模型记账不会让自己的提案过期。
- 模型调用先登记预算/调用/outbox，再派发；已完成调用重放返回原结果。未知结果保留 UNKNOWN 并阻塞，不自动补发。
- 新提案重复读取同一窗口有自己的幂等回执，不重复扣读取额度；缓存命中不算取得新证据。
- 已消费的提案若尚未读取，撤销仍能阻止它。迟到模型回执不能恢复已停止或撤销的任务。
- CHECK 引用的规则片段必须被完整读到；单看规则名称或源码不足以满足此门槛。
- CHECK 必须引用已观察证据，不能把一个 `R1`/`R5` 字符串当作观察需求。
- Final 永远返回缺口；历史 PASS 与模型假设均不进入当前候选覆盖。
- 本地规划预算实现的是模型/READ 的预留上限。`used` 表示已准入预留，不是精确 Provider 账单。完整生产级五预算池账务尚未实现。

本轮采用 **Runtime 投影重建恢复**。没有第二套 SQLite 账本，也没有宣称实现 LangGraph PostgreSQL checkpointer。数据库集成验证了 Runtime 实例重建和提交/回执窗口；完整多主机、全链路进程故障注入仍未完成。

## 5. 真实模型校准结果

两例都是 `CALIBRATION_SYNTHETIC`，不是新真实 PR，也不是上游缺陷。它们的 goal 与适用规则相同，仅版本差异不同。最终两次使用同一系统提示词、`mistral:latest` 权重摘要与生成参数；各最多 5 次模型调用、3 次 READ。

| 校准变化 | 实际运行 | 最终提案 | 能确认什么 |
|---|---|---|---|
| 时间窗口上界从 `<=` 改为 `<` | 4 次模型调用；读取候选代码、R1、R2 | R2 / `check.business` / `v2_boundary` | 本次识别到相关业务方向，并选到能区分该变化的场景候选。 |
| 从决定投影中移除 `reason_codes`、`missing_evidence` | 4 次模型调用；读取 R5、R1、R2 | R5 / `check.business` / `v1_history` | 本次关注了决定数据传递与输出语义，已完成读取后提案登记。 |

对应私有证据：

- `artifacts/verify-agent/20261112-window-bounded-01/full-evidence.json`
- `artifacts/verify-agent/20261112-projection-bounded-01/full-evidence.json`

两次都只有 `PROPOSED_ONLY`，`current_candidate_coverage=[]`、`business_verdict=NOT_EVALUATED`。`PLANNED` 表示已存档一份通过基础准入的提案，**不表示测试规划质量通过或业务通过**。

规划质量仍有明确不足：窗口提案未展开“恰好 604800 秒仍在窗口内、超出 1 秒的对照、实际函数/业务字段读回”；投影提案未解释 `v1_history` 如何揭示理由或补证信息丢失，也未读取候选完整源码。部分 READ 的说明与实际选中的规则不一致，观察需求仍有“收集规则输出”这类笼统句子。

这两个样本参与了本轮调试，属于迭代校准，不能用来宣称稳定性、泛化能力或节省 QA 成本已得到证明。详细质量结论见结构化报告。

## 6. 联调中发现并修复的问题

1. Provider 不完整支持 `if/then` 生成约束：改为从同一协议派生明确的动作分支，最终仍用原 Schema 校验。
2. PostgreSQL `jsonb` 重排了 Schema 字段，模型先填 payload 再选动作：改为保存 `request_text` 并原样发送；真实 PG 测试验证 HTTP 字节一致。PostgreSQL 对该行为有明确说明：[JSON 数据类型文档](https://www.postgresql.org/docs/current/datatype-json.html)。
3. 模型把 diff hunk 的源码行号当成材料行号：提供明确的材料行数及有效分页窗口，非法范围仍由 Runtime 拒绝。
4. 无具体原因的 INVALID 无法有效纠错：反馈安全的 `keyword/path`，例如重复引用的 `uniqueItems`；原始模型响应和已报告用量保留。
5. 模型未读取合同就编造约束、或缺少证据：增加完整规则读取、非空证据、观察细节的门槛。
6. 缓存提案重放映射、UNKNOWN 迟到扫描与撤销窗口存在竞态：均已修正并增加真实数据库回归。

之前失败的 Qwen/Llama/Mistral 开发运行保留在结构化报告索引中，没有删除失败后只展示成功。这些运行使用过不同实现/提示，不是公平的模型排行榜。

当前模型、Prompt、协议与 Runtime 构建身份分开记录并进入配置摘要。窗口最后一次运行开始于构建身份记录功能加入前，因此其 `runtime_build_sha256` 为缺失；保留原值，不事后补造。该次的实际请求文本、模型摘要、候选身份和响应均已归档。

归档的是 **Verify Agent 规划模型** 的实际请求与响应；原客服 SUT 的模型输入/输出观察能力没有因此自动补齐。原始模型/材料和 token、数据库配置都只保存在私有 artifacts，未提交 Git。

## 7. 验证与复现

- TypeScript 类型检查通过；全仓测试 **273/273**，其中包括真实 PostgreSQL **22 项**，无跳过。
- Python Agent 测试 **31/31**；包含共享协议 corpus 的跨语言校验。
- 目录/装配测试 **89/89**。
- TS/Python 共用的协议 corpus 为 **61** 份；协议、生成约束和数据库测试不等价于 Agent 业务推理质量评测。

已提供脚本：

```bash
pnpm typecheck
pnpm test:verify
pnpm test:verify:python
pnpm test:registry

pnpm verify:calibration --output-directory artifacts/verify-agent/new-inputs
pnpm verify:live \
  --bundle artifacts/verify-agent/new-inputs/window-bundle.json \
  --database-url-file /path/to/private-postgres-dsn \
  --output-directory artifacts/verify-agent/new-run \
  --model mistral:latest --max-model-calls 5 --max-reads 3
```

新环境需先按锁文件创建独立 Python 3.14 环境（uv 0.13.7），执行 `pnpm install --frozen-lockfile` 与 `pnpm build`。本机已完成安装。Live 启动器要求已安装本地模型和可用的独立 PostgreSQL；DSN 文件须为 0600，不提供密码到命令行。无 `EGAP_VERIFY_TEST_DATABASE_URL` 时 PG 单测明确跳过；本报告的最终回归提供了真实测试 DSN，22 项实际执行。

## 8. 尚未接通与下一步

尚未接通业务 Runner/Observer/Evaluator、可信 CheckResult、Final、Codex 修复回路和 MCP Server；也没有企业身份接入、恶意候选沙箱或不可篡改证据存储。本期是本机进程/接口范围控制。

下一步应先收紧检查提案的业务可执行性：明确变化行为、需要区分的结果、场景阶段、观察对象、预期关系和对照；用独立样本评估读取与选测质量，并将缺项作为一次有界修正反馈。通过该门槛后，再接一条真实、已校准的业务实验到同一 Runtime，而不是直接把全部目录能力设为可执行。
