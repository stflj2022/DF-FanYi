"""优先级队列核心(ticket-007, 工程书 §25)。

§25 Queue Protocol: P0 realtime / P1 interactive / P2 important / P3 background /
P4 prefetch, 五级; job 含 id/priority/text_hash/attempt/deadline_ms。

PriorityQueue = 线程安全最小堆:
- 数值小者优先级高, 恒先出队(验收: P0 先于 P3, 假 LLM 控制完成顺序验证);
- 同优先级按入队顺序(FIFO, seq 递增);
- 容量上限 max_size(§27 max_queue=32), 满员拒绝新任务(push 返回 False, 不阻塞);
- close() 后 pop 返回 None → worker 优雅退出, 队列不死锁(§30 铁律)。
"""
from __future__ import annotations

import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    """§25 优先级(数值越小越优先; 配置/日志里的别名见 PRIORITY_NAMES)。"""

    REALTIME = 0       # P0: 玩家即将看到的文本
    INTERACTIVE = 1    # P1: 交互面板
    IMPORTANT = 2      # P2: 公告
    BACKGROUND = 3     # P3: 一般文本
    PREFETCH = 4       # P4: 预取


PRIORITY_NAMES: dict[int, str] = {
    0: "P0",
    1: "P1",
    2: "P2",
    3: "P3",
    4: "P4",
}


class JobStatus:
    """job 生命周期(§29/§42; 与 database.store.JobStatus 语义对齐)。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEMOTED = "DEMOTED"  # §29: realtime 超时降级为 background


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass
class TranslationJob:
    """§25 Job 字段 + 处理状态。

    deadline_ms: realtime 任务的时效窗口(相对 created_ms, 超过即降级 §29);
    result: 完成时的 TranslationResult(延迟导入避免循环依赖, 见 scheduler)。
    """

    id: str
    priority: Priority
    text_hash: str
    source_text: str
    attempt: int = 1
    deadline_ms: int | None = None
    created_ms: int = field(default_factory=_now_ms)
    context: dict[str, Any] | None = None
    status: str = JobStatus.PENDING
    was_demoted: bool = False
    result: Any = None  # TranslationResult, 终态填充
    error: str | None = None


class PriorityQueue:
    """线程安全的最小堆优先级队列(priority, seq, job)。"""

    def __init__(self, max_size: int | None = 32) -> None:
        self._max_size = max_size
        self._heap: list[tuple[int, int, TranslationJob]] = []
        self._seq = itertools.count()
        self._cond = threading.Condition(threading.Lock())
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def push(self, job: TranslationJob) -> bool:
        """入队。已关闭或满员 → False(调用方自行降级处理, 绝不阻塞)。"""
        with self._cond:
            if self._closed:
                return False
            if self._max_size is not None and len(self._heap) >= self._max_size:
                return False
            heapq.heappush(self._heap, (int(job.priority), next(self._seq), job))
            self._cond.notify()
            return True

    def pop(self, timeout: float | None = None) -> TranslationJob | None:
        """阻塞取出最高优先级任务; 空队列等待至多 timeout; 关闭后返回 None。"""
        with self._cond:
            remaining = timeout
            while True:
                if self._heap:
                    return heapq.heappop(self._heap)[2]
                if self._closed:
                    return None
                if remaining is not None and remaining <= 0:
                    return None
                t0 = time.monotonic()
                if remaining is None:
                    self._cond.wait()
                else:
                    self._cond.wait(remaining)
                    remaining -= time.monotonic() - t0

    def qsize(self) -> int:
        with self._cond:
            return len(self._heap)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()