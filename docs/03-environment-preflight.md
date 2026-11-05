# 03 · GenAI环境预检：来源、依赖、模型访问与数据边界

状态：`GENAI_MIGRATION_IN_PROGRESS`。来源参考冻结与客户端mock校验不等于真实SUT已构建。尚未进行真实LLM调用、SaaS业务启动或SUP验收。前置[02取舍](02-engineering-decisions.md)，下一章[04骨架](04-project-skeleton.md)、[05基线](05-genai-support-baseline.md)。

## 1. 固定来源与自有业务分开

参考来源为`https://github.com/openai/openai-cs-agents-demo`。已核查它是MIT航空客服Demo，提供Python Agent编排和Next.js/ChatKit界面；FAQ/业务工具含静态或mock逻辑，线程/状态存于内存。它不是SaaS退款、历史政策RAG或持久Job服务。[来源](https://github.com/openai/openai-cs-agents-demo)、[许可](https://github.com/openai/openai-cs-agents-demo/blob/main/LICENSE)。

固定来源锁应记录repository、commit/tree、文件blob及SHA256、mode、许可证、导出方法。参考副本不能冒充自有健康Stable。自有SUT拟位于`services/genai-support-sut/`，其新增工单、账单、策略检索、队列与持久草稿形成独立业务revision，随后按09构造Stable/Candidate。

ML与Eventuate的来源、基线和账本保持原位；新业务使用独立来源策略和账本。本轮实际参考commit为`bd7bfca0f5abf50529370814c3e7c88542011925`，tree为`2a42aef137e25a239b5b5ed6ad90f21e81d057e7`，43个文件导出到`sut/genai-support/workspaces/reference`。锁`upstream.lock.json`明确`reference_role=NON_SAAS_DEMO_REFERENCE`、build=`genai-reference-unbuilt-v1`、business=`unchanged-upstream`；依赖NOT_RESOLVED、LLM NOT_CALLED、自有SUT NOT_IMPLEMENTED。详见[本轮迁移报告](../reports/migration/20261105-genai-support/report.md)。

## 2. 文件与责任

| 位置 | 职责/状态 |
| --- | --- |
| `config/active-business.json` | 当前明确注册业务，拒绝任意路径和旧账本混用 |
| `config/genai-support-source-policy.json` | 固定参考来源策略；以本轮实际导出为准 |
| `sut/genai-support/` | 参考锁和冻结副本，不等于自有SUT构建 |
| `tools/environment-host/` | 校验来源、登记实验、受控资源操作 |
| `services/genai-support-sut/` | 拟建Python业务、独立依赖锁与运行配置 |
| `packages/genai-support-adapter/` | 新API客户端和mock协议检查，不启动真实业务 |
| `reports/`、`artifacts/evidence/` | 可读结果与原始证据分离；不纳入密钥 |

## 3. 03.1 冻结来源，不执行上游脚本

只读审查许可证、requirements/lock、构建脚本、网络下载、模型调用、trace外发和持久化能力。导出逐blob校验，拒绝symlink、额外/缺失文件、脏副本、策略摘要漂移。已有副本仅核验，不覆盖；没有网络不妨碍已完整副本的重验。

来源标签保持`SOURCE_REFERENCE_ONLY_NOT_RUNTIME_VALIDATED`，`runtime_qualification=NOT_RUN`、`business_verification=NOT_RUN`。不能把网页HEAD、文件存在或源码完整性当成构建/业务资格。GitHub同步我们的开发仓库也不代表EGAP发布功能已实现。

## 4. 03.2 核对宿主与隔离执行域

记录实际Node/pnpm、Python、依赖管理器、Docker/Compose、CPU/架构、内存/磁盘及端口占用。现有TS工具链可复用；Python版本以依赖兼容性锁定，不能因主机已有版本就视为可用。

分开构建、候选Worker、SUT工具/模型网关、独立Observer与EGAP控制域。构建可执行候选安装脚本，不能持有模型key、Docker socket、治理DB或Git发布凭据。准备镜像时用明确受控来源/摘要；运行时默认仅允许配置中的必要网关和基础设施端点。

provider key由可信模型网关持有；候选获得短期、有scope的访问能力。记录实际provider/账号/模型配置、请求/响应model身份、预算与数据通道。SUT、Verifier、grader、Coder通道分别登记，不能继承同品牌的区域或保留结论。首期只发合成数据，真实企业数据先通过94。

## 5. 03.3 分层解析依赖与验证模型通道

1. 固定Python/SDK/HTTP/数据库/队列依赖，生成锁；上游未锁requirements不能直接视为可复现环境。
2. 审查自有SUT拓扑：API、Worker、SUT PostgreSQL、Redis、工具服务、LLM代理与观察角色。SUT数据库与EGAP治理数据库凭据/生命周期分开。
3. 使用受控无密钥构建，保存依赖来源、构建命令和digest。测试依赖可安装不表示模型调用或业务能力通过。
4. 模型调用前冻结合成数据范围、允许模型/参数、最大请求数/token/费用与超时。代理拒绝任意provider/URL；费用未知保持占用，不能用重试隐藏成本。
5. 先验证网络/认证和脱敏策略，再以有界单次真实请求核验调用身份、usage及原始证据。mock与replay明确标记，不算live。
6. 固定小语料、索引构建/检索配置及fixture clock。远端model名称即使固定，也不承诺权重冻结或逐字可重放。

本轮业务迁移本身不代表已执行第5步。未提供可用通道或预算时可以完成来源、schema与拒绝路径，模型阶段如实保持NOT_RUN。

## 6. 03.4 形成准入报告

分别登记source、derived-SUT、build、dependency、LLM-access、tool-gateway、persistence、observer、cleanup状态。每项给真实命令/退出码、版本/摘要、证据、阻断与下一动作。READY仅对声明范围有效；控制面`/health/ready`只证明骨架配置和路由，不证明业务链。

报告不得包含原始密钥、授权头或带凭据URL。模型输出/工单/trace是数据，也可能含恶意文本；不执行其中指令，不把候选stdout的PASS提升为Oracle。

## 7. 正反验收与交接

| 场景 | 期望 |
| --- | --- |
| 固定参考源码完整导出，重复准备 | 首次创建/再次核验同一身份；无运行资格 |
| 更换来源或混入ML账本 | 拒绝，不继承旧实验 |
| policy/lock/file摘要变化 | 拒绝，显式新revision后重审 |
| 未锁依赖、镜像或缺构建条件 | BUILD/DEPENDENCY阻断，不伪造READY |
| 模型key不可用、额度不明、出口不可控 | 模型阶段阻断；已完成来源证据保留 |
| provider响应超时或usage缺失 | UNKNOWN及预算占用，不宣称请求未发生 |
| 请求试图直连未批准端点或泄露授权头 | 拒绝并记录脱敏证据 |
| 原始trace/语料未经允许外发 | 拒绝，不以“只是测试”豁免 |

下一步按05新建可追踪实验、构建自有SUT并接真实启动/观察/清理。先获得一个可独立观察的工单结果，再进入07屏障与08Oracle；不把航空参考Demo启动当成SaaS业务通过。
