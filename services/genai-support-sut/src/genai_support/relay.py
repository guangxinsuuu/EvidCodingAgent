"""持久outbox → Redis Stream通知；Redis不是任务事实或业务授权来源。

API先在PostgreSQL提交Job/outbox，Relay才发布job_id。发布后标记的回执丢失会
造成重投，由Worker的DB租约/终态去重吸收；不能先标已派发再投递而留下丢失窗口。
"""

import re
from threading import Event
import time
from typing import Any
from uuid import uuid4


STREAM_KEY = "egap:support:jobs:v1"
CONSUMER_GROUP = "support-workers-v1"


class Relay:
    """可信relay角色。构造不访问网络，run_once可由测试或run_forever触发。

    store来自当前实验的SUT数据库，redis来自同一实验的专用实例。固定stream键
    不承担跨实验隔离；环境编排必须保证实例/凭据对应当前实验。
    """

    def __init__(self, store: Any, redis: Any, *, owner: str | None = None) -> None:
        self.store = store
        self.redis = redis
        self.owner = owner or "relay-" + uuid4().hex
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", self.owner) is None:
            raise ValueError("INVALID_RELAY_OWNER")

    def run_once(self, *, limit: int = 20) -> int:
        """发布最多limit条通知，返回已确认标记数；任何不确定异常交上层重试。

        消息只含job_id；tenant、revision和草稿从不由队列输入决定。多relay看到
        同一outbox也只增加通知次数，不能生成新的Job或新的模型逻辑请求。
        """
        self.store.heartbeat("relay", self.owner)
        delivered = 0
        for item in self.store.pending_outbox(limit=limit):
            self.redis.xadd(STREAM_KEY, {"job_id": item.job_id})
            self.store.mark_dispatched(item.outbox_id)
            delivered += 1
        return delivered

    def reconcile_once(self, *, limit: int = 20) -> int:
        """DB扫描修复Redis通知丢失；只重投原job_id，不重建业务任务。

        仅用于启动/低频恢复，不在每次空轮询无限追加。PROCESSING须租约过期后
        才被store选出；真实模型是否已发出仍由gateway journal判断，不能据此重试。
        """
        published = 0
        for job_id in self.store.reclaimable_jobs(limit=limit):
            self.redis.xadd(STREAM_KEY, {"job_id": job_id})
            published += 1
        return published

    def run_forever(self, stop: Event, *, poll_seconds: float = 0.25,
                    recovery_seconds: float = 30.0) -> None:
        """角色入口，停止事件由主进程信号处理器设置；不清理别的环境资源。"""
        if not 0.01 <= poll_seconds <= 10 or not 1 <= recovery_seconds <= 600:
            raise ValueError("INVALID_RELAY_POLL_POLICY")
        next_recovery = 0.0
        while not stop.is_set():
            try:
                self.run_once()
                if time.monotonic() >= next_recovery:
                    self.reconcile_once()
                    next_recovery = time.monotonic() + recovery_seconds
            except Exception:
                # 不打印DB/Redis异常文本，连接URL可能含密码。保留outbox供恢复。
                print("SUPPORT_RELAY_ITERATION_UNKNOWN", flush=True)
            stop.wait(poll_seconds)
