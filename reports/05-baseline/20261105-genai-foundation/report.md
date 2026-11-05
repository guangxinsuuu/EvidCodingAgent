# GenAI客服基础：运行预检、合成业务数据、可启动服务骨架

日期：2026-11-05。执行范围为用户本轮授权的03运行预检、05.1合成业务基线和自有客服服务骨架。本报告记录实现事实；没有修改 `docs/**` 或项目 README，也没有将DRAFT业务假设升级为批准规则。

本轮完成：独立Python环境和依赖锁、可重复加载的合成业务数据、跨语言共享接口协议、可真实启动和关闭的本机客服API骨架。当前状态 `SKELETON_ONLY`，整体运行资格仍为 `NOT_RUN`。测试通过只证明本轮声明的范围。

## 1. 现在能做什么

- 通过 `pnpm preflight:support` 只读观察宿主、Docker、本地目标镜像、预定端口和模型配置变量是否存在。
- 通过 `pnpm setup:support` 在项目内安装固定版本工具及锁定依赖，不改全局Python。
- 通过 `pnpm fixtures:support` 验证并读取同一版本的合成数据，输出摘要和固定测试时钟。
- 通过 `pnpm dev:support` 启动本机服务：检查认证、输入、工单引用和租户范围；返回准确的能力状态。
- 通过 `pnpm test:support` 验证数据防误改、接口一致性、HTTP拒绝路径、实际进程退出和端口释放。

没有实现持久Job/outbox、Redis relay/Worker、业务工具网关、模型代理、模型调用、草稿生成/保存、SUP Oracle、Verifier或发布门禁。合法提交尚不能取得202；API存活不等于客服业务可用。

## 2. 环境实测与依赖

| 项目 | 本轮实测/处理 |
| --- | --- |
| Python | 3.14.5，`.python-version`固定补丁版本；本轮仅验证macOS arm64 |
| Node / pnpm | 25.4.0 / 11.7.0，沿用现有平台工具链 |
| uv | 初始PATH没有；项目 `tmp/genai-tooling/` 安装0.13.7，未全局安装 |
| FastAPI / Uvicorn | 0.162.0 / 0.54.0，服务HTTP基础 |
| jsonschema | 4.26.0，消费TS导出的协议Schema |
| 测试客户端 | httpx2 2.16.0；初次httpx运行有弃用提示，按已安装Starlette源码接口改为推荐依赖 |
| Docker / Compose | daemon与CLI 28.4.0 / Compose 2.39.4-desktop.1；通过固定本地socket读取 |
| 本地镜像 | 存在postgres:16-alpine、redis:7-alpine；记录的是local image ID，尚未冻结运行镜像/拓扑 |
| PostgreSQL / Redis | 没有在本轮启动、认证或连接；宿主psql/redis-cli不在PATH |
| 模型通道 | 未配置受控网关、模型或预算；实际模型请求0次 |

`pyproject.toml`固定直接依赖，`uv.lock`固定传递依赖及下载摘要。`setup:support`使用 `uv sync --locked`，拒绝无意更新锁；`uv lock --check --offline`通过。当前平台安装22个包，`uv pip check`全部兼容；锁中的跨平台候选包不等于全部安装。

预检本身不验证锁，也不把key存在当成认证成功。宿主和服务虚拟环境的预检结果分别保存；其中uv的PATH检查仍可能显示不可用，因为实际工具安装在项目tmp下。模型SDK、数据库/队列客户端随对应实现再引入，不将它们描述成已验证。

## 3. 合成公司的业务资料

位置：`datasets/genai-support/synthetic-v1/sut/`。共2租户、9张工单、9条订阅、9份账单、8条用量记录、8份标准合同、2个政策版本。CS-1042刻意缺使用和合同资料。

| 场景 | 工单 | 公开校准参考 |
| --- | --- | --- |
| 十月续费、十一月收到工单且缺资料 | Tenant A / CS-1042 revision3 | 十月政策，NEEDS_MORE_EVIDENCE |
| 完整资料且满足条件 | Tenant A / CS-1043 | ELIGIBLE，仍需人工审核 |
| 完整资料但已使用 | Tenant A / CS-1044 | INELIGIBLE |
| 新政策申请窗口恰好7天 | Tenant A / CS-1045 | 边界包含，ELIGIBLE |
| 新政策窗口超过1秒 | Tenant A / CS-1046 | INELIGIBLE |
| 十月最后一秒续费 | Tenant A / CS-1047 | 按旧14天政策 |
| 十一月首秒续费 | Tenant A / CS-1048 | 按新7天政策，样本超过窗口 |
| 申请窗口起点 | Tenant A / CS-1049 | 边界包含 |
| 同工单ID的另一租户 | Tenant B / CS-1043 | 独立账单和用量，不可与Tenant A混用 |

时钟固定为 `2026-11-10T12:00:00Z`，是实验时钟，不是实际系统日期。演示约定明确为DRAFT：UTC、经过的24小时天数、申请窗口首尾包含、政策生效区间左闭右开、续费日期选政策、工单收到时间算申请时间、仅支持无覆盖条款的标准合同。不存在企业Owner批准或真实退款授权。

事实包SHA256：`806ead4ec1e6f8a2d801181dd99ec4134e0c80d0a163268ac222263e439f2152`。

`fixtures.py`校验固定摘要、重复JSON键、严格字段、记录唯一性、同租户引用、账单/订阅关联、时间顺序、用量观察范围与政策区间；检查失败返回固定错误。文件读取和嵌套深度有界，加载后递归只读。Loader不计算退款资格，不能把结构通过当业务PASS。

参考期望位于 `datasets/genai-support/verification/synthetic-v1.json`，服务加载器不读取它。它是公开的校准参考，当前仅做目录/加载路径的逻辑分离；同仓库文件不是隐藏测例，更不代表候选执行的物理隔离已经完成。

## 4. 文件和调用关系

| 文件 | 实际变化及用途 |
| --- | --- |
| `tools/genai-support-preflight.py` | 有界只读探测；固定本地Docker socket、临时空Docker配置；结果脱敏，不启动业务 |
| `tools/support-service.py` | setup/test/start/fixtures统一入口；独立venv；安装和运行环境白名单；启动使用exec直接交接进程身份 |
| `services/genai-support-sut/pyproject.toml`、`.python-version`、`uv.lock` | 固定解释器、工具和依赖，不沿用未锁定的参考Demo环境 |
| `.../config.py` | 显式客服会话配置，仅允许127.0.0.1；token不进入repr和错误信息 |
| `.../fixtures.py` | 读取可信准备的合成业务资料，防误改并按tenant查询 |
| `.../protocol.py` | 不可变SupportRequest与响应校验；消费共享Schema，校验Job/草稿同工单同revision |
| `.../schemas/support-v1.json` | 从现有TS契约导出的快照，不维护第二套手写Schema |
| `tools/export-support-schemas.mjs` | 生成快照；`--check`只读检查漂移；调用前先build确保不是旧dist |
| `.../server.py` | 创建FastAPI应用；认证先于body解析；受限JSON；租户来源固定；未接持久层时准确返回503 |
| `.../main.py` | 配置→fixture→应用→真实socket→Uvicorn；动态端口报告实际绑定值；无模型/DB副作用 |
| `.../tests/test_fixtures.py` | 22项事实包、防误改、时间和租户关联检查 |
| `.../tests/test_protocol.py` | 3项协议测试，其中36组输入由Node/Ajv及Python共同验证；协议不裁决业务 |
| `.../tests/test_server.py` | 8项ASGI拒绝路径、配置和骨架状态验证 |
| `.../tests/test_lifecycle.py` | 1项真实HTTP子进程测试，精确结束本测试PID并检查端口关闭 |
| `tools/test_genai_support_preflight.py`、`tools/test_support_service.py` | 6项预检与2项启动环境能力边界验证 |
| `config/active-business.json` | SUT状态从NOT_IMPLEMENTED变为SKELETON_ONLY，运行资格保持NOT_RUN |
| `tools/environment-host/src/business-profile.ts`及对应测试 | 接受明确骨架状态，继续拒绝虚构READY/PASS及错误类型 |
| `package.json`、`.gitignore` | 新增可执行命令；排除Python环境/缓存，锁与合成输入仍入库 |

阅读入口与调用链：

```text
pnpm dev:support
  → tools/support-service.py main / runtime_environment
  → genai_support.main.main
  → load_config → load_fixture_bundle → create_app
  → 绑定本机socket → Uvicorn
  → HTTP路由 → bearer认证 → parse_support_request → tenant范围fixture查询
  → 503 JOB_BACKEND_NOT_IMPLEMENTED
```

返回503是当前持久层缺失的准确表达：没有Job/outbox事务，不能承诺202受理；没有真实Job查询，不能对合法Job ID宣称404确定不存在。未知工单则可通过已冻结fixture确定，返回404。`/health`返回200仅表示HTTP存活，`/health/ready`返回503并列出未实现能力。

启动包装器不把模型、Git、云凭据、代理或外部PYTHONPATH带入子进程；本轮仍是同用户本机执行，不具备生产容器、文件系统或网络隔离资格。直接手动绕过包装器运行Python也不在此环境白名单保证范围内。

## 5. 验证和证据

| 验证 | 结果 |
| --- | --- |
| `pnpm setup:support` | 通过；真实安装/同步锁定独立环境 |
| `uv lock --check --offline` / `uv pip check` | 通过；22个已安装包兼容 |
| `pnpm test:support` | 42/42：8项工具检查＋34项服务检查；最终无初次弃用告警 |
| Schema快照和跨语言判定 | 通过；36组相同输入，不含业务裁决 |
| `pnpm typecheck` | 通过 |
| `pnpm test` | 100/100，包含既有平台与旧业务适配器回归 |
| `pnpm test:integration` | 3/3，现有控制面HTTP/生命周期 |
| `pnpm egap:lab status` | 通过；原GenAI来源账本sequence3、1实验/2attempt、0环境/0action |
| `git diff --check` | 通过 |

真实客服骨架测试观察到：health200、readiness503、无凭据401、有效工单503、非法revision422、注入tenant422、合法格式Job查询503。进程接收SIGTERM退出，监听端口关闭；没有留下后台客服服务。

本轮是开发进程验收，没有登记为05.3实验环境，也没有挪用原来源实验授权创建GenAI运行资格。file-lab中的0环境/0action与本轮临时HTTP测试是不同范围。

原始证据位于忽略目录 `artifacts/evidence/genai-foundation/20261105/`：初始宿主预检、服务venv预检、setup、lock-check、dependency-check、fixtures、初次/最终测试输出、初次/最终lifecycle JSON、TS回归、控制面集成、typecheck和lab-status。

一次额外 `uv pip check` 因未指定缓存目录而尝试写沙盒外的默认cache，命令失败；保留原输出，改用项目 `tmp/uv-cache` 后通过。没有通过忽略失败或修改测试预期获得成功。

## 6. 本地复现与下一步

在项目根、已安装指定Node/pnpm和Python3.14.5的环境中执行：

```sh
pnpm setup:support
pnpm test:support
pnpm fixtures:support
```

手动启动需要显式本地会话，命令生成一次性随机token，不是模型供应商密钥：

```sh
EGAP_SUPPORT_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
EGAP_SUPPORT_TENANT_ID=tenant-a pnpm dev:support
```

默认监听127.0.0.1:4321，可浏览 `/health` 和 `/health/ready`；Ctrl-C停止。`EGAP_SUPPORT_PORT=0`可让系统分配端口，启动事件给出实际值。启动事件表示socket已绑定，仍需HTTP探测确认服务可响应。

下一步按05.2/05.3建立实际实验环境：冻结PostgreSQL/Redis镜像与配置、登记资源身份和端口；实现Job＋dispatch outbox同事务后返回202、按tenant查询Job、relay投递与恢复、精确清理。再接入Worker、受控业务工具/模型通道及草稿＋DONE同事务，才进入05.4真实业务smoke。第06–08的批准规则、可信观察及Oracle校准另行推进。

参考实现使用[FastAPI的手动服务启动方式](https://fastapi.tiangolo.com/deployment/manually/)；依赖同步使用[uv的锁与同步机制](https://docs.astral.sh/uv/concepts/projects/sync/)。本轮没有运行参考航空Demo，也未修改其冻结来源或旧ML/Eventuate历史证据。
