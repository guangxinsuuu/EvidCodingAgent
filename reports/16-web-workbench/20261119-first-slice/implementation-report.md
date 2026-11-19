# 首版 Web 审查工作台实现报告

日期：2026-11-19。实现基线：066ceaa。范围：本地实验身份、历史真实材料审查，不包含自主 Agent 派发、企业 SSO、GitHub Checks 或发布。

## 交付结果

新增 `apps/review-workbench`，提供中文「我的任务 → 审查详情 → 证据详情」三页，以及审查决定、补证请求和审计时间线。浏览器与 CLI 调用同一套 API，身份、租户、材料版本和幂等判断均在服务端执行。

本机入口为 `http://127.0.0.1:4174`。业务、数据来源和 QA 三种身份分别使用随机登录码；这些是本地协作实验身份，不是真实公司员工或已接入的企业身份。登录码与数据库配置仅存于忽略的 `artifacts/review-workbench/` 私密文件，未进入仓库。

首版使用现有锁定版本的 Fastify、pg 和 TypeScript，前端使用同源 HTML/CSS/JavaScript，没有新增前端依赖或外部 CDN。后端使用专用 PostgreSQL 数据库与 `egap_review` schema，数据库角色不是管理员。现有 Verify Runtime 数据和客服系统保持原状。

## 实际导入的材料

- 固定来源：`reports/05-baseline/20261119-late-usage-execution/{summary.json,checks.json}`，验证原始字节 SHA-256 后再进行字段白名单提取。
- 候选：`45a0378585b8970fb2860897c9e49792e15a9df0`；审查对象：`review-egaplate20261119a-v1`。
- 六份材料：实验摘要、迟到用量前后观察、申诉后用量前后观察、原始草稿语义待审探针、最终交付检查摘录、限定范围确定性检查摘录。
- 保留原报告 183 条业务断言、固定计划执行方式、缺失项及范围限制；这些是历史报告事实，本轮没有重新执行这些业务断言。
- 明确缺少 baseline/diff/PR、完整模型请求/输出及最终备注全文；未把 151 个历史私有引用说成本轮重新复核的证据。
- 三项义务分别关注业务适用口径、来源完整性、草稿与最终交付语义。初始化均为待审，没有导入或生成公司的批准。

## 文件与调用关系

| 文件 | 职责 |
| --- | --- |
| `src/setup.ts`、`src/config.ts` | 一次性生成高熵身份码、私密配置及恢复中断初始化；限制本地连接，拒绝 pg query.host 覆盖 |
| `src/main.ts` | 配置 → schema → 幂等历史导入 → 回环监听；处理停止信号 |
| `src/auth.ts` | 服务端会话、期限、登录限速、登出与 CSRF；重启后重新登录 |
| `src/server.ts` | 同源静态资源、Host/Origin 校验、认证钩子和安全响应头 |
| `src/types.ts` | 身份、证据、义务、决定及补证命令的类型契约 |
| `src/routes.ts` | 只读投影和显式命令入口；不接收客户端提供的 actor/tenant |
| `src/store.ts` | PostgreSQL ACL、冻结 Bundle、材料一致性、行锁、幂等回执、revision 与事件事务 |
| `src/importer.ts` | 固定来源适配器，校验来源字节、路径、大小和符号链接，再提取安全摘录 |
| `src/cli.ts` | 登录 → 同一 HTTP API → 登出；不直接访问数据库 |
| `public/index.html`、`styles.css`、`app.js` | 登录与三页交互、证据纯文本展示、表单和审计记录、冲突及未知回执恢复 |
| `src/*.test.ts` | 身份边界、真实 PG、来源校验、初始化恢复及三角色完整 HTTP 流程 |

写入调用链为 `app.js / cli.ts → server.ts 会话与 CSRF → routes.ts → store.ts.command → PostgreSQL 事务`。查询链只读取审查投影，不调用具有恢复/派发副作用的 `Runtime.projection()`。审查专用账本尚未接到 Runtime 的实验事件消费链；不会把历史结果导入伪装为 Agent 自主执行。

## 已实施的约束

1. 命令绑定 `candidate_sha + bundle_digest + expected_revision + idempotency_key`，以及服务端确定的身份。跨租户对象不可见，人员只能更新自己角色的义务。
2. 一把 case 行锁内完成幂等检查、版本检查、证据检查、决定写入、revision 更新和事件登记。相同命令重放得到原回执；同 key 更改内容或身份被拒绝。
3. 父 Bundle 摘要、独立证据行与 Bundle 内登记材料保持一致。内容与哈希同时改变、来源元数据变化等情况仍拒绝展示或引用。
4. GET 使用只读一致性快照，不运行模型、不推进实验。所有展示材料通过文本节点输出，CSP 阻止内联脚本和外部连接。
5. 补证请求为 `WAITING_CAPABILITY`。未结请求保留义务阻塞；人员确认不会消除机器缺证或产生 PASS。`ACKNOWLEDGED` 只表示材料核对，不表示发布批准。
6. 浏览器在发送前把未决命令写入按租户/身份/case 隔离的 sessionStorage；不保存访问码、cookie 或 CSRF。刷新后可重试原 payload 和原 key，存储失败则阻止发送。明确退出、换身份清理相应日志。
7. 当前会话在进程内，重启后须重新登录；决定和事件在 PostgreSQL 中。浏览器存储不是授权来源，所有恢复请求重新走服务端检查。

## 验证结果与边界

| 验证 | 结果 |
| --- | --- |
| TypeScript build / typecheck | 通过 |
| 前端 JavaScript 语法检查 | 通过 |
| 工作台专项测试 | 19 项通过，0 失败，0 跳过；其中 10 项使用真实 PostgreSQL |
| 既有项目 `test:unit` 回归 | 270 项通过，27 项既有可选 PG 测试未设置其专属数据库变量而跳过，0 失败 |
| 三角色完整 HTTP + PG | 读取实际历史报告摘录、两份限定确认、一份补证请求、旧版本拒绝、服务重建和原 key 重放均通过；全部在隔离测试库 |
| 最终运行进程的真实网络查询 | 三身份分别登录、读取任务和六份证据、退出；CLI 同一接口查询成功 |
| 浏览器视觉与点击验收 | 未完成：CUA 标签连接报 `timeoutMs` 异常，原生窗口截图为空灰色，不能据此宣称浏览器完整交互通过 |

曾发生的验证问题：受限沙箱不允许既有 HTTP mock 监听端口，改用已批准的本机权限后相同回归通过；并行回归期间两项 PG 用例出现 3 秒连接超时，之后串行运行完整专项集 19/19 通过，没有通过放宽断言掩盖失败。

主工作台导入对象保持 revision 1、待审、0 个人决定、0 补证请求；验收没有代用户做正式业务判断。原始验收日志位于忽略的 `artifacts/review-workbench/validation/`。

## 本机使用与复现

已配置的本机直接在仓库根目录运行：

```sh
pnpm review:start
```

打开本地入口，从 `artifacts/review-workbench/login-codes.private.json` 读取相应身份的访问码。单独的 `business.code`、`source.code`、`qa.code` 可用于 CLI：

```sh
pnpm review:cli --code-file artifacts/review-workbench/qa.code tasks
```

全新环境需先准备 PostgreSQL 和独立非管理员数据库角色，将连接串写入仅本机用户可读的文件，再执行：

```sh
pnpm review:setup --database-url-file PRIVATE_DATABASE_URL_FILE
pnpm review:start
```

默认端口 4174；setup 可在文件参数后传另一个端口。已完成配置不会被重新 setup 静默覆盖；数据库使用动态发布端口时，容器重启后应重新确认端口并由操作者更新本机私密配置。不要把连接串或登录码放入仓库或聊天。

`pnpm test:review` 可执行专项集；真实数据库用例需要 `EGAP_REVIEW_TEST_DATABASE_URL` 指向隔离测试库。没有该变量时会明确跳过，不能据此宣称 PG 验收通过。

## 尚未接通

企业 OIDC/SSO、真实人员分配与撤权、补证请求到 Agent/Runtime 的执行桥、新材料包推进与待办关闭协议、GitHub PR/Checks、Codex 修复和受保护 Final 均未在本轮实现。当前只能使用固定历史导入适配器，不是通用任意报告上传入口。

本轮没有修改 `docs/**` 或项目 README，也未更改被测客服业务、启动客服实验或调用模型。后续优先完成浏览器实际验收，再把一项已授权的补证请求接入真实 Agent 执行和新材料回流。
