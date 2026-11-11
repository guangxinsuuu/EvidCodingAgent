# Catalog Consumption & Task Assembly v0.1 实现报告

本次完成“固定目录 → 受控资料读取 → 任务上下文 → 分维缺口报告”的平台集成。操作范围为 `PLAN_ONLY`，不表示客服业务已重新验证，也不表示测试工程 Agent 已发现新问题。冻结的 docs、README、六份 YAML、SUT、商业来源与业务规则均未修改。

## 实际实现与阅读顺序

1. `tools/registry/catalog_cli.py`：本地入口，暴露 `search / describe / read / assemble`。可信本地包装构造读取范围；任务 JSON 不接受批准、模型出口或执行权限字段。可显式导出到一个全新目录，拒绝覆盖已有输出。
2. `tools/registry/catalog_reader.py`：加载指定目录提交中的六份 YAML，复用原校验器并建立角色、来源、要求、能力、场景、阶段、数据流和保留策略索引。查询保留匹配依据，名称命中不证明覆盖；返回副本，调用方不能修改内部目录。
3. `tools/registry/reference_resolver.py`：依据 provenance 的完整 Git commit 读取本地普通 blob，再校验 SHA256、权限、用途和片段范围。不 checkout、不导入来源代码、不读取工作树副本、不访问网络。未登记、无权限、来源缺失、版本不存在、摘要错误分别返回稳定错误码。
4. `tools/registry/task_assembler.py`：装配六块 TaskContext。v0.1 使用明确的终止订阅调查配方，不是通用语义规划 Agent。规则摘录、历史观察、目录元数据与当前候选覆盖分开。
5. `tools/registry/readiness.py`：读取现有七项 gap，附任务影响与已登记维护角色。目录、材料、规则、能力、授权、证据分别报告，不使用一个总 `ready=true`。
6. `tools/registry/validate_catalog.py`：仅增加固定版本来源、目录和历史读取回调。原 CLI 的工作树审查方式及 21 项测试保持；目录消费方式不要求历史文件等于当前工作树。

主调用链：`catalog_cli.main → local_plan_scope → load_catalog → 原校验器 + ReferenceResolver → assemble_test_context → route_catalog_gaps / build_readiness`。仅显式导出时进入 `_export` 写入新的本地任务包。

## 实际装配结果

任务：调查终止订阅账单争议为何在创建 Job 之前失败，并规划需要的验证。

| 项目 | 本次实际值 |
| --- | --- |
| 任务模式 | `KNOWN_ISSUE_INVESTIGATION`，不是未知缺陷发现评测 |
| 目录提交 | `6b1d4815b4178c070f4442edfc2b5606069a3f11` |
| 登记材料提交 | `cb3881c0a9bfd67fec4ac05f31457b3d95e587b3`，逐项核对摘要 |
| 本轮产品候选 | `null / UNBOUND`；未使用 HEAD、目录提交或历史候选补齐 |
| 历史产品候选 | `bdccf89ea431ab6ff678e754a97113b54547e5d4`，仅保留历史范围 |
| 场景 | `terminated_subscription`、`v2_boundary`，各一个 initial 阶段 |
| 相关要求 | R1、R2、R4 来自原场景草案映射；R3、R5 为本任务明确审阅引用，不新增已证明覆盖 |
| 能力 | `check.business`、`native.prepare`、`observe.native`、`runtime.source`、`runtime.sut`、`source.prepare`、`sut.submit` |
| 来源 | 10 项相关来源元数据；没有加载其外部系统或凭据 |
| 正文投影 | 固定规则的相关片段、历史调查报告的观察字段，共两份登记材料 |
| 其他材料 | 17 项仅引用材料，带 commit、SHA256 和 `content_projected=false` |
| 实际执行 | 0 项业务实验、0 个新 Job、0 次模型调用、0 次 Codex 修复 |

活跃续期场景只是相关正常路径对照，租户、账期、覆盖和付款事实也不同，不能仅凭两个场景差异证明订阅状态导致故障。其他十组场景没有进入该任务包，也没有被宣布永远无关。

完整目录校验会读取 66 个登记文件以验证原有出处和历史映射；这与向任务包投影正文是两件事。任务包不复制这 66 份正文。它目前是可检查的本地装配产物，尚无模型传输授权；后续 Graph 还应按节点选择必要部分，不能直接把整个包当 Prompt。

历史调查的原始 SUT 观察与当时的后续原生读回分开保留，报告中的 inference、classification 等归因不变成本轮发现。当前候选覆盖为空、状态 `NOT_RUN`。缺失的历史模型原始请求/响应仍标为缺失，没有重建并冒充实录。

## Readiness 实际状态

| 维度 | 结果 | 含义 |
| --- | --- | --- |
| catalog | `VALID_DRAFT` | 原目录、66 项来源和 13 项历史观察映射校验通过，不是业务批准 |
| materials | `READABLE` | 本任务获准的两份固定资料可读，线上来源未探测 |
| rules | `LOCAL_CALIBRATION_PENDING_REVIEW` | 实验条款可用于计划，尚非公司正式验收标准 |
| capabilities | `REGISTERED_NOT_DISPATCHABLE` | 七项组件已定位，可派发数量为 0 |
| authorization | `LOCAL_PLAN_ONLY` | 允许本地阅读与计划；执行和模型出口均为 false |
| evidence | `HISTORICAL_READING_ONLY` | 历史材料可解释，但不支持当前候选通过 |

受限身份的行为也经过测试：只许元数据时返回 `METADATA_ONLY`，目录状态为 `VALID_DRAFT_METADATA_ONLY`，不绕过权限读全文完成校验；某份材料无权读取时保留 `DENIED`，不以工作树或历史摘要补正文。

缺口沿用固定目录提交中的 `reports/06-source-catalog/20261111-local-draft/gaps.json`：`REAL_OWNER_ASSIGNMENT`、`ENTERPRISE_RULE_AUTHORITY`、`ENTERPRISE_SOURCE_SCOPE`、`RETENTION_AND_RESIDENCY`、`RAW_MODEL_AND_HTTP_ARCHIVE`、`REGISTERED_EXECUTION_PROFILES`、`PROVIDER_API_CONTRACT`。每项保留原 ID、原说明、来源摘要、影响动作和维护角色。真实人员、正式合同和保留策略未确认，不阻断获准的本地查询，也不授予企业读取或发布资格。

两条待实现数据流直接来自 YAML：`flow.proposed_model_invocation_archive`、`flow.proposed_raw_observer`；没有另造目录。候选未绑定单独列为任务条件，不冒用已有 gap ID。

## 测试与真实执行记录

先分别编写三个测试文件并运行：开始均因实现模块不存在而 RED（ModuleNotFoundError，exit 1），随后实现。后续针对字节截断、内部权限、只读身份引用和投影失败等边界也先复现失败，再修正。RED 过程保留在本轮工具执行记录；下列日志保存最终完整运行。

```bash
tools/registry/.venv/bin/python -m unittest discover -s tools/registry -p 'test_*.py' -v
tools/registry/.venv/bin/python -m unittest discover -s tests/catalog -p 'test_*.py' -v
```

实际结果：原 21 项通过（0.531 秒）；新增 49 项通过（22.584 秒），合计 70 项。新增测试分布为目录/CLI 14 项、Resolver/固定版本回调 18 项、装配/就绪度 17 项。

覆盖固定历史读取与工作树变化、原摘要错误、越界路径/链接/用途、元数据不读正文、历史与候选分离、伪造审批拒绝、能力依赖选择、来源失败分类、缺口路由、片段缺失/截断、只读执行边界、真实目录装配和新输出目录拒绝覆盖。本轮没有运行 SUT 测试或业务端到端流程。

本地证据目录：`artifacts/catalog-consumption/20261111T034130Z-v0.1/`。

- `task-input.json`：实际任务输入。
- `consumption-tests.log`、`existing-validator-tests.log`：完整最终测试日志。
- `task-bundle/task-context.json`：六块任务上下文。
- `task-bundle/readiness.json`：独立就绪度。
- `task-bundle/gaps.json`：本任务缺口、待实现流与下一实验条件。
- `verification.json`：执行命令、结果、产物摘要和范围记录。

上述生成文件仅本地保存，受 `artifacts/` 忽略规则保护，不上传为源码；本报告及平台代码/测试进入 Git。新任务包目录权限 0700、文件权限 0600，未覆盖旧实验材料。材料是现有本地合成业务规则和脱敏历史观察，未加入凭据或私有运行配置。

## 调用示例

在项目根执行：

```bash
tools/registry/.venv/bin/python -m tools.registry.catalog_cli search --scenario-id terminated_subscription --kind capability
tools/registry/.venv/bin/python -m tools.registry.catalog_cli describe sut.submit --kind capability
tools/registry/.venv/bin/python -m tools.registry.catalog_cli read business-source-pack/v1/policies/rules.md --start-line 1 --end-line 20
tools/registry/.venv/bin/python -m tools.registry.catalog_cli assemble --task artifacts/catalog-consumption/20261111T034130Z-v0.1/task-input.json
```

不传输出目录时只返回 JSON；若导出，需增加 `--output-directory` 指向一个不存在的新目录。默认目录版本固定为上列完整 commit，可由可信 CLI 显式指定其他完整提交；不会隐式读取 HEAD。当前版本尚未接入真实产品候选清单，非空 `product_candidate_ref` 明确返回 `CANDIDATE_BINDING_UNSUPPORTED`。

## 下一阶段的最小范围

下一步可以基于四个只读接口接最小“计划/调查 Graph”：读取任务 → 找相关资产 → 阅读片段 → 提出待证假设与证据请求 → 输出有界实验计划。Graph 的计划状态与未来操作回执分开保存，本轮没有实现 Graph。

真正执行所选实验前，仍须：登记真实产品候选和环境身份；为选定动作补 Runner/Observer/Rebuild 包装、预算、恢复和清理边界；确认本地实例与对象范围及该动作授权；核对固定 Lago API 的状态/期间语义及实验规则映射；以新 run 收集准入结果与原生错误/期间/指标事实。先做 Job 准入前调查不要求先调用模型；完整模型原始请求/响应采集是后续生成归因的条件。Codex 修复继续由控制层另行调度。
