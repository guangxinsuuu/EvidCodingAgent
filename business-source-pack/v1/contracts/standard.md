# 合成客户订单表与实验退款补充条款

实例：${contract_id} / ${version}。客户：${customer_id}。订阅：${subscription_id}。
有效起点：${effective_from}。有效终点：${effective_to}。合同状态：${status}。
接受方式仅为实验准备器记录的 SIMULATED_ACCEPTANCE，不是真实客户签名。

## 固定底稿

引用 Common Paper Cloud Service Agreement Standard Terms 2.1：
https://commonpaper.com/standards/cloud-service-agreement/2.1/
版权作者 Common Paper；开放许可 CC BY 4.0：https://creativecommons.org/licenses/by/4.0/。
下文为本项目新增的实验订单表和补充条款，未修改所引用标准条款原文。

## EGAP-LAB-REFUND-1：仅本地校准的退款例外

明确修改上述标准条款4.1的默认不可退款安排：仅针对本订单表客户和订阅，允许依
refund-standard 政策在续期生效时对应的版本生成资格建议。2026-11-10T00:00:00Z之前
的适用续期使用 lab-v1 的14天窗口；该时刻起使用 lab-v2 的7天窗口。采用UTC经过秒数，
端点包含，不按申请时最新版本选择。

要求已发生第二期或后续的有效续期，合同在续期和申请时均有效；指标为该实验套餐唯一
指定的 units 指标。续期至原始申请的闭区间用量须为0，有完整受控生成器覆盖与实际Lago
聚合校准，以及同发票、币种、金额的模拟付款记录。缺失资料只允许补证建议。

这些例外仅用于模拟客户的本地实验。系统不会执行实际退款或代表真实客户取得权利。
只能形成内部待审核草稿，不能发送客户消息或自动关单。本补充条款优先于底稿4.1的
默认安排；不改变其他标准条款，也不提供本实验以外的法律判断。
