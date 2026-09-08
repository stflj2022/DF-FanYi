"""ticket-007 验收: 优先级队列(P0-P4)机制(工程书 §25)。

- 最小堆: P0 恒先于 P3(验收); 同优先级 FIFO;
- max_size 满员拒绝(push=False), 不阻塞;
- close 后 pop 返回 None → worker 优雅退出, 队列不死锁(§30)。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.queue import JobStatus, Priority, PriorityQueue, TranslationJob  # noqa: E402


def make_job(text: str, priority: Priority, **kw: object) -> TranslationJob:
    return TranslationJob(id=f"j-{abs(hash(text))}", priority=priority, text_hash="h", source_text=text, **kw)


def test_p0_popped_before_p3_same_queue() -> None:
    """验收: P0 恒先于 P3 被取出(不依赖插入顺序)。"""
    q = PriorityQueue()
    q.push(make_job("p3-first", Priority.BACKGROUND))
    q.push(make_job("p0", Priority.REALTIME))
    q.push(make_job("p3-second", Priority.BACKGROUND))
    assert q.pop().source_text == "p0"
    assert q.pop().source_text == "p3-first"  # 同优先级 FIFO
    assert q.pop().source_text == "p3-second"


def test_all_five_priorities_ordering() -> None:
    """§25: P0 < P1 < P2 < P3 < P4 五级完整有序。"""
    q = PriorityQueue(max_size=None)
    for p in (Priority.PREFETCH, Priority.IMPORTANT, Priority.REALTIME, Priority.BACKGROUND, Priority.INTERACTIVE):
        q.push(make_job(f"job-{int(p)}", p))
    order = [q.pop().priority for _ in range(5)]
    assert order == [0, 1, 2, 3, 4]


def test_job_has_spec_fields() -> None:
    """§25 Job 字段: id/priority/text_hash/attempt/deadline_ms。"""
    job = TranslationJob(
        id="abc", priority=Priority.IMPORTANT, text_hash="deadbeef", source_text="x", attempt=2, deadline_ms=3000
    )
    assert job.id == "abc"
    assert job.priority == Priority.IMPORTANT
    assert job.text_hash == "deadbeef"
    assert job.attempt == 2
    assert job.deadline_ms == 3000
    assert job.status == JobStatus.PENDING


def test_max_size_rejects_when_full() -> None:
    q = PriorityQueue(max_size=2)
    assert q.push(make_job("a", Priority.BACKGROUND)) is True
    assert q.push(make_job("b", Priority.BACKGROUND)) is True
    assert q.push(make_job("c", Priority.REALTIME)) is False  # 满员拒绝, 不阻塞
    assert q.qsize() == 2


def test_pop_blocks_then_returns_after_push() -> None:
    q = PriorityQueue()
    result: list[str] = []
    thread = threading.Thread(target=lambda: result.append(q.pop(timeout=5).source_text))
    thread.start()
    time.sleep(0.05)
    q.push(make_job("wake", Priority.PREFETCH))
    thread.join(3)
    assert result == ["wake"]


def test_pop_none_after_close_deadlock_free() -> None:
    q = PriorityQueue()
    q.close()
    assert q.pop(timeout=1) is None  # 关闭后 pop 不阻塞不报错(§30 队列不死锁)


def test_pop_timeout_returns_none_and_keeps_queue_usable() -> None:
    q = PriorityQueue()
    assert q.pop(timeout=0.05) is None
    q.push(make_job("later", Priority.BACKGROUND))
    assert q.pop(timeout=1).source_text == "later"