# 12 · 可信接收候选，并按已验证缺陷调用 Codex 修复

实施状态：`NOT_STARTED`。本章描述拟建实现；所有代码、脚本、接口和证据路径都是设计，不表示当前已经存在。本文编写没有调用模型、修改异步推理业务代码或运行服务。

前一章：[11 · 恢复与预算](11-recovery-and-budget.md)。下一章：[13 · LangGraph 验证规划](13-langgraph-verifier.md)。本章还依赖 [04 · 项目骨架](04-project-skeleton.md)、[09 · 缺陷样本](09-seeded-defects.md)、[10 · 持久化 Runtime](10-durable-runtime.md)。

Coding 只修改 WorkOrder 允许的业务代码及开发测试。批准的模型文件、feature snapshot、输入 schema、隐藏评估集、Oracle 与发布阈值属于独立冻结输入；需要改它们时产生范围/需求变更提案，不能把改验收答案算修复。候选封存必须绑定所有会改变推理行为的制品，而非仅 Python diff；模型字节变化也属于 CHANGED。

## 学完后应能演示什么

主场景先由可信接收器接收“稳定发布基线→含新功能/重构的外部提交候选”，固定完整内容与谱系，再交 Discovery 验证；这个入口无需 CodingAttempt。只有发现可信 CODE_DEFECT 且修复已授权，才给 Coding Worker 当前候选、允许修改范围和已验证反例，在独立工作区产生后继候选。两条路径共用可信冻结器，需发布的候选在 Final 前固定精确 Git commit/tree。你可以回答：验证/修复的是哪个版本、为什么改、是否越界、下一次独立验证拿到哪份内容。

先演示 `scripted-fake` provider 的协议与故障处理，再接入 `live` Codex。两者必须在数据库、界面和报告中分开。fake 根据固定脚本写出补丁，只能证明候选交接协议；它没有证明模型能找出或修复缺陷。Codex 输出“已修复”也只是提案，业务结论来自第 08、14 章的独立执行。

开始前必须已有：稳定/输入版本的可核验身份与适用资格、当前候选及批准需求、独立 oracle，以及 Run/预算持久化。进入修复分支还须当前候选的 ValidatedFinding、允许修改范围和 Attempt。synthetic seed 只在受控缺陷实验中声明，不是所有 Release 的前置条件；示例 V3.4/V4.0 为自有测试标签，不声称上游版本或真实线上历史。若环境/基线构建失败，先回第 03/05 章，不把环境问题交给模型改业务代码。

## 为什么首期选择进程适配器

首期采用 `CodexExecProvider`，由可信 Worker supervisor 启动非交互 Codex 进程。进程边界便于记录启动配置、控制工作目录、限制输出、取消执行和区分退出状态；业务层只依赖 `CodingProvider`，不依赖具体模型或 CLI 事件细节。后续可实现官方 TypeScript SDK adapter，保持同一输入输出契约。

官方资料支持 `codex exec`、JSON 事件输出和沙箱配置；示例参数采用 `--sandbox workspace-write --json`，不使用已不支持的 `--full-auto`。正式实现先记录本机版本并核对该版本帮助，不把本文参数组合当作永远不变的产品协议。[官方非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)

官方 SDK 提供线程启动/恢复能力，可作为另一适配器；线程 ID 是模型会话定位符，不是 EGAP Run、Candidate 或副作用账本。是否使用 SDK 由恢复、认证和运维需求决定，不为了框架统一而立即替换已验证的进程适配器。[官方 Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)

旧 AAA 仓库的 [executor.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/harness-worker/src/executor.ts>) 已展示独立工作区、子进程、工具错误检查、候选提案交接等模式。但它运行 DSH，包含支付/Kafka fixture 和特定凭据配置；不能复制后改名就声称接入 Codex。其 [kafka-candidate.ts](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/harness-worker/src/kafka-candidate.ts>) 对实际 Git 路径、未跟踪文件和 NUL 分隔的检查值得借鉴，具体允许路径须由 ML Release WorkOrder 决定。

## 拟建文件及职责

| 拟建路径 | 职责 |
|---|---|
| `packages/coding/src/provider.ts` | Provider 输入、事件与结果接口，不包含具体 CLI 解析。 |
| `packages/coding/src/providers/scripted-fake.ts` | 确定性协议测试；明确记录 fake。 |
| `packages/coding/src/providers/codex-exec.ts` | 参数数组启动、事件解析、超时与取消、实际用量记录。 |
| `apps/coding-worker/src/supervisor.ts` | claim、预算预留、工作区分配、结果交接；不是模型决策节点。 |
| `packages/coding/src/workspace-policy.ts` | 文件类型、路径、真实 diff、越界与隐藏文件核查。 |
| `packages/artifacts/src/freeze-candidate.ts` | 可信冻结、内容摘要、不可变存储、谱系登记。 |
| `apps/control-plane/src/candidate-intake.ts` | 接收外部提交候选，核对 Stable/ChangeSet/需求闭包，复用冻结器；不启动模型。 |
| `tests/integration/coding-provider.spec.ts` | fake/进程失败/协议边界；不借此声明 live 效果。 |
| `tests/qualification/coding-isolation.spec.ts` | 凭据、控制库、文件逃逸、发布权限的否定测试。 |

Provider 不直接更新 Run 到 `WAITING_APPROVAL` 或 `SUCCEEDED`，不直接发布 Git，不写 oracle 或 CheckResult；它返回候选提案，由 Runtime 和 Candidate Registry 接管。

## 检查点 12.1：先定义交接协议

以下为项目接口伪代码，不是官方 SDK API。实际类型放在 `packages/contracts`，运行边界用同一 JSON Schema 校验。

WorkOrder/Run/FrozenRunContext 在准入时冻结 `task_mode=RELEASE_VERIFICATION|INCIDENT_REPAIR`、`repair_policy=DISABLED|ON_VALIDATED_FINDING` 和独立的 `completion_mode=FINAL_ONLY|DRAFT_PR_CI`。Release 的外部候选先验证；DISABLED 可配 CODING=0。CodingProvider 仅在 ON_VALIDATED_FINDING、当前可信代码缺陷与 scope/预算有效时启动，不能为正常新增功能先跑一次模型。最终 change_kind 比较任务基线：有变化候选按 completion_mode 完成；相对基线完整输入相等且任务满足才可能 NO_CHANGE。Provider 无权改变这些模式或决定终态。

RELEASE_VERIFICATION 的 WorkOrder 冻结 `stable_release_manifest_ref, submitted_candidate_ref, release_changeset_ref, requirement_delta_ref, historical_coverage_refs`。外部提交内容经可信 intake 核验真实 Git 对象、完整输入、来源和批准需求后，生成/登记不可变 CandidateManifest，由第 10 章 RELEASE_CANDIDATE_ACCEPTED 直接进入 Discovery。历史覆盖只供分析和必要 Stable 资格核对，不签发当前候选 PASS；外部作者提交的“已通过”同样不是本系统 Final 结果。

```ts
type CodingRequest = {
  run_id: string;
  attempt_id: string;
  frozen_context_ref: string;
  repair_input_candidate_ref: string; // 本次修复必须从当前可信候选开始，不是Stable别名
  parent_candidate_ref: string | null;
  work_order_ref: string;
  validated_finding_refs: string[];
  workspace_lease_ref: string;
  budget_reservation_ref: string;
  provider_config_ref: string; // 版本、模型配置、认证方式；不放密钥
};

type CodingOutcome = {
  execution_mode: "scripted-fake" | "live";
  provider: "scripted-fake" | "codex-exec";
  outcome: "CANDIDATE_PROPOSED" | "NO_CHANGE_PROPOSED"
    | "SCOPE_CHANGE_REQUEST" | "ERROR" | "CANCELLED" | "UNKNOWN";
  workspace_snapshot_ref: string | null; // NO_CHANGE_PROPOSED 必须为 null；只是提议
  local_feedback_refs: string[];
  event_log_ref: string;
  usage_ref: string; // unknown 不能伪装成 0
  summary: string;
};
```

`NO_CHANGE_PROPOSED` 仅表示本次修复没有提出新内容，不是 Run 的最终 `NO_CHANGE`。Coder 不上传候选快照，`workspace_snapshot_ref=null`，只提供理由和证据引用；可信 Collector 保留并核验 `repair_input_candidate_ref` 对应候选，不能回退稳定版本。如果输入候选相对 Stable 有新增功能，即使本次修复无修改，它仍可能是 CHANGED。缺陷未解决时保留 Finding，不能用“无输出”当成功。`UNKNOWN` 表示模型请求、子进程或费用确认不完整，不等于候选业务结果未知；两种未知保存在不同对象中。

协议校验按 outcome 区分：CANDIDATE_PROPOSED 必须有可核验 workspace_snapshot_ref；NO_CHANGE_PROPOSED 必须为 null。二者都不是可信 change_kind，也不能携带自行签发的业务 PASS；最终分类与候选身份由 Collector 产生。

CodingRequest 的 validated_finding_refs 必须非空，且每项由可信裁决绑定当前 repair input、有效 scope 和 CODE_DEFECT；不能仅凭模型诊断启动。parent_candidate_ref 若填写必须与 repair_input_candidate_ref 一致，不接受另选 Stable 作为工作区起点。修复策略、exploration_closed、预算与租约由服务端重新检查。

为请求和结果准备正常、缺字段、错误 Run、过期 lease、无预算、引用越权的 fixture。此时只用 fake，不接模型。完成标志是 Runtime 能登记一个 Attempt，接受合法提案、拒绝非法提案，且重放同一请求不会重复领预算。

## 检查点 12.2：准备工作区和最小上下文

1. 由可信 supervisor 从 `repair_input_candidate_ref` 的完整冻结内容创建独立工作区，写入 source manifest；任务 comparison base 只作为对照，不能取代修复输入。Incident 的初始修复输入可来自原 fault 版本，Release 修复输入则保留提交候选的新功能/重构。不在冻结的 ML 上游来源副本或历史 Eventuate 参考目录直接工作。
2. 将源码、任务允许的局部测试放入工作区。批准可读的Stable源码、submitted/current候选及真实release diff可用于分析；私有正确V4参考实现、标准修复/seed答案、holdout、oracle、Registry、证据库和发布凭据留在其他权限域。上下文引用只授权本Run所需对象，不能借Git历史取回隐藏答案。
3. 提供可解释任务包：问题现象、已批准新功能与保留不变量、最小反例、允许路径、批准的本地反馈命令、停止条件。明确哪些行为变化是预期需求，不能为消除反例删掉已批准功能。若使用 seed，来源写明 synthetic，但不暴露注入 diff 或标准答案。
4. 保存输入包摘要、CLI/模型配置、prompt 模板版本、工作区 lease 和预算引用。模型上下文的恢复必须核对这些身份。
5. 控制写入范围。路径校验检查绝对路径、`..`、符号链接、硬链接/文件类型、大小、未跟踪文件和忽略文件；不能只信任 `git diff` 的可见列表或模型提交的文件清单。

不要把“禁止修改测试”泛化为所有测试都不能碰。局部开发测试可按 WorkOrder 授权；独立 oracle、隐藏验收、允许路径策略和已批准合同不可由候选修改。构建脚本、依赖锁和数据库迁移改变执行面，首期默认不在 scope，确需修改时生成 `SCOPE_CHANGE_REQUEST` 供工程负责人明确新范围。

## 检查点 12.3：实现 CLI 边界，先解决认证与执行隔离

`workspace-write` 是工具配置的一部分，不能替代操作系统权限、出网规则和凭据隔离。Python build backend、依赖安装钩子、测试、模型反序列化和候选脚本均可执行代码；ONNX/custom operator 加载面也须明确限制。模型 API key 不得作为整个不可信构建 job 的全局环境变量；也不得出现在工作区、日志或可被工具读取的认证目录。

拟采用可信 supervisor/认证代理与无秘密候选执行域分离的部署设计。模型认证由可信域管理；真实构建交给无模型密钥、无发布凭据的独立构建进程。CLI 的实际工具继承环境、认证文件可见性、网络和文件挂载必须通过否定测试验证。仅把 CLI 与 Python 构建/推理写成两个函数，不构成隔离。

如果选定 CLI/部署方式不能证明候选工具无法读取认证材料，live 接入保持阻断，先完成模型代理或受控工具执行通道；不通过放宽 sandbox 或把 key 写入仓库继续。第 15 章将做完整平台 qualification，本章在任何 live 工作区执行前仍须完成最小凭据与权限预检。

实现细节按以下顺序落地：

1. 使用 `spawn(binary, args, options)` 的参数数组，不拼接 shell 字符串。工作目录、二进制路径、配置引用由可信服务解析。
2. 环境变量采用 allowlist；日志只记录变量名与配置摘要，不输出值。每 Run 使用受控临时目录，不能复用包含个人认证和历史任务的目录。
3. stdout JSON 事件与 stderr 分开采集，限制单条大小和总量。未知事件保留受控原始记录并报告解析兼容性，不能将任意模型 JSON 当可信候选或 PASS。
4. 明确区分进程退出码、工具失败、任务声明和文件变化。退出码 0 仍可能无补丁、越界改动或局部测试失败。
5. 取消先阻止新调用，再终止属于本 Attempt 的进程组，并读回真实退出事实。超时后不假称进程已经停止，也不立即启动第二个付费请求。
6. 登记 token/调用次数/墙钟、供应商 usage 和费用估计来源；用量缺失保持未知。费用确认迟到时按第 11 章对账。

以下是未来进程参数设计，仅用于解释 adapter；不是现在可以运行的命令，也没有给出任何密钥注入方式：

```text
拟建参数：codex exec --sandbox workspace-write --json <经过审查的任务文本>
启动前：核对已安装 CLI 版本、帮助、认证隔离和当前任务范围
实际启动：由 CodexExecProvider 使用参数数组，不经 shell 拼接
```

## 检查点 12.4：局部反馈和可信冻结

外部候选接收与模型提案使用同一可信冻结器。外部入口先取得已冻结提交对象；修复入口先由 supervisor 停止所有候选写入。冻结器重新读取完整文件集合，校验批准变化范围、类型和内容摘要，将候选复制到新的只读快照。不能只对外部作者或模型给出的 diff 文本算 hash。

可信组件在独立 Git 对象区严格核验外部提交，或为授权修复生成并核验候选 commit，登记 `candidate_commit_sha`、`candidate_tree_sha`、`base_commit_sha`、真实 Git `parent`/父列表，以及独立的 `parent_candidate_id`、文件清单、源码/资源摘要、依赖和构建配置。Release 可能包含多个提交；comparison base 是批准祖先，不要求直接 parent=base，也不为迎合单提交假设重写外部候选。修复 commit 的实际父节点是修复输入 commit；parent_candidate_id 表达候选谱系，不能代替 Git 父链。核验从 base 到候选的真实父链、对象闭包及授权变化，核对树与封存文件，包括新增、删除、重命名和 mode；完整封存对象包及摘要。不能信任工作区自报 HEAD，不能制造空 commit 或把 base 的真实 parent 改成自身。

上述封存必须在第 14 章 Final 前完成，不等到第 15 或 17 章再生成 commit。首期推荐两种 completion_mode 都使用同一封存器；`DRAFT_PR_CI` 必须具备精确可发布 Git 对象。Runner 从该封存 commit/tree 对应内容构建，后续发布器传输同一对象，不能重新在可变 base 上应用补丁。

冻结前后若文件变化，返回 `CANDIDATE_MUTATED_DURING_FREEZE`，保留现场，不登记可验收候选。冻结存储的写身份与候选用户分离，后续 Runner 从已登记快照构建，不从仍可变工作目录构建。

局部 Python 测试/构建与推理冒烟是开发反馈，记录命令、退出码、日志引用和工具链；失败不删除，重试产生新 physical attempt。独立验证会重新构建并验证当前 Candidate，所以不能将局部测试报告直接填入 Final coverage。若输入范围不允许修改依赖，解析失败转环境问题，不生成“升级整个项目”的补丁。

### 比较基线、修复输入与无修改提议必须分开

CandidateManifest 保留可信派生的 `change_kind=UNCHANGED|CHANGED`、`base_commit_sha/base_input_digest` 和 `candidate_input_digest`。base 语义在任务内固定：RELEASE_VERIFICATION 为稳定版本，INCIDENT_REPAIR 为原故障/输入版本；不会随修复轮次改成上一个 Candidate。CodingAttempt 的 repair_input_candidate_ref 另行冻结。两个 input digest 使用相同规范化 ML release manifest：完整源码/资源与文件 mode、构建脚本和命令配置、依赖锁及实际解析依赖摘要、工具链/构建镜像、相关配置和 commit/tree 身份，另包含外置 ONNX 实际字节摘要、不可变模型身份、labels 文件摘要、输入/输出 schema、预处理代码与 manifest 摘要、影响推理的 feature snapshot 引用。外置工件必须有可复核的不可变对象及来源回执，不能只保存可变URL、alias或版本字符串。排除 run/candidate ID、时间戳等记录元数据。测试环境的实例/reset 身份仍绑定 CheckRun，不冒充输入相等性；评估集、阈值及Oracle版本另入冻结验收闭包，变化必须失效旧检查资格。只有相对任务 base 完整输入相等才是 UNCHANGED；“平台未修复”不等于无发布变化。

完整输入 digest 不同而 Git tree 相同，例如外部配置或解析依赖漂移，既不是 UNCHANGED，也不能靠空 commit/空 PR 变成可发布代码修复。当前任务中未授权、不可重现或无法映射到批准 Git 发布包的输入变化，应阻断候选接收/发布，记录 `BUILD_INPUT_DRIFT_OUT_OF_SCOPE` 或相应完整性原因。可发布 CHANGED 必须有真实、授权的 Git 变更，且构建输入变化受批准包和执行配置约束；纯外部配置变更应另建独立 scope，不暗借 CREATE_DRAFT_PR 的权限。本轮不扩展该发布协议。

Collector 处理 NO_CHANGE_PROPOSED 的顺序：

1. 核对本 Attempt 的 `repair_input_candidate_ref`，从受控对象存储物化该输入的干净完整内容；另读取 WorkOrder 冻结比较 base。不能把两者混用，也不能采用脏 checkout 或可变依赖缓存。
2. 检查 repair input 对象包、实际文件、依赖/配置和工具链引用，核验输入候选摘要。保留原 Candidate 内容与谱系；按需追加可信接收/交接记录，不为空提案制造空 commit 或新代码变化。
3. 继续使用任务 base 与该 Candidate 的完整 input digest 判定 change_kind。如果本来就是 CHANGED，不因本轮没有修复而改为 UNCHANGED；只有完整输入与任务 base 相等，才保留/登记真实 base commit/tree、真实 parent 及对象包的无变化候选。
4. 若 Worker 曾使用可写工作区，仍审计相对 repair input 的实际改动与越界行为；无快照提议不能掩盖已发生的改动。工作区与 repair input 不等、缺依赖摘要或存在完整性问题时拒绝“本次未修改”的认定，保留缺口；相对任务 base 的 change_kind 仍单独计算，不因它是 CHANGED 而拒绝合法原样交回。
5. 将保留的当前 Candidate 交给第 13 章 Discovery；仅满足送验条件后进入第 14 章独立 Final。适用 mandatory 包含批准新功能/兼容要求，且检查任务目标、未决 Finding、相关 UNKNOWN/清理条件；Stable 的旧 PASS 不直接冒充当前 Final。已知缺陷未解决时不可通过无修改提议绕过 readiness。

当合格 Final 完成、任务需求满足且无未知/清理阻断时，Runtime 才能将相对任务 base 的 UNCHANGED 候选终结为 NO_CHANGE；优先于 completion_mode，发布/CI 均为 NOT_APPLICABLE，不创建 Approval/空 PR。外部新功能候选没有经过修复仍可 CHANGED：FINAL_ONLY 则 SUCCEEDED、发布/CI 为 NOT_REQUESTED，DRAFT_PR_CI 则沿审批交付链。回退 Stable 若丢失新需求应被 Final 阻断；已知缺陷未修复同样不能成功。运行 Gate 不读取隐藏 ground truth，第 18 章另外校准 correct_no_change、无修复验收通过和 repair_success。

## 检查点 12.5：验证交接，再做一次受限 live 实验

先完成 fake 的正反例和真实隔离预检。live 实验应明确一个合成任务、允许代码范围、模型/CLI 配置、费用与墙钟上限、输出边界；记录启动授权来源。文档编写授权不等于未来所有模型调用或远端动作授权。

任何真实模型通道启用前还须核对 [94 · 数据流转、区域与保留](94-data-flow-and-retention.md) 的 `MODEL_DATA_EGRESS_READY` 记录：实际 Coding provider/账号/endpoint、发送字段、用途、区域、输入输出及服务端保留能力与当前配置一致。公开固定源码和合成任务可使用仅覆盖该数据等级的记录，不要求提前完成企业部署；企业数据则另需 ENTERPRISE_DATA_READY。提示词、工具参数、输出事件、归档及后续 checkpoint 都进入实际流向清单，密钥和 hidden answers 禁止外发。本项是未来实施准入，不是本文已经取得的批准。

未来命令：本章实现脚本后才可执行。下面的脚本名为拟建接口；执行前还需验证参数和范围。

```sh
pnpm run coding:protocol-check -- --provider scripted-fake
pnpm run coding:isolation-check -- --profile local-isolated
pnpm run coding:run -- --work-order <已批准WorkOrder-ID> --provider codex-exec
```

| 验收场景 | 预期结果与证据 |
|---|---|
| fake 产生范围内补丁 | 有 CandidateManifest，标记 fake；无模型修复率结论。 |
| live 产生合法补丁 | 有真实 provider 事件/用量、冻结候选；业务状态仍待独立验收。 |
| Release 外部新功能候选，修复关闭/CODING=0 | 可信接收后直接Discovery/Final，无Coding调用；Planner仍可按DISCOVERY预算调用获准模型，通过后仍按相对Stable的CHANGED分流。 |
| 模型自报成功但没有快照 | 只形成无修改提案；保留 repair_input_candidate_ref，不回退 Stable 或消除 Finding。 |
| 改 oracle、隐藏文件或越界符号链接 | 冻结拒绝，记录路径策略原因，原证据不改写。 |
| 构建读取密钥/发布 token/控制库 | 权限层拒绝；测试失败阻止 live 或发布资格。 |
| 冻结时后台继续写文件 | 候选登记失败；不产生“已验证同一份代码”的假象。 |
| commit/tree/parent 与封存文件或批准 base 不一致 | 冻结拒绝；发布模式不能先验文件包、发布时再造不同 commit。 |
| 多提交 Release、base 为祖先而非直接 parent | 完整核验真实父链和对象闭包，可合法接收；不得强制改写单提交。 |
| 进程崩溃或请求超时 | Attempt 保留真实状态；按账本对账后决定是否新尝试。 |
| CLI 事件格式变化 | 解析兼容性失败可见，不把缺失 usage 当零成本。 |
| 源码相同但依赖/构建配置变化或缺失 | 完整 input digest 不等或不可核验，不能标 UNCHANGED。 |
| input digest 变化但 Git tree 不变，变化不在批准发布包内 | 阻断接收/发布，不造空 commit/PR；外部配置任务另定 scope。 |
| 相对任务 base 输入相等且当前需求独立通过 | Runtime 可 NO_CHANGE；不造空 commit、Approval 或 PR。 |
| 回退 Stable 删除模型固定、版本回显等批准新功能 | 新需求检查阻断，不能靠旧版本冒烟通过宣布成功。 |
| 已知 seeded defect 原样返回 | Finding/Final 仍阻断，不能把未修改计为修复。 |

拟建证据目录为 `reports/coding/<run_id>/<attempt_id>/`，包含脱敏输入 manifest、事件、局部反馈、路径审核、冻结收据和用量记录；真正的不可变内容由 ArtifactStore 管理，目录只作为人可读导出。

## 排障、回退和完成检查

认证失败先查看错误码与配置引用，不打印 key；协议解析失败固定 CLI 版本和脱敏最小事件样本；编译失败区分源代码错误与依赖解析；无必要修改则保留当前候选继续验证，不退回 Stable；越界请求返回 scope 变更理由。若 live adapter 不稳定，可切回 fake 继续协议开发，但报告必须明确 provider 变化，不能将 fake 结果补到 live 成绩里。

完成检查：

- [ ] Provider 与 Runtime/Candidate Registry 分离，接口已校验。
- [ ] fake/live、版本、配置和实际用量可追溯。
- [ ] 工作区、凭据、隐藏答案和发布权边界通过否定测试。
- [ ] 冻结读取真实文件，能拒绝竞态、越界和伪造成功。
- [ ] 外部 Release 候选直接可信接收/Discovery；task_mode、repair_policy、completion_mode 已冻结。
- [ ] Final 前固定 commit/tree/base/真实父链和可校验对象包，parent_candidate_id 独立记录。
- [ ] NO_CHANGE_PROPOSED 无快照仅提议，保留 repair input；相对任务 base 的完整输入相等且独立验收满足需求才判 NO_CHANGE。
- [ ] 当前候选已交第 13/14 章；没有借局部测试自动放行。
- [ ] 本章证据和仍未解决事项写入 [92 · 进度记录](92-progress-and-session-log.md)。

下一会话可复制：

> 请按第 12 章先实现可信外部候选接收和共同冻结器，验证 Release 无需 CodingAttempt 即可进入 Discovery；再实现 CodingProvider/scripted-fake 的修复交接。核对第 09–11 章证据、任务模式和权限，不复制 DSH/fixture 业务，不调用真实模型、不发布。验证修复必须有有效 Finding，NO_CHANGE_PROPOSED 保留 repair input，多提交父链正确，冻结竞态/越界/恢复均拒绝错误输入；记录实际证据与缺口。之后再推进 CLI adapter，live 前落实认证与工具隔离。

