"""翻译调度器(ticket-007, 工程书 §25-30)。

路由(§34 + ticket-007): submit() 先算复杂度 ——
- ≤ queue.async_threshold(默认 0.25, 词典≈0.1/规则≈0.25 档) → 同步快路径:
  缓存(L1+L2)/词典/规则, 绝不调 LLM(主线程快返回); 未命中 → 下探异步;
- 高于阈值 → 直接入内存优先级队列, 本地 worker 异步翻译, 立即返回原文占位
  (§29: 游戏主线程绝不等待 LLM)。

异步语义:
- 完成时触发 on_translation_done(job, result)(§25 事件回调, DFHack 桥接入点);
- 失败重试 max_attempts 次后 FAILED + on_translation_failed(job)(§29/§30);
- §29 超时分级: realtime(P0) 任务默认 deadline = realtime_timeout_ms(10s),
  worker 取出时已超时 → 降级 background(见 worker 模块);
- 队列满(§27 max_queue=32) → 拒绝并返回原文回退结果, 绝不阻塞主流程;
- 存储集成: 有 store 时任务生命周期写入 translation_jobs 表(§42 崩溃恢复),
  privacy.store_source_text=false 时只记录任务不落原文(§46 隐私)。

云端 worker 为第三阶段: worker 模块预留同一定时/循环契约, 无需改动内核。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import TYPE_CHECKING, Any, Callable

from df_fanyi.core.orchestrator import TranslationResult
from df_fanyi.core.parser import normalize_text, source_hash
from df_fanyi.core.queue.heuristic import ASYNC_THRESHOLD_DEFAULT, complexity_score
from df_fanyi.core.queue.priority import (
    PRIORITY_NAMES,
    Priority,
    PriorityQueue,
    TranslationJob,
    JobStatus,
)
from df_fanyi.core.queue.worker import LocalTranslationWorker

if TYPE_CHECKING:
    from df_fanyi.core.orchestrator import Orchestrator
    from df_fanyi.database.store import SQLiteStore

logger = logging.getLogger("df_fanyi.queue.scheduler")


class AsyncHandle:
    """异步任务的等待句柄: wait() 阻塞到终态(COMPLETED/FAILED), 不抛异常。"""

    def __init__(self, job: TranslationJob) -> None:
        self._job = job
        self._done = threading.Event()

    @property
    def job(self) -> TranslationJob:
        return self._job

    @property
    def job_id(self) -> str:
        return self._job.id

    @property
    def ready(self) -> bool:
        """任务是否已到终态(COMPLETED/FAILED)。"""
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> TranslationJob:
        """阻塞至终态; 超时返回 job(状态可能仍 RUNNING/PENDING, 调用方按需轮询)。"""
        self._done.wait(timeout)
        return self._job


class Submission:
    """submit() 返回值。

    - 同步快路径命中: result 即最终译文, is_async=False;
    - 异步路径: result 是原文占位(§29: 立即可用), handle 等待后台完成。
    """

    def __init__(
        self,
        *,
        result: TranslationResult,
        handle: AsyncHandle | None = None,
    ) -> None:
        self._result = result
        self._handle = handle

    @property
    def is_async(self) -> bool:
        return self._handle is not None

    @property
    def result(self) -> TranslationResult:
        """立即可得的结果: 同步=译文, 异步=原文占位。"""
        return self._result

    @property
    def handle(self) -> AsyncHandle | None:
        return self._handle

    def wait(self, timeout: float | None = None) -> TranslationResult:
        """阻塞到后台完成; 超时/失败 → 原文回退结果(绝不抛异常, §30 铁律)。"""
        if self._handle is None:
            return self._result
        job = self._handle.wait(timeout)
        if job.status == JobStatus.COMPLETED and job.result is not None:
            return job.result
        if job.status == JobStatus.FAILED:
            return TranslationResult(
                job.source_text,
                job.source_text,
                error=job.error or "translation failed",
            )
        return self._result  # 超时未完成 → 仍是原文占位


class TranslationScheduler:
    """优先级队列 + 本地 worker 池(工程书 §25-30, ticket-007)。"""

    def __init__(
        self,
        translator: "Orchestrator",
        *,
        workers: int = 1,
        max_queue: int = 32,
        async_threshold: float = ASYNC_THRESHOLD_DEFAULT,
        max_attempts: int = 3,
        realtime_timeout_ms: int = 10_000,
        store: "SQLiteStore | None" = None,
        store_source_text: bool = True,
        autostart: bool = True,
    ) -> None:
        self._translator = translator
        self._queue = PriorityQueue(max_size=max_queue)
        self._store = store
        self._store_source_text = store_source_text
        self._max_attempts = max(1, max_attempts)
        self._realtime_timeout_ms = max(0, realtime_timeout_ms)
        self._async_threshold = max(0.0, min(1.0, async_threshold))
        self._workers: list[LocalTranslationWorker] = []
        self._started = False
        self._handles: dict[str, AsyncHandle] = {}
        self._handles_lock = threading.Lock()
        self._done_callbacks: set[Callable[[TranslationJob, TranslationResult], None]] = set()
        self._failed_callbacks: set[Callable[[TranslationJob], None]] = set()
        self._cb_lock = threading.Lock()
        self._conf = {
            "workers": max(1, workers),
            "max_queue": max_queue,
            "async_threshold": self._async_threshold,
            "max_attempts": self._max_attempts,
            "realtime_timeout_ms": self._realtime_timeout_ms,
        }
        if autostart:
            self.start()

    # ---- 事件回调(§25, DFHack 桥接入点) ---------------------------------

    def subscribe_done(self, callback: Callable[[TranslationJob, TranslationResult], None]) -> None:
        """注册完成回调: 后台任务完成时回调(job, result)(worker 线程执行)。"""
        with self._cb_lock:
            self._done_callbacks.add(callback)

    def subscribe_failed(self, callback: Callable[[TranslationJob], None]) -> None:
        """注册失败回调: 任务重试耗尽 FAILED 时回调(job)(worker 线程执行)。"""
        with self._cb_lock:
            self._failed_callbacks.add(callback)

    # ---- 提交 -------------------------------------------------------------

    def submit(
        self,
        text: str,
        *,
        priority: Priority = Priority.BACKGROUND,
        context: dict[str, Any] | None = None,
        deadline_ms: int | None = None,
    ) -> Submission:
        """提交一句待翻译文本。

        - 复杂度 ≤ 阈值 → 同步快路径(缓存/词典/规则, 不调 LLM);
        - 否则入队异步, 立即返回原文占位(主线程不等待 LLM, §29);
        - realtime(P0) 未给 deadline_ms 时取 realtime_timeout_ms(§29 默认 10s)。
        """
        normalized = normalize_text(text)
        if not normalized:
            return Submission(result=TranslationResult("", "", error="empty input"))
        try:
            priority = Priority(priority)
        except ValueError:
            priority = Priority.BACKGROUND
            logger.warning("非法优先级 %r, 降级为 background", priority)
        if priority == Priority.REALTIME and deadline_ms is None:
            deadline_ms = self._realtime_timeout_ms

        # §34: 复杂度 ≤ 词典/规则档阈值 → 同步快路径(命中即返回)
        if complexity_score(normalized) <= self._async_threshold:
            fast = self._translator.fast_translate(normalized, context=context)
            if fast is not None:
                return Submission(result=fast)

        # 异步路径: 先注册句柄(防 worker 比注册更早完成), 再入队
        job = TranslationJob(
            id=uuid.uuid4().hex,
            priority=priority,
            text_hash=source_hash(normalized, context),
            source_text=normalized,
            context=context,
            deadline_ms=deadline_ms,
        )
        handle = AsyncHandle(job)
        with self._handles_lock:
            self._handles[job.id] = handle
        if not self._queue.push(job):
            with self._handles_lock:
                self._handles.pop(job.id, None)
            logger.warning("队列已满(§27 max_queue=%s), 丢弃任务: %r", self._conf["max_queue"], normalized[:40])
            return Submission(result=_fallback(normalized, "queue full: dropped"))
        if self._store is not None and self._store_source_text:
            try:
                self._store.job_enqueue(
                    job.text_hash,
                    normalized,
                    priority=int(priority),
                    context_json=json.dumps(context, ensure_ascii=False) if context else None,
                    job_id=job.id,
                )
            except Exception as exc:  # noqa: BLE001 — 持久层故障不阻塞翻译
                logger.warning("job_enqueue 持久化失败(任务仍在内存队列): %s", exc)
        return Submission(result=_placeholder(normalized, priority), handle=handle)

    # ---- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        """启动 worker 池。autostart=True(默认)时构造即启动; False 由调用方显式启动。"""
        self._ensure_workers()

    def shutdown(self, timeout: float | None = 5.0) -> None:
        """停止接收 + 关闭队列 + 等待 worker 退出(未完成的内存任务不再处理)。"""
        self._queue.close()
        for worker in self._workers:
            worker.stop()
        for worker in self._workers:
            worker.join(timeout=timeout)

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    # ---- 内部 -------------------------------------------------------------

    def _ensure_workers(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self._conf["workers"]):
            worker = LocalTranslationWorker(
                self._queue,
                self._translator,
                store=self._store,
                max_attempts=self._conf["max_attempts"],
                on_done=self._handle_done,
                on_failed=self._handle_failed,
                name=f"df-fanyi-local-{index}",
            )
            self._workers.append(worker)
            worker.start()
        logger.info("启动 %d 个本地 worker(§27 默认 1)", self._conf["workers"])

    def _handle_done(self, job: TranslationJob, result: TranslationResult) -> None:
        with self._handles_lock:
            handle = self._handles.pop(job.id, None)
        if handle is not None:
            handle._done.set()
        with self._cb_lock:
            callbacks = list(self._done_callbacks)
        for callback in callbacks:
            try:
                callback(job, result)
            except Exception as exc:  # noqa: BLE001 — 回调异常不影响任务结果
                logger.warning("on_translation_done 回调异常: %s", exc)

    def _handle_failed(self, job: TranslationJob) -> None:
        with self._handles_lock:
            handle = self._handles.pop(job.id, None)
        if handle is not None:
            handle._done.set()
        with self._cb_lock:
            callbacks = list(self._failed_callbacks)
        for callback in callbacks:
            try:
                callback(job)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_translation_failed 回调异常: %s", exc)


def _placeholder(text: str, priority: Priority) -> TranslationResult:
    """原文占位: 异步排队即立即可用(§29 主流程不等待)。"""
    return TranslationResult(
        text,
        text,
        model="queued",
        provider="local",
        confidence=0.0,
        is_placeholder=True,
        error=f"异步排队({PRIORITY_NAMES[int(priority)]}), 后台完成后刷新",
    )


def _fallback(text: str, error: str) -> TranslationResult:
    """不可用时的同步回退(队列满等): 显示原文, 不阻塞。"""
    return TranslationResult(text, text, confidence=0.0, error=error)