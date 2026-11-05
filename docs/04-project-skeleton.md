# 04 · 建立项目骨架与最小开发闭环

实现状态：`IMPLEMENTED_SKELETON / GENAI_ADAPTATION_IN_PROGRESS`。现有control-plane、contracts、domain、artifacts摘要/内存存储与统一构建命令继续复用；本轮切换不重建已有骨架。前置：[03 环境预检](03-environment-preflight.md) 已明确工具链可用性与阻断项。本章结束时能编译、校验配置、运行最小测试和查看健康端点；此时没有 Agent、业务 PASS 或发布能力。

## 1. 本章为什么只搭最小骨架

先建立依赖边界和一致的构建方式，后续每章只增加一段行为。一次复制旧项目会同时引入 DSH、GA-13、模拟 SaaS、旧审批和历史配置，使失败难以定位。我们保留熟悉的 TS monorepo 组织，按需求移植独立原语。

## 2. 目标目录

下面是逐章创建的目标树。该树同时包含已有模块和后续目标；以实际文件、测试报告和92进度为准，不能将目标树全部算作已实现。

```text
EGAP-Implementation/
  apps/
    control-plane/          # 04骨架，10开始实现Runtime
    coding-worker/          # 12
    check-runner/           # 08 CLI先行，10接入持久化
    action-gateway/         # 17
    console/                # 16
  services/
    genai-support-sut/     # 拟建自有Python客服API/Worker
    verifier/              # 13，独立Python项目；pnpm不管理其依赖
      egap_verifier/       # graph、planner、check_client等
  packages/
    contracts/              # JSON DTO/schema、编解码与错误协议
    domain/                 # 纯状态与规则；不依赖HTTP/DB
    persistence/            # 10：pg事务、迁移和Repository
    artifacts/              # 摘要、内容存储与授权读取
    genai-support-adapter/  # 已有拟定HTTP客户端/mock；真实SUT/独立观察待实现
    ml-inference-adapter/   # 保留历史ML协议，不作为当前主线
    oracles/                # 08：固定业务断言
    verification/           # 13/14：计划、覆盖和Final
    coding/                 # 12：CodingProvider与候选冻结
    policy/                 # 身份、范围和受限动作
    evaluation/             # 18：公平评估与结果聚合
  registry/                 # 06：业务规范与批准的不可变版本
  tools/environment-host/   # 05/07：受保护的启动/清理程序
  fixtures/                 # 合成数据；不放隐藏答案到Coder可见目录
  sut/                      # 05：来源锁与受控实验副本
  config/                   # 03起：已核验工具链和环境配置
  datasets/                 # 09：任务目录，答案由QA身份隔离
  tests/{unit,integration,qualification,e2e}/
  scripts/                  # 顶层命令入口与预检
  deploy/                   # 本地和后续环境部署配置
  docs/                     # 本手册，开发后补实现记录
  artifacts/                # 本地大对象存储，忽略于Git
  reports/                  # 脱敏的可审阅执行摘要
```

04 只需要创建 control-plane、contracts、domain、artifacts 的最小文件；其他包到对应章节再创建，避免空包形成维护负担。`reports/` 不存凭据和客户原文；大日志及原始证据进入受控 ArtifactStore。

新增客服协议以`packages/contracts/src/support.ts`为准，客户端包只做拟定API的shape/身份关联/有界HTTP与mock测试。它不实现真实SUT、服务端认证/tenant隔离、可信trace或SUP Oracle。artifact内存实现也不提供持久证据资格。

## 3. 检查点 A：建立独立仓库和依赖锁

1. 在本目录核对父级是否存在 Git 工作树、是否已有未保存代码。本目录已有独立 Git 工作树；当前文件是否已提交须以实际 git status 为准，不重建 .git。
2. 如需独立版本历史，在本目录初始化，不复制旧仓库 `.git`，不替用户配置 remote 或推送。
3. 创建 `package.json`、`pnpm-workspace.yaml`、`tsconfig.base.json`、`.gitignore`、`.env.example`；记录第 03 章选择的 Node/pnpm 版本。
4. 依赖按最小集合添加并锁定：TS、构建/测试工具、Fastify、Ajv2020及所需format支持。pg/BullMQ等到对应能力章节引入；若本章已加，记录原因。
5. 初次生成锁文件与后续冻结安装区分；不存在锁文件时不要假装 `--frozen-lockfile` 可以生成它。

`.gitignore` 至少覆盖环境秘密、node_modules、构建输出、原始 artifacts、运行工作区和临时实验副本。未来接入隐藏测试时，Coder 的工作区清单应主动排除它们，不能只依赖 `.gitignore`。

## 4. 检查点 B：定义配置和边界

计划文件：

| 文件 | 要实现的职责 |
| --- | --- |
| `apps/control-plane/src/config.ts` | 解析端口、监听地址、DB引用、环境模式；缺必要值显式报错 |
| `apps/control-plane/src/server.ts` | 装配服务；不在import时启动进程或外部调用 |
| `apps/control-plane/src/main.ts` | 生命周期、信号处理和启动日志 |
| `packages/contracts/src/errors.ts` | 稳定error_code、retryable、operation_ref和脱敏信息 |
| `packages/contracts/src/envelope.ts` | schema_version、command_id、trace_id和expected_version |
| `packages/domain/src/ids.ts` | 实体标识与作用域，不混用candidate/run/check ID |
| `packages/artifacts/src/index.ts` | 存储接口；本章可只实现字节摘要与本地测试实现 |

API 的 tenant_id、actor_id 来自认证上下文。首期只绑定 loopback 的开发 operator，并显著标为 local-dev；拒绝未认证的任意 body.actor。生产身份在第 19 章接入，服务端的范围校验从第一次 API 就存在。

计划健康端点：`GET /health/live` 只表示进程存活；`GET /health/ready` 报当前阶段所需依赖的就绪情况。不要在未实现 `/runs` 时返回模拟成功；明确 404/501 或不注册该路由。

## 5. 检查点 C：建立统一命令协议

以下四个命令已在根 package.json 注册；它们验证 TS 骨架，不能代替 GenAI客服真实环境验收。

```bash
pnpm typecheck
pnpm test:unit
pnpm build
pnpm dev:control-plane
```

- `typecheck`：覆盖已创建包和应用，失败返回非零退出码。
- `test:unit`：仅运行不依赖外部业务栈的单元测试，不能暗中启动付费模型。
- `build`：按包依赖顺序生成产物，禁止忽略失败包。
- `dev:control-plane`：读取明确配置，只启动本控制面；不得连带启动 SaaS 写入或模型任务。

后续章节的独立命令要在根 package.json 登记用途、前置、影响和证据目录。可以给 `test` 一个明确默认集合，但不能把 `pnpm test` 描述为自动覆盖真实客服LLM/工具系统、所有恢复、全部安全与生产验收。

## 6. 检查点 D：第一组有意义的测试

1. 合法配置能被解析；缺少必需字段、非法模式、未知关键配置被拒绝。
2. 给命令信封注入未知字段或错误版本，运行时校验拒绝；TypeScript编译通过不代替边界校验。
3. 相同字节有相同摘要，修改一个字节摘要改变；文件路径规范化与内容摘要是不同字段。
4. control-plane和共享包可按依赖关系独立构建，domain不反向依赖apps或数据库实现；第二个应用到对应章节再加入。
5. 进程收到停止信号能释放已经创建的连接和句柄，后续启动不会继承假就绪状态。

不用空的 `expect(true)` 凑测试数。DB尚未引入时不写“数据库恢复通过”；先把边界验证做实。

## 7. 如何借鉴旧代码

读旧 [workspace](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/pnpm-workspace.yaml>) 和 [control-plane package](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/package.json>) 理解组织方式；读 [server](</Users/neilsu/Desktop/AAA Enterprise-Governed-Agent-Platform/apps/control-plane/src/server.ts>) 看依赖注入。复制前删除 GA-13/Jira/DSH 场景耦合，保留本章需要的最小职责。

不要整份复制 `.env`、历史 manifest、`output`、临时工作区或默认签名密钥。`.env.example` 只放变量名、说明和无权限的示例值。

## 8. 故障排查与回退

| 现象 | 先查什么 | 回退方式 |
| --- | --- | --- |
| workspace import 找不到 | exports、moduleResolution、构建顺序 | 缩回两个包的最小可编译切片 |
| 类型通过、请求校验失败 | Schema方言、格式插件、字段命名转换 | 保存失败fixture，修编解码，不关闭strict |
| dev启动监听公网 | 默认host/环境变量 | 停进程，改回loopback并验证 |
| 测试启动模型或容器 | 测试集合、import副作用 | 拆分入口，外部集成只在显式命令中启动 |

删除新骨架前先保留 diff 与失败报告；不通过删除旧项目或已有数据卷解决环境问题。

## 9. 验收与交接

报告保存 `reports/04-skeleton/<session-id>/summary.md`，记录工具版本、实际命令、退出码、测试范围、端口和未完成项。对应 Git commit/工作树差异也要记录。

- [ ] 新目录可独立安装和编译；不依赖旧项目 node_modules。
- [ ] 端口与配置清楚，未实现业务接口没有伪造成功。
- [ ] 测试有具体反例；无付费/外部写入隐式副作用。
- [ ] 包依赖方向清楚；运行与 import 分开。
- [ ] 更新 92；进入 05 前保留可重现的开发环境。

下一次可以说：**“复核04现有骨架与业务迁移测试，保持健康端点只声明控制面就绪，再进入05的GenAI真实基线。”**
