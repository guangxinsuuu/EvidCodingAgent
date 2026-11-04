# 第05章首个实现切片：05.0＋05.1

本轮已完成文件型实验账本与正式基线副本。结论为 **FILE_LAB_SOURCE_READY_CALIBRATION_ONLY**，可继续受控启动切片；没有授予业务健康、BASELINE_GOOD或生产资格。

## 实际结果

- `pnpm typecheck`、`pnpm build`通过；默认`pnpm test`已覆盖新模块，50项测试通过、0失败、0跳过。
- 28项lab测试包含9项来源导出、8项锁、11项账本；其余22项为协议/领域/工件/控制面回归。重复核验不重复计入50项。
- `sut/workspaces/baseline/`实际导出190个文件，来源commit为`894f2dbbe9aba2be6129748b6bbf5f30356deb07`。从Git固定commit重新archive，未复制旧缓存、.git或凭据。
- 仅两份properties显式采用第03章已核验构建补丁；业务Java源码未变，LICENSE保留。
- 第一次CLI返回CREATED；第二次同实验核验返回VERIFIED_EXISTING，来源锁摘要始终为`f302ba765c41742febf810f89b2e79f36db303b838043ca0f8d1e10c176958da`。
- 实际登记实验`experiment_3aa31c85-49e3-4a0a-9365-f2261b16d940`、run `run_daeb0a56-053d-47f4-8733-d78dcff50af7`，两次物理核验分别获得不同check/attempt。
- 实际账本3条事件：EXPERIMENT_CREATED、SOURCE_BOUND、ATTEMPT_ALLOCATED；重开日志后状态一致。没有实际环境/资源/端口，last_epoch仍为0，不以示例epoch冒充已分配环境。
- 原参考190个文件与38个受保护文件保持原样。docs、README、AGENTS和控制面源码未改动。

## 如何阅读实现

| 文件 | 本轮职责 |
|---|---|
| `tools/environment-host/src/cli.ts` | 命令入口；打开账本、登记尝试、调用来源导出、绑定来源锁；固定错误码，不暴露任意shell/Docker入口。 |
| `tools/environment-host/src/lab-ledger.ts` | 身份、来源、环境预分配、资源观察、动作意图/回执、UNKNOWN对账及epoch状态机；日志重放与实时提交使用同一个校验器。 |
| `tools/environment-host/src/lab-lock.ts` | 私有目录、原子排他锁、真实PID/宿主/启动身份核验、显式死锁恢复与恢复守卫。 |
| `tools/environment-host/src/baseline-source.ts` | 固定生产来源策略，硬编码本次已审查commit、文件数、Wrapper/ZIP摘要。 |
| `tools/environment-host/src/baseline-source-core.ts` | 校验预检证据、Git来源、全文件摘要与模式；构建补丁；暂存整棵sut后原子rename；存在副本时只核对。 |
| `packages/contracts/src/lab.ts` | 共享LabIdentity/EnvironmentInstance及严格解析，拒绝身份串用、额外字段、非法epoch、秘密URL和越界引用。 |
| 对应`*.test.ts` | 临时Git仓库、真实竞争进程、SIGKILL、半写尾、损坏记录、旧epoch、UNKNOWN与幂等副本的验收。 |
| 包配置和根脚本 | 加入tools工作区/TS引用；注册`test:lab`和`egap:lab`；默认test纳入新模块。没有增加第三方依赖。 |
| `sut/upstream.lock.json` | 上游/构建/业务三个身份，190文件原始与有效摘要、模式、许可及构建输入。 |
| `sut/build-inputs/` | 保存校验metadata和供审阅的脱敏补丁；不是候选源码或业务PASS。 |

主要调用链：

```text
pnpm egap:lab prepare-source
  → cli.main
    → LabLedger.open → acquireLabLock → 日志重放/快照恢复
    → createExperiment 或 newAttempt
    → prepareBaselineSource → prepareBaselineSourceWithPolicy
      → 证据/ZIP/Git核验 → git archive → 两properties补丁 → sut整体落位
    → bindSource → 事件fsync → 原子快照
    → close → 校验owner并释放锁
```

## 为什么这样实现

**日志为准，快照可重建。** 事件带sequence、previous_hash与hash；完整事件fsync后才算提交。若快照写入失败，明确返回EVENT_COMMITTED_SNAPSHOT_FAILED和序号，禁止当前handle继续写，防止调用者盲目重试制造重复实验。半写尾先隔离并fsync再截断；完整换行但内容/摘要/状态转换错误时拒绝恢复，不悄悄丢弃。

**不靠超时抢锁。** 锁记录token、PID、hostname、boot_identity。只有显式expected token一致、同宿主同启动周期且kill(pid,0)明确ESRCH才可恢复；活PID、权限不明、身份不完整均拒绝。恢复守卫阻止两个恢复者同时移动旧锁，旧owner与恢复回执保留。若宿主已重启、owner缺失或恢复守卫本身遗留，先人工核查，不自动清空。

**未知外部结果不能当失败重发。** beginAction持久化普通动作意图；重开后未完成动作投影UNKNOWN，阻断普通新动作和接管。未来可信只读collector检查实际资源/屏障后才能提交reconcileAction回执。collector尚未实现；对账不能另建普通INSPECT动作来绕开UNKNOWN。回执中的observed_resource_ids表示已检查的账本资源ID，包含确认已不存在者。

**分配与运行事实分开。** reserveEnvironment从账本实际递增分配epoch，输出PLANNED；资源和实测manifest由未来provisioner登记。所有相关状态入口校验epoch，旧epoch拒绝。接管必须覆盖完整资源清单、没有未决动作，并清除旧manifest要求重新观察。它只约束可信合作入口，不对绕开入口的Docker客户端提供外部fencing。

**源码整体落位且不覆盖污染。** 来源锁、构建输入和源码一起暂存，再以一次rename提交。已有副本连额外空.git/.gradle目录、文件模式变化或symlink都拒绝；不删除脏副本重来。崩溃留下的导出锁/暂存目录也不按时间擅自清理。

## 关键验证

- 两个真实进程争抢写锁，只允许一个成功；两个恢复进程不能同时回收同一死锁。
- 真实子进程在事件fsync之后、snapshot rename之前被SIGKILL，恢复后已提交实验身份仍在。
- 缺失/损坏快照可从完整日志重建；半写尾隔离；完整坏hash或非法事件拒绝。
- UNKNOWN→对账→新epoch→新动作→再次重开，旧结果保留、新epoch保留、新未决动作变UNKNOWN。
- 旧epoch动作/资源登记拒绝；不完整资源对账拒绝；实际环境manifest不能只靠规划字段填充。
- 两个临时Git导出竞争、脏/半成品/证据损坏/ZIP损坏/符号链接/缓存污染均经过负例验证。
- 真实源码准备和第二次核验保持同一来源锁，第二次生成不同check/attempt，未重复绑定来源。

测试中使用的容器ID、端口和manifest仅为临时合成输入；没有将它们登记到实际账本。当前机器实际账本只有实验与来源绑定。

## 命令与使用边界

```sh
pnpm test
pnpm egap:lab status
pnpm egap:lab prepare-source --experiment experiment_3aa31c85-49e3-4a0a-9365-f2261b16d940
```

第三条会执行一次新的来源核验并登记新的check/attempt；不修改已经核对一致的副本。不带--experiment会创建新实验。源码目录中的Wrapper使用容器内ZIP路径，不应在宿主baseline目录直接运行Gradle。后续构建应把锁定输入复制到隔离构建域。

本工具沙箱禁止读取macOS启动身份，因此含锁的测试和真实CLI使用了已获准的宿主执行；代码不会自动提权，也不会在核验失败时降级为猜测。

证据在`artifacts/evidence/baseline/20261104T044024Z-05ab/`，包含命令账本、输出摘要、实际来源回执、日志/快照副本、源码清单及完整性检查。运行账本在`artifacts/lab/local/`，两者用途分开。实际writer/recovery/export锁和导出暂存目录已清理；保留正式sut、账本和证据。本轮没有创建Docker资源。

## 下一切片

接05.2–05.3审查启动入口、实现可信provisioner、记录真实资源/拓扑/动态端口与environment epoch，并同步提供按精确资源清单清理。之后执行四条真实API smoke：足额批准、额度不足拒绝、未知客户拒绝、恰等额批准。数据库/Kafka/CDC、服务镜像ARM兼容、业务smoke、只读外部对账采集与独立Oracle均未执行；不把本次单机基础代码称作第五章全部完成。
