# 第04章 A/B 实现与验证结果

会话：`20261103T100614Z-04ab`。范围：本地开发控制面骨架、配置与共享边界。结果：**A/B 已实现，当前限定范围验证通过**；未把整个后续平台、Eventuate基线或生产资格标为通过。

## 本次实际完成

- 在新目录初始化独立Git，分支`main`；未创建commit、配置remote或推送。原有手册保持原字节，当前全部文件尚未提交。
- 建立pnpm workspace：根工程、control-plane、contracts、domain、artifacts共5个项目。只引入本期需要的依赖。
- Node `v25.4.0`、pnpm `11.7.0` 精确约束；TypeScript 7.0.2、Fastify 5.12.7、Ajv 8.20.0、ajv-formats 3.0.1、@types/node 25.9.9精确锁定，实际验证后保留pnpm-lock.yaml。
- 独立下载并安装52个包；依赖安装脚本禁用。后续`--frozen-lockfile --offline`安装验证成功。本项目依赖位于自己的node_modules/.pnpm与.pnpm-store，不使用旧项目node_modules。
- 建立strict TypeScript ESM配置与项目引用。配置/协议运行时校验和类型校验分别执行。
- 新增33份源码/配置/锁文件，其中15份TypeScript文件。代码含中文职责、调用关系和关键不变量注释。

## 代码阅读顺序与调用关系

1. `apps/control-plane/src/config.ts::parseConfig`：读取显式EGAP_MODE/EGAP_PORT，默认127.0.0.1，拒绝非回环、非法端口、未知EGAP配置和凭据URL。返回冻结配置；错误只输出字段和固定原因。
2. `apps/control-plane/src/server.ts::createServer`：装配Fastify与健康路由；import/装配都不监听，也不连DB。/runs尚未注册，返回404；任意body.actor不构成授权。
3. `apps/control-plane/src/main.ts::main`：parseConfig → createServer → listen → SIGTERM/SIGINT → close。关闭去重并设置有界超时；监听失败清理服务。
4. `packages/contracts/src/envelope.ts::parseCommandEnvelope`：Ajv2020严格校验schema_version、command_id、trace_id、expected_version；额外字段/类型转换/越界值拒绝。当前是传输协议边界，不签发业务授权。
5. `packages/domain/src/ids.ts`：Run/Candidate/Check的前缀+UUIDv4运行时检查及类型品牌；ID不是访问凭据。
6. `packages/artifacts/src/index.ts`：原始字节SHA-256；MemoryArtifactStore写读均复制字节，读取核对内容摘要。该内存实现只用于测试/本地开发，不是持久化或企业权限系统。

当前启动路径：`main → parseConfig → createServer → listen`。共享包各自有明确边界，尚无业务命令，因此没有为了演示调用关系而把所有包强行串入健康请求。

## 已执行验证

| 验证 | 最终结果 | 范围 |
|---|---|---|
| 初次安装及依赖锁 | 通过 | 首次联网安装成功，精确版本与完整性摘要进入锁文件 |
| 冻结离线安装 | 通过 | 已有锁文件和本项目缓存，不发生锁漂移 |
| `pnpm typecheck` | 通过 | 全部当前源码含生命周期测试 |
| `pnpm build` | 通过 | 共享包→应用项目引用编译，产物在各包dist |
| `pnpm test:unit` | 17通过、0失败 | 配置拒绝、无监听装配、HTTP注入、协议、ID、字节摘要及别名隔离 |
| `pnpm test:integration` | 3通过、0失败 | CLI配置拒绝、import无启动副作用、真实HTTP和信号生命周期 |

真实监听地址为`http://127.0.0.1:63162`：live=200、ready=200、POST /runs=404。SIGTERM退出0后端口可重绑；同端口重启后SIGINT退出0，端口再次释放。测试子进程已退出，本报告不表示后台保留了一个运行服务。

`/health/ready`明确返回`scope=control-plane-skeleton`、`business_verification=not_implemented`和`database_connection=not_implemented`。当前所需外部依赖为空，不表示Docker、DB或Eventuate检查通过。

## 首次失败与修正记录

- 公开registry读取和首次安装在受限网络内失败；安装还显示了缓存元数据中的旧版本提示。保留这两次记录，随后获准访问公开registry完成真实安装，没有降级依赖来掩盖网络失败。
- 首次typecheck发现Fastify错误参数为unknown，以及NodeNext下Ajv Formats默认导入不可调用。修正运行时类型收窄，并根据已安装模块真实导出使用`.default`；没有用any关闭类型检查。之后typecheck、build和所有相关测试通过。
- 首次真实HTTP测试的本地listen被沙箱以EPERM拒绝，另外两项生命周期测试通过。获准进行本机回环测试后，完整3项通过；没有跳过实际HTTP用例来取得成功。

全部14条记录及其首次失败保存在证据目录；最终测试数为17+3，不将失败/重跑合并成额外成功样本。

## 本机启动与验证命令

首次启动时在项目目录复制不含凭据的示例配置；`cp -n`保留已有.env。服务默认只监听127.0.0.1:4317。

```sh
cd /Users/neilsu/Desktop/CodeSuperAgent/EGAP-Implementation
cp -n .env.example .env
pnpm dev:control-plane
```

`dev:control-plane`先构建再启动，当前没有热重载。另一个终端可以请求：

```sh
curl http://127.0.0.1:4317/health/live
curl http://127.0.0.1:4317/health/ready
```

按Ctrl+C关闭。后续复核可执行`pnpm typecheck`、`pnpm build`、`pnpm test:unit`、`pnpm test:integration`。`pnpm test`仅指unit集合。真实监听测试在限制本机端口的沙箱中会失败，应记录环境原因，不静默跳过。

## 状态与剩余工作

- 本轮交付04A/B，并为验证提供了必要命令和测试；没有扩展到持久化Runtime、Agent、Console或发布服务。
- 后续先按04剩余检查点核对命令边界和交接条件，再进入05。03剩余执行域/Wrapper真实性/依赖解析/Gradle构建资格未完成。
- 上轮Docker daemon不可连接的记录仍未解决，本轮未重新启动或检测Docker；不能将HTTP健康200当作该问题已解决。
- 现有工具组合只在当前macOS arm64验证。Node/依赖升级需单独重验，不能自动称为生产版本矩阵。
- docs/README和协作指令共32份摘要保持一致；旧Eventuate的190份跟踪源码摘要保持一致。依照文档许可规则，没有更新91/92；本报告是当前实现事实入口。

## 证据与参考

- [机器结果](result.json)
- [命令账本](../../../artifacts/evidence/04-skeleton/20261103T100614Z-04ab/commands.jsonl)
- [验证及边界核对](../../../artifacts/evidence/04-skeleton/20261103T100614Z-04ab/validation.json)
- [实现文件摘要](../../../artifacts/evidence/04-skeleton/20261103T100614Z-04ab/implementation-manifest.json)
- [真实进程验证输出](../../../artifacts/evidence/04-skeleton/20261103T100614Z-04ab/integration-loopback.output.txt)
- [依赖元数据快照](../../../artifacts/evidence/04-skeleton/20261103T100614Z-04ab/registry-versions.json)

实现时核对了[Fastify官方测试指南](https://fastify.dev/docs/latest/Guides/Testing/)的装配/监听分离与Node测试用法、[TypeScript项目引用](https://www.typescriptlang.org/docs/handbook/project-references)的依赖构建关系、[Ajv JSON Schema](https://ajv.js.org/json-schema.html)的2020-12入口，以及[pnpm 11迁移说明](https://pnpm.io/blog/releases/11.0)的配置位置与安装设置。实际是否可运行以上述本地执行结果为准。
