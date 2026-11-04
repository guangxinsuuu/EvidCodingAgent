# EGAP · Async ML Inference Release Verification

本项目面向**异步机器学习推理的新版本发布验证**：提交稳定版本、包含真实功能变化的候选与需求增量，分析受影响行为，选择有针对性的实验；独立Runner/Oracle确认缺陷后，才按授权调用Coding Agent修复，再完成当前候选Final及可选Draft PR/CI。

首个SUT是固定来源的FastAPI + Redis Streams + ONNX图片分类服务（MobileNetV2），不是已建成的信用评分系统。重点验证排队任务的模型/预处理身份、结果提交与完成顺序，随后覆盖重复投递、恢复与schema兼容；真实模型质量评价需另有批准数据和门槛。

**当前已有：**TS控制面健康骨架、共享协议/制品原语、可恢复的单机实验账本、受限Docker适配器、新ML来源冻结与API适配器。新来源固定commit `c200bcb4721c4428a9bfe808ca8a61648a2b16f6`，27个源码文件已冻结，重复准备可核验已有副本。此次迁移typecheck/build通过，默认测试77/77、控制面生命周期集成3/3通过；ML API相关测试使用mock HTTP，不能作为真实业务证据。

**尚未完成：**ML工具链/镜像/模型工件的完整运行预检、真实四场景smoke、可信Observer/实验屏障、ML Oracle、Python LangGraph Verifier及正式发布闭环。当前没有ML生产或业务PASS资格。

- 从 [实施手册目录](docs/README.md)、[总体计划](docs/00-overall-plan.md) 和 [架构](docs/01-system-architecture.md) 理解主线。
- 当前入口见 [03 ML环境预检](docs/03-environment-preflight.md)、[05 ML真实基线](docs/05-async-ml-baseline.md)；04与通用账本无需重做。
- [92 真实进度](docs/92-progress-and-session-log.md) 区分已完成、待迁移与未运行；[93 覆盖映射](docs/93-v3-acceptance-coverage.md) 和 [94 数据治理](docs/94-data-flow-and-retention.md) 保留企业门槛。

已注册的本地命令：

```sh
pnpm typecheck
pnpm build
pnpm test
pnpm test:integration
pnpm egap:lab status
pnpm egap:lab prepare-source
pnpm dev:control-plane
```

`prepare-source`默认准备ML，可用 `--reference-repository <固定来源本地Git库>` 指定受控本地来源；它只建立/核验来源和实验身份，不下载模型、不启动SUT。控制面健康端点只声明当前骨架就绪。

主业务配置在 [config/active-business.json](config/active-business.json)，新来源锁在 [sut/ml-inference/upstream.lock.json](sut/ml-inference/upstream.lock.json)，新账本位于 `artifacts/lab/ml-inference/`。旧Eventuate代码归档于 `legacy/eventuate/`，旧来源、账本和证据原样保留；它们不再进入默认构建/业务入口，旧PASS不能转移为ML资格。

架构保留TypeScript治理控制面，未来以 `services/verifier/` 中独立Python LangGraph进程规划实验，通过版本化JSON Schema交接。Coding Agent可修改授权业务和开发测试；可信验收、观察证据和发布批准位于其权限之外。另开一个LLM进程本身不构成独立验证。

**文档修改约定继续有效：**本次业务迁移获得了用户明确授权；之后修改 `docs/**` 或项目README仍须取得明确许可。“继续实现”不自动授权修改文档。详见 [项目指令](AGENTS.md)。历史参考项目与其原始材料保持只读，运行事实与脱敏报告保存到 `artifacts/`、`reports/`。
