# 独立 QA 发布与已归档阶段证据复核

日期：2026-11-26。用户授权先使用虚拟企业来源，本轮继续完善独立 QA 边界。没有修改 docs 或 README，没有改产品业务代码，也没有再次调用模型来替换已有输出。

## 1. 已确认结果

阶段检查已形成独立 QA 仓库版本，通过独立发布者发布，并在无网络、只读、非 root 容器中运行。EGAP 读取固定发布原件及其准确 Git 身份，向检查器提交已有实验的原始观察，不把以前的 PASS 当成新输入。

| 验证 | 结果 |
| --- | --- |
| 已归档实验 | `evidphase20261126a1`，两组已知阶段案例，原四个 Job |
| 独立检查 | 193 项限定确定性检查通过；六份阶段 verdict 与原结果逐项一致 |
| 原件完整性 | 重新验证 112 份操作账本证据摘要；观察和重试/重评回执与 COMPLETED 操作原结果相等 |
| 文案缺项 | 原 3 项 `RAW_DRAFT_LAB_SCOPE_NOT_EXPLICIT` 提示全部保留，仍为 REVIEW_REQUIRED |
| 实际来源权限 | 21 项 HTTP 边界检查符合预期 |
| 容器负向验证 | 2 项，损坏 wire 原件返回 INCOMPLETE；错误申请时点关系返回 FAIL |
| 自动测试 | 40 项通过：独立 QA 22、平台边界 12、来源入口 6 |
| 业务和模型成本 | 本轮 0 次模型调用、0 次产品/计费/客服业务写入；有 QA 发布及临时 ACL 测试操作 |

结果范围是已知候选、已归档观察的独立 QA 重审。没有新的业务实验、未知任务成功率、当前部署新鲜性证明、完整语义 PASS 或 Final 资格。两次正常复核不是两个新实验，也不能重复计算覆盖。

## 2. 固定身份

- QA 仓库：`https://github.com/guangxinsuuu/EvidAcceptanceAssets.git`。
- QA commit：`be8b133d665ac1d7be93a8dec2b7eb203657aca6`，已推送 main。
- QA 契约：`qa.commercial-phase-review.v1`，发布版本 `phase-v1`。
- QA 维护/发布身份：`lab:qa-publisher`，属于本机模拟 QA 身份，`SYNTHETIC_LOCAL_CALIBRATION / DRAFT`。
- QA 来源实例：`evid-sources-906d8c1857bb`，来源服务 commit 为 `3fee03abaa3df1c9c8da90bb187e64879e6b4eec`。
- QA 来源镜像：`sha256:50f443748665c64e3487cbbe8063e5b94bebbca6a0f4b4dca6d7456a01e7a1a2`。
- QA 检查镜像（最后一次正常重审）：`sha256:ab8e3901a878c94379f5d1857c5d84607256205f6ec32b09bd2e79c716dc24fd`。
- 最后一次正常重审：`7ef41be778734fb394824a10162907ca`。
- 原执行索引固定于 EGAP commit `cbe92a225c3a53f9ca9fc7772aa733fca0b7cda1`。
- 原候选仍是 SUT commit `76076dd17368a8cf5627e45758db25300b2a2bd2`，image `sha256:fa5ac89e8c77bfa8356d01b2102ee266ce8326c7a063d2504ed57562209830cb`。

QA 来源独立于已有 `evid-sources-6b1f4995a5dc`。原 Jira `EGAPLAB-100` 与 `refund-rules` 已实际重新读取，摘要保持不变。默认来源指针仍指向原 Jira/规则实例；专用 QA 实例要求显式指定。重复发布复用原五个 publication_id，没有覆盖旧版本或增加新版本。

## 3. 文件与调用关系

独立 QA 仓库新增：

| 文件 | 职责 |
| --- | --- |
| `oracles/phase_oracle.py` | 从 EGAP 固定 commit 抽取原阶段纯函数；保留原业务断言与词汇探针语义，不导入候选产品代码 |
| `oracles/phase_entry.py` | `main → evaluate → validate_package → assess_observation / assess_transition → summarize_execution`。校验原件及阶段前提后重新计算结果 |
| `oracles/phase-contract.v1.json` | 登记输入顶层结构、必需读操作、阶段前提、输出状态、限定范围与失败语义；跨字段约束由固定 Python validator 强制执行 |
| `oracles/Dockerfile.phase` | 固定 Python 基础镜像，复制五个 QA 文件，以 `10001:10001` 执行唯一 JSON 入口 |
| `phase-publication.v1.json` | 登记每个文件的摘要及来源；区分原样保留、纯函数抽取和新写的包装代码，保留原发布 manifest |
| `tests/phase_fixture.py`、`tests/test_phase_entry.py` | 保留已知合成夹具谱系，验证证据缺项、错误绑定和真正的业务关系错误；不把夹具当未见评测 |

EGAP 新增/修改：

| 文件/函数 | 职责与边界 |
| --- | --- |
| `config/phase-acceptance-publications.v1.json` | 独立五对象目录；只有契约允许 Planner 读取，其余 QA 执行代码只允许 Oracle 读取；Product 无权读取 |
| `tools/source-platform/host.py registration_path / instance_registration` | 每个来源实例固定自己的目录路径和摘要；兼容原实例的旧 manifest，禁止越界与软链接目录 |
| `host.py up / publish` | 专用目录不会替换原默认入口；使用新 QA lineage manifest；发布状态按 namespace/object/version 区分，未知发布不换身份重发 |
| `host.py verify_phase` | 实际检查 QA/Planner/Product/跨租户边界、版本不可覆盖、ACL 不可用及准确对象撤权；临时更改在 finally 恢复 |
| `tools/platform-boundary/phase_qa.py load_archive` | 用已提交的索引读取私有归档，核对候选、配置、seed、112 份原件摘要及相应操作原结果 |
| `phase_qa.py fetch_bundle` | 每轮真实 HTTP 读取固定版本，验证 ACL、publisher、profile、审批范围、Git commit/blob/内容与 manifest；拒绝时没有离线缓存兜底 |
| `phase_qa.py build_checker / run_checker` | 仅将验证后的 QA 公开代码放入构建上下文；业务输入走 stdin；保存镜像、容器限制、工具摘要、原输入/输出及失败记录 |
| `test_phase_qa.py`、`test_host.py` | 覆盖源码来源错配、原件篡改、ACL 拒绝、实例目录漂移及“exit=0 但协议无效”的失败落账 |

本轮是独立 QA 复核入口。既有 `phase_runtime.py` 仍保留原阶段检查，尚未自动改用这个容器；本轮没有把该入口登记成 Agent 的 MCP 执行能力。不能将新增契约文件描述成主图准入已完成。

## 4. 输入检查具体保证什么

每个案例提交 `case / before / after / old_after / retry / reevaluation`，不接受调用方给出的 verdict。两组必须恰好为 `late_usage` 与 `post_claim_usage`。

每份观察检查实验 ID、候选 commit、完整部署绑定、六个业务对象范围、请求 Job、完成状态、上下文摘要、九类必要读操作及实际模型原件。原件检查包括请求/响应 SHA、解析后的请求/输出、decision、操作 ID、部署和前后模型权重。不能仅靠 `binding_checks=true` 跳过原件一致性检查。

初态必须观察到 OPEN，后态必须观察到 SEALED；旧 J1 的原快照须再次读取。普通重试仍为 J1、显式重评生成 J2、原申请身份/时间不变等由独立阶段规则核验。缺证据与业务断言失败分别返回 INCOMPLETE 和 FAIL。

这些检查证明归档内部关联及其与已提交指纹的一致性。Git 索引不等于第三方公证，模型自述和外部文本也没有因此取得公司业务批准权。

## 5. 实际隔离与失败记录

检查容器实测使用 `network=none`、只读根目录、无宿主机卷挂载、无 Docker socket、用户 `10001:10001`、capabilities 全移除及 no-new-privileges。限制为 256 MiB、1 CPU、32 PIDs、30 秒宿主等待上限。私有业务输入通过 stdin，输出保存在 0700 目录下的 0600 文件；不在 Docker 日志中保存原件。

第一次容器运行 `cbc9aa9aaa804575b842e1bcb5cefc75` 因 QA 代码在构建上下文为 0600 而被非 root 用户拒读。实际退出码为 1，失败原件和事后对账记录均保留。修正仅将公开 QA 源码改为 0644，父目录仍 0700，业务原件/凭据没有放宽。失败容器已按准确身份移除。

之后正常复核得到 193 项结果。负向容器测试分别为：

| 派生测试 | 实际结果 |
| --- | --- |
| `3ef8c63ac53d4f009311c4ca6a8ef3e3`：只修改 wire response 字节，保留原摘要 | INCOMPLETE / MODEL_WIRE_DIGEST_MISMATCH |
| `8baed65a649a47fb91a414315b488126`：把对照组事件的发生时刻移到申请前 | FAIL；命中独立来源内容不一致和申请时点关系错误 |

负向测试使用内存副本，不修改归档原件，不代表产品又发生了两个缺陷。检查器运行仍由本机受信操作员驱动，QA 的逻辑身份分离和无网络容器不等于已实现企业 IAM 管理隔离或生产远端证明。

负向测试使用前一次正常构建的镜像 `sha256:3a6e7722879e55a52d860ddc28f61824eff21c4b63602f697c401eeee19855e0`。两次构建的 QA commit 和五个代码文件摘要一致，各自保存实际镜像 ID；这里没有声称镜像构建达到字节级可重复。

## 6. 下一步边界

1. 把 QA 契约进一步接到 Runtime/Graph 的正式能力准入：阶段输入如何由已完成操作投影、哪类观察可以复用、能力版本和预算怎样冻结。主图不能只看到一个泛化的 CHECK 字段。
2. 用准入后的固定能力连接 Agent 提案与真实操作；当前仍不能声称 Agent 已自主选择这条实验。
3. 继续 Provider Contract 与 Project 1 的版本化交接入口。上游诊断应携带准确候选、证据指纹、假设/未决项、修复范围和验收依据，不得自带发布批准。
4. 文案语义三项复核提示仍未关闭；没有通过重新生成文案抹除原结果。

本轮交付代码、执行原件和报告；六步整体目标仍有后续工作。公开索引见同目录 `evidence-index.json`，私有业务原件、凭据与模型内容没有上传。
