# 03 · ML 环境预检：来源、依赖和模型工件分别核验

状态：`ML_SOURCE_FROZEN / RUNTIME_PREFLIGHT_NOT_RUN`。TS 工具链、控制面骨架及旧 Eventuate 构建已有历史证据；新ML来源27文件已冻结并重复核验，API适配器已完成mock HTTP测试。**旧 Java 编译、镜像下载或单元测试不证明 ML 栈能运行。** 当前实际命令与结果以 [92](92-progress-and-session-log.md)、`reports/`、`artifacts/` 为准。

前置：[02 工程取舍](02-engineering-decisions.md)；后续：[04 已有骨架](04-project-skeleton.md)、[05 ML 基线](05-async-ml-baseline.md)。本章输出分项 `PreflightReport`，将来源不明、依赖解析、模型缺失与业务缺陷分开。

## 1. 固定的新来源与真实业务

来源为 [kevin-sparkflow/ml-inference-service](https://github.com/kevin-sparkflow/ml-inference-service)，固定 commit `c200bcb4721c4428a9bfe808ca8a61648a2b16f6`。本次来源审计识别 27 个文件；重新准备时仍须核验精确对象和每个文件摘要，不能跟随默认分支漂移。

这个上游是 **MobileNetV2 异步图片分类模板**：FastAPI 接受图片，Redis Streams 将任务交给 ONNX Worker。它不是信用评分系统，也没有已经核验的长期线上历史。用户材料中的信用评分、MLflow、Feast、`/scoring-jobs` 是可选设计方向，不能写成当前源码事实。

| 已审计接口/机制 | 范围与限制 |
| --- | --- |
| `POST /predictions`，multipart `file` | 声明支持 PNG/JPEG/WebP；接受后 `202`，返回 `id` 与 `status=queued` |
| `GET /predictions/{id}` | 状态 `queued / processing / done / failed`，具体响应以固定源码与真实运行核对 |
| `GET /health` | 检查 Redis 可达，不证明模型加载、Worker 或端到端推理健康 |
| Redis Streams `inference:queue:v2` | Consumer、`XAUTOCLAIM`、Lua 租约/fencing 的存在需真实故障实验验证语义 |
| AOF `everysec`、任务/结果 TTL 3600 秒 | 不能宣称零数据丢失或永久保留；TTL 到期需要独立业务约定 |
| MobileNetV2 ONNX | 模型字节、许可、下载来源、预处理、输入输出形状均需独立冻结 |
| 缺少每任务模型身份/alias 绑定、API 幂等键 | 属于显式能力缺口，不可由适配器虚构支持，也不能称为已确认上游 bug |

原 Eventuate 固定来源与 `sut/upstream.lock.json` 保留历史身份。新来源使用 `sut/ml-inference/upstream.lock.json` 与 `sut/ml-inference/workspaces/baseline/`，不覆盖旧锁，不改旧项目。

## 2. 文件与责任

| 路径 | 责任 |
| --- | --- |
| `config/active-business.json` | 当前主业务和受控来源/适配器引用，不授予任意 URL 或命令执行权 |
| `sut/ml-inference/upstream.lock.json` | 仓库、commit、文件摘要、许可、导出方式与来源证据 |
| `sut/ml-inference/workspaces/baseline/` | 固定上游副本；不混入候选、新功能或隐藏答案 |
| `config/toolchain-lock.json` 及后续 ML revision | 区分 TS 已验证工具与 ML 尚未核验的 Python/ONNX/容器矩阵 |
| `artifacts/evidence/preflight/<inspection_id>/` | 来源、依赖、模型工件和真实命令结果 |
| `reports/preflight/<inspection_id>/` | 脱敏摘要与下一步阻断项 |

以上包含既有文件与本次迁移目标，是否已创建由实际文件和执行报告决定。工具不能因为配置中出现路径就相信它经过审核；真实路径、符号链接、摘要和允许根目录都要校验。

## 3. 小步实施

### 03.1 冻结来源，不运行未经审查的上游脚本

记录远端对象 commit、tree、导出文件清单与许可。下载/导出到任务专属临时目录，先审查 Dockerfile、Compose、Python requirements、模型获取脚本、入口与网络请求。禁止把 `.git`、缓存、凭据或下载失败页面当源文件复制。

源码锁只证明“读到什么”，不证明“能构建”。记录缺失或未知摘要为 null/NOT_RUN，不能填示例 SHA。准备副本重复执行时应逐文件核验，发现漂移则拒绝覆盖并给出差异。

### 03.2 核对宿主与执行域

按实际需要读取 OS/CPU、Node/pnpm、Python、Git、Docker client/server 版本。Docker CLI 存在与 daemon 可用分开记录；确认只使用本任务许可的本机宿主，不切换陌生或生产 context。不打印完整环境变量、Docker 凭据或云配置。

已有 Node/pnpm 骨架无需重建。新 ML 构建需单独核对 Python ABI、ONNX Runtime wheel、Pillow/NumPy 以及镜像架构兼容性；macOS ARM 的工具可用不意味着 Linux ARM64 镜像和 wheel 已通过。镜像标签只能用于发现，运行身份最终固定 digest。

可信宿主能管理本实验的资源；候选安装/构建/测试进程无 Docker socket、宿主目录写权、治理 DB、模型 API key 和发布凭据。Python build backend、安装脚本、模型解析与 import 都是代码执行，不能当作只读文本检查。

### 03.3 分层验证依赖与模型工件

1. 读取依赖声明与已有锁；不存在完整可复现锁时如实登记，不能把 requirements 中若干版本误称传递依赖全锁定。
2. 在任务专属、无秘密执行域解析依赖，记录具体 wheel/镜像摘要、索引与失败原因；不要直接升级全部版本。
3. 审核模型下载来源与许可，冻结 ONNX 字节摘要和文件大小；下载 URL、文件名或“MobileNetV2”名称不是内容身份。
4. 核对模型输入 shape/type、输出维度/标签映射、归一化/resize/channel order 与 ONNX provider。模型输出不能脱离预处理解释。
5. 先验证构建/import/模型加载，再由05执行真实 API→队列→Worker→结果链。每层独立记录，不用最后一项覆盖前面的失败。

首次观察到的 hash 是内容指纹，不自动提供独立来源真实性。必要构建修补单独记 `build_revision`；改变模型选择、预处理、状态或持久化语义则属于 `business_revision`，不得伪装环境修补。

### 03.4 形成准入报告

```ts
type PreflightReport = {
  inspection_id: string;
  business: "ASYNC_ML_INFERENCE_RELEASE_VERIFICATION";
  source_commit: string;
  source_manifest_ref: string;
  host_arch: string;
  toolchain_lock_ref: string | null;
  model_manifest_ref: string | null;
  checks: Array<{
    id: string;
    status: "PASS" | "FAIL" | "NOT_RUN" | "INCONCLUSIVE";
    evidence_refs: string[];
    reason_code?: string;
  }>;
  readiness: "BLOCKED" | "READY_FOR_BASELINE";
};
```

这是待统一到共享 contracts 的报告协议，不表示完整预检器已经实现。`READY_FOR_BASELINE` 只允许尝试05固定范围；模型未下载、构建未完成时仍可完成源码冻结和 TS 适配器开发，不能宣称业务就绪。

## 4. 正负验收

| 情况 | 结果与证据 |
| --- | --- |
| 固定 commit 与清单一致 | 来源核验通过；不附带运行 PASS |
| 源文件漂移/符号链接越界/缺许可 | 拒绝准备，保留差异，不覆盖旧目录 |
| Docker client 在但 daemon 不可达 | 容器步骤 BLOCKED；不影响只读来源审计 |
| 依赖/模型下载失败或架构不支持 | ENVIRONMENT，保存坐标和脱敏原因 |
| 运行配置需要宿主 socket 或不明远端 | 拒绝执行配置 |
| 已有旧 Eventuate PASS | 仅引用历史范围，ML 当前项仍 NOT_RUN |
| 无每任务模型绑定 | 明确缺口；不能将 ML-02 标作支持或失败已确认 |

失败保留首次命令、退出码与输出摘要，重试增加物理 attempt。缓存问题创建任务专属新缓存，不清用户全局缓存；任何新版本都产生新 revision。报告不得收录凭据、完整生产数据或隐藏模型认证信息。

## 5. 交接

- [ ] 新旧来源、构建与业务修订分开。
- [ ] 真实 API、队列、模型和持久性限制已列明。
- [ ] 构建执行域与可信宿主权限边界明确。
- [ ] 模型/预处理内容身份已取得，或准确保留阻断项。
- [ ] 已执行与 NOT_RUN 分项列出，未把来源冻结当作真实基线。

下一步：复用04骨架，按05接通受控启动、实际端口发现和精确清理，再执行四条图片推理 API smoke；不直接进入模型修复或正式 Final。
