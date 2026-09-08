"""翻译 worker(ticket-007, 工程书 §26-29)。

§26 建议配置: 1 个 Translation Scheduler + 1~2 个 Local LLM Worker。
本仓库默认本地 worker=1(§27: 不在 4800U 上启动大量并发 LLM, 可配 workers_local)。

云端 worker 预留接口(第三阶段): 云端 worker 与本模块同一定时/循环契约
(线程 start + 队列 pop + 终态回调), 第三阶段实现后接入 TranslationScheduler 即可,
内核无需改动。

本地 worker 语义:
- 失败重试: attempts 未达 max_attempts(默认 3) → 重新入队(降级任务按新优先级);
  达上限 → FAILED + on_failed(job)(§29, 队列绝不因单任务卡死 —— §30 铁律);
- §29 超时分级: realtime 任务超过 deadline_ms → was_demoted=True + 优先级降为
  background(后续重试不再抢实时队列; 任务本身仍会完成, 结果照常回调);
- 任何异常(编排器/LLM/回调)都按失败处理并记录, worker 循环永不中断。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from df_fanyi.core.queue.priority import JobStatus, Priority, TranslationJob

if TYPE_CHECKING:
    from df_fanyi.core.queue.priority import PriorityQueue
    from df_fanyi.core.orchestrator import Orchestrator, TranslationResult
    from df_fanyi.database.store import SQLiteStore

logger = logging.getLogger("df_fanyi.queue.worker")

# 完成/失败回调签名(DFHack 桥接入点, §25 事件)
DoneCallback = Callable[[TranslationJob, "TranslationResult"], None]
FailedCallback = Callable[[TranslationJob], None]


class LocalTranslationWorker(threading.Thread):
    """本地翻译 worker: 队列 pop → 编排器 translate → 终态/重试/回调。

    构造参数: queue(共享 PriorityQueue), translator(编排器, 含 LLM),
    store / max_attempts(失败重试上限), on_done / on_failed(调度器注入的事件回调)。
    """

    def __init__(
        self,
        queue: "PriorityQueue",
        translator: "Orchestrator",
        *,
        store: "SQLiteStore | None" = None,
        max_attempts: int = 3,
        on_done: DoneCallback | None = None,
        on_failed: FailedCallback | None = None,
        name: str = "df-fanyi-local",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._queue = queue
        self._translator = translator
        self._store = store
        self._max_attempts = max(1, max_attempts)
        self._on_done = on_done or (lambda _job, _result: None)
        self._on_failed = on_failed or (lambda _job: None)
        self._stop_requested = threading.Event()

    def stop(self) -> None:
        """请求退出(队列关闭后 pop 返回 None, 循环自然结束)。"""
        self._stop_requested.set()

    def run(self) -> None:
        while not self._stop_requested.is_set():
            job = self._queue.pop(timeout=0.5)
            if job is None:
                if self._queue.closed:
                    return
                continue
            try:
                self._process(job)
            except Exception as exc:  # noqa: BLE001 — 队列不死锁铁律(§30)
                logger.exception("worker 处理任务异常: %s", exc)
                self._fail(job, f"worker internal error: {exc}")

    # ---- 内部处理 ---------------------------------------------------------

    def _process(self, job: TranslationJob) -> None:
        # §29: realtime 任务等待超时 → 取消实时性, 转 background(结果仍会完成)
        if job.priority == Priority.REALTIME and job.deadline_ms is not None:
            elapsed = _ms_since(job.created_ms)
            if elapsed >= job.deadline_ms:
                job.was_demoted = True
                job.priority = Priority.BACKGROUND
                logger.info(
                    "realtime 任务 %s 已等待 %dms ≥ 时效 %dms → 转 background(§29)",
                    job.id,
                    elapsed,
                    job.deadline_ms,
                )
                job.deadline_ms = None
        job.status = JobStatus.RUNNING
        if self._store is not None:
            try:
                self._store.job_start(job.id)
            except Exception as exc:  # noqa: BLE001 — 持久层故障不阻塞翻译
                logger.warning("job_start 持久化失败(任务继续): %s", exc)
        result: "TranslationResult | None"
        error: str | None = None
        try:
            result = self._translator.translate(job.source_text, context=job.context)
        except Exception as exc:  # noqa: BLE001 — 编排器按契约不抛, 这里兜底
            result = None
            error = f"LLM/管线异常: {exc}"
        if result is not None and result.error is None:
            self._complete(job, result)
            return
        self._retry_or_fail(job, (result.error if result is not None else None) or error or "未知错误")

    def _retry_or_fail(self, job: TranslationJob, error: str) -> None:
        """失败重试: attempts 未达上限重新入队, 否则 FAILED(§29/§30)。"""
        if job.attempt >= self._max_attempts:
            self._fail(job, error)
            return
        job.attempt += 1
        job.status = JobStatus.PENDING
        job.error = None
        if not self._queue.push(job):
            # 队列已满/关闭 → 不丢失, 直接 FAILED(§27 max_queue 语义, 不阻塞)
            self._fail(job, f"重试入队失败(队列满/已关闭): {error}")
            return
        logger.info(
            "任务 %s 失败(第 %d 次尝试), 重排队: %s",
            job.id,
            job.attempt,
            error,
        )

    def _complete(self, job: TranslationJob, result: "TranslationResult") -> None:
        job.status = JobStatus.COMPLETED
        job.result = result
        if self._store is not None:
            try:
                self._store.job_complete(job.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("job_complete 持久化失败(任务已完成): %s", exc)
        try:
            self._on_done(job, result)
        except Exception as exc:  # noqa: BLE001 — 回调异常不影响任务结果
            logger.warning("on_translation_done 回调异常: %s", exc)

    def _fail(self, job: TranslationJob, error: str) -> None:
        job.status = JobStatus.FAILED
        job.error = error
        if self._store is not None:
            try:
                self._store.job_fail(job.id, error)
            except Exception as exc:  # noqa: BLE001
                logger.warning("job_fail 持久化失败: %s", exc)
        try:
            self._on_failed(job)
        except Exception as exc:  # noqa: BLE001 — 回调异常不影响终态
            logger.warning("on_translation_failed 回调异常: %s", exc)


def _ms_since(created_ms: int) -> int:
    return int(time.monotonic() * 1000) - created_ms