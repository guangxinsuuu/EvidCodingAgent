# 03.3–03.4 构建预检结果

本次检查：`20261104T020503Z-build-preflight`，2026-11-04。结论：**READY_FOR_BASELINE**，可以进入第05章尝试真实基线；尚未证明订单业务健康或生产资格。

## 实际完成

固定来源为 `894f2dbbe9aba2be6129748b6bbf5f30356deb07`。从干净参考仓库导出190个文件，不含.git、缓存或凭据。原目录保持不变；仅在专属临时副本改了 `gradle.properties` 和 Wrapper properties：移除个人Maven路径、使用容器内合同目录、通过已核验的本地Gradle ZIP引导。业务Java源码未改动，未加入seed。

Docker Desktop已启动，本机Engine 28.4.0、Linux ARM64可用。固定Temurin镜像内实际Java/javac为17.0.21，Gradle为8.14.5；宿主默认Java与容器JDK分开记录。Wrapper JAR匹配官方7.6.2–7.6.6系列摘要，分发ZIP匹配官方8.14.5摘要；没有把二者误写成相同版本。

| 检查 | 结果 | 命令用时 |
|---|---|---|
| Gradle版本 | PASS | 0.878s |
| 配置及首次依赖清单 | PASS | 97.125s |
| E2E任务发现 | PASS | 3.816s |
| 12个Java模块编译 | PASS | 13.595s |
| 三个服务bootJar | PASS | 6.728s |
| 新源码目录离线重编译 | PASS | 6.52s |

编译覆盖Customer、Order、API Gateway三个main服务及其依赖，共12个Java编译任务。bootJar图已先审查，没有执行test/check/contractTest/服务启动或发布。发现 `runApplicationPostgres` 与 `runApplicationMySql` 只证明任务存在，未执行它们。

离线阶段断开容器网络，inspect返回空网络集合。在新源码目录、无既有build输出、`--offline --no-build-cache`条件下，14个任务全部实际执行（含buildSrc的2个任务和12个Java编译任务）。这证明同次依赖缓存足以重新编译，**不等于全新联网环境独立复现**。

首次 `help --write-verification-metadata sha256`尝试解析全部可解析配置，包含部分测试依赖，但没有运行测试。记录1698个缓存文件、862个metadata组件、1430条制品声明；文件包含JAR/POM/module等，不能把这些数量当测试数。后续构建复用清单，不重复bootstrap。依赖摘要是首次观察字节的清单，尚无逐依赖独立真实性批准。

## 隔离与工件交接

宿主可信编排器创建唯一带本次标签的容器/卷；构建进程使用UID501、2CPU、4GiB内存、512进程上限、512MiB临时目录、每次Gradle600秒上限。根文件系统只读、cap-drop ALL、no-new-privileges；无宿主bind mount、Docker socket、控制面数据库或发布凭据。

联网引导使用bridge，仓库域名白名单未强制执行。ZIP的0444模式也不是抗恶意篡改的不可变挂载。因此这里只验证固定已审查源码的预检边界，第15章对抗验证仍未实施。两份容器源码的190个输入文件在构建后均与预期摘要一致。

实际交接链：`container-stage.py → Docker隔离构建 → 三个JAR → 宿主SHA-256复核`。后续“可信provisioner加载固定拓扑”尚未实现。JAR只做构建与交接检查，没有启动。

| 服务 | 大小 | SHA-256 |
|---|---|---|
| customer-service | 109.43 MiB | `bd35cfa8c91cdd005aff53ecde5e037996940a56460ebf67917298833940affc` |
| order-service | 109.53 MiB | `bfb8efa509cdd5181f11c0e27c051fa26e6e314771d7cf5c167a2cc02467243f` |
| api-gateway-service | 62.62 MiB | `3d14f0d55be8e331c59fab0f71c452a81bd71ad11ee225f88ad2d5bd2904a282` |

JAR保存在 `artifacts/build/preflight/20261104T020503Z-build-preflight/`。manifest声明Build-Jdk-Spec 17；顶层应用class主版本为61。没有把顶层class检查推断为所有内嵌依赖字节码合格。

## 遇到的问题与处理

1. 最初Docker daemon不可用，启动本机Docker Desktop后重查通过；原失败记录保留。沙箱对Docker socket/Desktop日志目录的限制通过已批准宿主操作处理。
2. cap-drop ALL下root不能chmod属于构建UID的ZIP。改为由文件实际所有者设权限、root设自身目录权限；保持全部capability禁用。首次失败输出没有覆盖。
3. 在线与离线编译各输出两处 `unknown enum constant AccessType.FIELD` / `jakarta.persistence.AccessType not found` 警告，两次编译退出码均为0。保留警告，在第05章真实启动与依赖验证中继续判断影响，未为消除警告擅改业务/版本。
4. Gradle提示弃用功能与9.0不兼容。本轮使用锁定的8.14.5，没有升级到9。

## 文件与阅读顺序

- `config/toolchain-lock.json`：实际工具链、镜像、Wrapper/ZIP摘要、依赖清单与证据引用；本轮快照也保存在evidence中。
- `config/build-repositories.json`：声明仓库、观测等级、移除的个人路径、未确认合同仓库、网络边界。
- `artifacts/evidence/preflight/20261104T020503Z-build-preflight/report.json`：机器可读结论、历史失败、警告、NOT_RUN与下一步。
- `artifacts/evidence/preflight/20261104T020503Z-build-preflight/container-stage.py`：本次会话编排入口；`prepare/finish-prepare → version/help/tasks → plan/compile/bootjar → offline → collect/export → integrity/cleanup`。它是本次取证助手，不是通用环境管理API；ID拒绝覆盖，不能原样重跑到已完成会话。
- `artifacts/evidence/preflight/20261104T020503Z-build-preflight/record-command.py`：记录argv、时间、退出码、脱敏输出和摘要；`commands.jsonl`是实际命令账本。
- `artifacts/evidence/preflight/20261104T020503Z-build-preflight/source-export.json`、`build.redacted.patch`：源码身份及独立build revision。脱敏补丁用于审阅；执行时应从固定来源重新生成精确覆盖，不能直接把占位符当原文件内容。
- `artifacts/evidence/preflight/20261104T020503Z-build-preflight/verification-metadata.xml`、`resolved-dependencies.json`、`artifact-inspection.json`：依赖字节清单及三个真实JAR的身份。
- `artifacts/evidence/preflight/20261104T020503Z-build-preflight/final-integrity.json`：原参考源码、两份容器输入与53个受保护文件的复核结果。

## 清理与后续

本次容器和卷已按精确名称/标签删除，残留数量均为0；删除了卷内依赖缓存。Docker Desktop保持运行，固定构建镜像、宿主临时源码/Gradle ZIP和三个已导出JAR保留，以便审阅。未操作用户其他容器或控制面服务。

本轮没有修改docs、README、AGENTS、现有控制面或共享包。冻结文档中NOT_STARTED标签因此可能落后，真实进度以本报告与证据为准。没有重新执行未改动TS骨架的测试。

下一切片是 **05.0文件型实验账本 + 05.1正式基线副本**，先实现身份分配、单写者锁、追加事件恢复和epoch校验，再接受控启动/动态端口发现/精确清理。之后才运行足额批准、不足拒绝、未知客户拒绝、恰等额批准四条真实API smoke。数据库、Kafka、CDC、SUT镜像ARM兼容、独立Oracle、恶意候选隔离和企业生产资格均未验证。

本次只是建立可用实验基础。后续稳定对照按来源和环境有效条件复用，发布任务重点验证新候选的变化，不按候选数重复Stable全量测试。通用TypeScript preflight CLI、正式provisioner与实验账本尚未实现。
