# 05 · GenAI客服真实基线：可恢复工单与可信工具记录

状态：`GENAI_MIGRATION_IN_PROGRESS / LIVE_NOT_RUN`。已有file-lab、Docker低层操作与新来源/客户端迁移基础；自有客服SUT、真实LLM、数据库任务及本章真实smoke尚未接通。前置[03预检](03-environment-preflight.md)、[04骨架](04-project-skeleton.md)，后续[06草案](06-contract-registry.md)、[07观察](07-observer-and-experiment-barrier.md)。

## 1. 目标与范围

搭建合成B2B SaaS续费争议处理链：提交工单→持久Job/outbox→Redis投递→Support Worker调用LLM/政策/账单工具→持久草稿→DONE。只生成待审草稿，敏感动作只经沙盒工具；不接真实退款或客户发送。

源码参考、派生SUT、健康Stable是三个对象。OpenAI航空Demo没有本章的Job API、SaaS数据和事务保证；这些是拟建自有业务。ML与Eventuate保留历史，不复用它们的资格、环境ID或结果。

## 2. 文件与分工

| 文件/模块 | 责任 |
| --- | --- |
| `tools/environment-host/src/cli.ts`、业务注册 | 选择明确GenAI来源与独立账本，拒绝旧绑定 |
| `lab-ledger.ts`、`lab-lock.ts` | 实验/attempt/环境/action身份、追加事件、恢复与UNKNOWN |
| `docker-host.ts` | 固定socket、精确资源归属的低层动作；拓扑编排仍需实现 |
| `services/genai-support-sut/` | 拟建API/Worker/业务工具与独立Python锁 |
| `packages/genai-support-adapter/` | 受控客户端协议、fixture和API smoke；mock不算live |
| `artifacts/lab/genai-support/` | 新业务file-lab账本；不复制旧账本改名 |
| `sut/genai-support/` | 来源锁/参考副本；自有候选另行冻结 |

## 3. 05.0 实验身份先于动作

复用LabIdentity分配experiment/run/check/attempt；明确`authority_mode=file-lab`、`evidence_scope=calibration-only`。动作登记绑定来源、business/build revision、fixture reset、environment instance、lease epoch和请求摘要。它们是身份不是权限凭据。

只有持锁协调器写账本；相同实验重验产生新的物理attempt，不覆盖历史。文件日志/快照可重建，残留锁只按已证明的同host/boot进程死亡恢复；不能按等待时长删锁。未知容器创建、模型请求或提交必须先对账，不换ID重复效果。

## 4. 05.1 参考来源与派生业务基线

先核验固定参考源码，再构造独立自有SUT revision；复制源码须保留MIT许可及修改来源。基线输入包括代码/依赖、Prompt、provider/model配置、政策corpus/index/retrieval配置、工具Schema/adapter、输出Schema和workflow。模型响应记录实际model/request ID/time；不要填写并不存在的远端权重digest。

首批fixture：CS-1042缺使用/合同证据；完整证据的符合与不符合条件；十一月新政策边界；Tenant B旁观数据。账单/政策/clock由可信fixture准备器提供，候选不能改ground truth。tenant由认证上下文派生，用户文本和body不构成授权。

## 5. 05.2 审核启动拓扑与持久协议

首期单宿主、独立API/Worker、SUT PostgreSQL、Redis、工具网关/模型代理与Observer角色。可信环境宿主创建带所有权label的网络/卷/容器，候选无宿主socket或provider key。模型与工具出口只到登记代理，防止绕过采集。

拟定受理协议：同一SUT事务保存Job和dispatch outbox后才返回202；relay向Redis发送，重复通知经Job身份/attempt条件写处理。Worker的draft提交与Job DONE在同一SUT事务中，随后ack；恢复不把重复执行当成新Job。若实现HTTP幂等，须独立定义key/request digest与冲突语义，未实现前客户端POST超时不得重发。

更新工单增加revision。旧Worker只能对其绑定revision提交；不满足条件时保留旧attempt记录，不覆写新草稿。此处是目标协议，真实DB故障窗口尚未验证。

## 6. 05.3 发现并登记真实环境

实际创建后记录镜像digest、观察到的端口/拓扑、PostgreSQL/Redis命名空间、工具与模型代理端点、fixture reset和失效时间。不能把规划端口当实测值。environment_config_digest来自实际配置；正式Environment Profile尚未冻结时保持null而不是虚构批准。

每次API调用前重新验证环境ID、epoch、租约和scope；loopback不是身份认证。控制面健康只证明控制面，SUT健康分别检查API/存储/队列/worker/代理可用性，不通过一次200推断全部组件已正常。

## 7. 05.4 从协议smoke接到真实链路

目标接口是`POST /support-resolution-jobs`与`GET /support-resolution-jobs/{job_id}`；job_id为32位小写hex，SUT Job状态为`QUEUED|PROCESSING|DONE|FAILED`。POST的202必须包含schema_version、job_id、tenant_id、ticket_id、ticket_revision与QUEUED。GET的DONE须有同ticket/revision的draft且error_code=null；FAILED须draft=null且error_code=PROCESSING_FAILED；非终态两者均null。health只声明schema_version=1/status=ok的API存活。这不是参考上游API。客户端当前只能证明这些拟定协议的校验/错误处理，真实服务须另行实现并核对。

当前四项客户端smoke为health、有效合成工单受理并产生结构化草稿、非法revision返回422、未知Job返回404；错误环境/身份/响应的拒绝由额外客户端负例测试覆盖。具体HTTP字段/状态以实现中的版本化客户端契约为准，不把文档例子当第二套协议。终态轮询有界，未终止保留INCONCLUSIVE，GET失败不重发POST。

真实成功至少记录Job/ticket revision、draft引用、请求/响应摘要和实际工具/模型调用；API smoke仅证明接口/受理/终态形状，不证明政策正确、租户全链隔离或真实模型确已使用。结果标`kind=genai-api-smoke`、`business_oracle=not-performed`、`trusted_tool_trace=not-observed`、`semantic_quality=not-evaluated`，并显式区分scripted-fake/live。待07/08独立校准。

## 8. 05.5 观察、失败与恢复

同一次物理attempt的job、ticket、candidate、tool/LLM call、draft commit必须可关联。Worker自报的模型/政策标签可作线索，网关与DB事实才进入独立证据。没有tool trace不能从“答案看起来对”推导工具正确。

区分provider超时、账单不存在、缺合同信息、政策冲突、模型结构化输出非法、DB写失败与业务拒绝。Job FAILED是SUT业务协议；EGAP执行ERROR/UNKNOWN与断言FAIL/INCONCLUSIVE是另一套协议。缺资料可能合法产生DONE+NEEDS_MORE_EVIDENCE，不代表工单已解决。

模型调用已发出而响应丢失时登记UNKNOWN与未知费用；保留原request关联并查可用结果，不盲重发。重试/重复采样有独立attempt，计入SUT总成本。回复措辞不要求逐字相同，业务断言按批准的结构/事实检查。

## 9. 05.6 精确清理与重建

停止新增业务→对账在途LLM/工具/队列动作→释放屏障→停止本实验Worker→核对owner/epoch逐项清理→记录残留和回执。只操作账本关联资源，不执行全局Docker prune，不删其他业务/旧证据。租约过期不是现实资源已经消失的证据。

重建生成新环境实例与fixture reset。模型/工具能力令牌撤销，缓存按tenant/版本隔离，代理日志和证据按94保留；未知对象移交，不删除账本制造“完成”。

## 10. 验收与交接

| 场景 | 必须观察 |
| --- | --- |
| 有效工单真实跑通 | 202、Job轨迹、实际LLM/工具、已提交draft与DONE关联 |
| 响应丢失、超时或未终止 | 不伪造FAILED/PASS，不重复POST，记录未知 |
| 写draft失败或提交回执未知 | 不冒充完整成功；07进一步验证时间因果 |
| 错tenant/epoch/环境、任意代理URL | 无越权执行，拒绝原因可审查 |
| 重启/重复投递 | 不丢已受理身份，重复计算与重复效果分别记录 |
| 清理中断后恢复 | 先查账本与真实归属，精确完成或记录残留 |

本章真实证据保存`artifacts/evidence/baseline/<experiment>/<attempt>/`；报告区分reference-source、mock-protocol、live-SUT。完成后先冻结06.1–06.4 DRAFT，再做07可信观察与08 SUP判定；不从smoke直接进入正式Final。
