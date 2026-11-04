# 第一次环境预检结果

检查编号：`20261103T055720Z-source-tools`。汇总时间：2026-11-03 17:03 AEDT。

本轮已完成03.1来源核对、03.2工具检查和证据报告。结果为“本轮检查完成，真实基线准入仍阻断”；第03章整体及第05章尚未完成。可以开始第04章最小骨架与依赖组合验证，不能报告Eventuate已经能构建或运行。

## 1. 来源与构建配置

- 固定commit：`894f2dbbe9aba2be6129748b6bbf5f30356deb07`，精确匹配计划；Git tree为`7d32b3be90ed2ad64947b6d145f2ec251a7d5a59`。
- 190个已跟踪文件均登记Git模式、blob、SHA-256，汇总时再次核对一致；检查前、中、后三次工作树状态为空。没有非忽略未跟踪文件。
- 检测到20个被忽略的缓存/构建目录，包括`.gradle/`、`buildSrc/build/`及各服务`build/`。这些历史痕迹不证明本次构建或业务测试成功；本轮未使用它们执行构建。
- 源码要求Java toolchain 17，Gradle wrapper声明8.14.5。分发地址为`downloads.gradle.org`，已声明SHA-256校验值。
- Wrapper JAR实测SHA-256：`14dfa961b6704bb3decdea06502781edaa796a82e6da41cd2e1962b14fbe21a3`。仅完成本地摘要；官方真实性、实际分发包校验均未执行。
- 源码声明Spring Boot 4.1.1、Spring Cloud 2025.1.3、Contract 5.0.3、Eventuate Platform 2026.1.RELEASE。这些是配置事实，尚未证明依赖可解析或版本兼容。
- 仓库包含个人`file://<DEVELOPER_HOME>/.m2/testdeploy`和Eventuate snapshot地址。后续需要在预检副本中显式处理、留下构建修订；不修改旧目录。配置snapshot仓库不等于已解析到SNAPSHOT依赖。
- `settings.gradle`显式包含13个模块；`migration-tests`目录存在但未include，不能算入默认根构建覆盖。
- `build/check`包含integration、component、contract、E2E等任务；`runApplicationPostgres`进一步涉及数据库、Kafka、CDC和服务容器。它们不能作为无副作用的工具版本检查。

## 2. 本机工具实测

| 项目 | 实测 | 结论 |
|---|---|---|
| 宿主 | macOS 15.7.9，arm64 | 主机架构已确认；SUT镜像架构仍待验证 |
| Node | v25.4.0，darwin/arm64 | 可执行；项目依赖兼容尚未验证 |
| pnpm | 11.7.0 | 可执行；本次禁止Corepack网络下载 |
| Git | 2.54.0 | 可执行，来源只读探测完成 |
| 默认Java / JAVA_HOME | OpenJDK 21.0.10 | 默认指向21；没有修改全局环境 |
| Java 17 | OpenJDK 17.0.18 | 实际执行`java -version`成功 |
| Java 17编译器 | javac 17.0.18 | 实际执行`javac -version`成功 |
| Docker CLI | 28.4.0 | 客户端可执行 |
| Docker Compose | 2.39.4-desktop.1 | 插件可执行 |
| Docker context | desktop-linux，本地Unix socket | 无DOCKER环境覆盖；结束复查仍相同 |
| Docker daemon | 两次探测均未返回Server | 当前连接不可用，容器基线阻断 |
| Gradle | 源码wrapper声明8.14.5 | 本轮未执行，运行可用性未验证 |

Java 17已经存在，不需要因为默认Java是21就先安装或全局切换。Gradle启动JVM与编译toolchain可分别选择；该仓库在当前组合下实际如何解析/构建仍待03.4验证。

## 3. Docker失败如何解释

第一次在受限会话中访问本地socket，退出码1，原因是`operation not permitted`。这只能证明本次会话无法访问，不能据此说daemon停止。

随后经执行环境审批，对已确认的本地socket仅重做一次只读`/version`查询，退出码仍为1，结果为`Cannot connect to the Docker daemon`且`Server=null`。因此结论是当前端点连接不可用；具体是Docker Desktop/Engine未运行、socket失效或其他本机服务问题，尚未进一步诊断。本轮没有启动Docker Desktop，没有启动容器，没有修改持久context；复查仍为`desktop-linux`。

两次尝试分别保留，没有覆盖第一次失败。外部TCP/SSH Docker端点未被访问。

## 4. 尚未执行与下一步

- 03.3：执行域和候选隔离尚未建立。
- 03.4：未导出预检副本，未执行wrapper、help/tasks、依赖解析或编译；未产生有效toolchain lock或批准仓库配置。
- 05及以后：未启动业务、数据库或Kafka，未检查订单/额度规则，未调用模型或进行外部发布。
- 04：可开始最小项目骨架，先选定并验证Node/pnpm与所需依赖组合，完成配置拒绝路径、编译和健康端点。当前只有工具可执行性证据，不是04已通过。
- 进入05前需要解决Docker服务连接，并完成03.3/03.4的限定构建预检。不要直接运行上游全量`build/check`来代替这些步骤。

## 5. 证据与只读边界

本轮记录28条探测命令及其参数、工作目录、起止时间、退出码、输出引用和SHA-256；其中2条Docker尝试非零，其余26条退出0。报告检查项为15项PASS、1项FAIL、8项NOT_RUN；这是本轮清单口径，不能当业务覆盖率。

- [机器报告](../../../artifacts/evidence/preflight/20261103T055720Z-source-tools/report.json)
- [命令账本](../../../artifacts/evidence/preflight/20261103T055720Z-source-tools/commands.jsonl)
- [源码摘要清单](../../../artifacts/evidence/preflight/20261103T055720Z-source-tools/source-manifest.json)
- [工具清单](../../../artifacts/evidence/preflight/20261103T055720Z-source-tools/tool-inventory.json)（保留首轮观测，Docker补充探测见机器报告）
- [静态构建审查](../../../artifacts/evidence/preflight/20261103T055720Z-source-tools/static-source-review.json)
- [证据一致性校验](../../../artifacts/evidence/preflight/20261103T055720Z-source-tools/validation.json)
- [本次采集脚本](../../../artifacts/evidence/preflight/20261103T055720Z-source-tools/collect-preflight.py)

采集调用关系：一次性`collect-preflight.py::main` → `run_probe`命令取证 → `source_inventory`文件摘要；确认本地endpoint后才探测daemon。额外`probe-docker-host-access.py`只处理第二次本地只读查询。它们是本次取证附件，不冒充04章后续正式TypeScript预检工具。

190份源码和32份受保护手册/指令文件摘要重新核对一致，命令输出摘要全部匹配。没有修改docs/README或旧参考源码，没有安装依赖、运行Gradle、启动服务、拉取镜像或执行模型任务。后续实施状态应结合本报告读取，冻结的92日志未自动改写。
