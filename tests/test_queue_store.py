"""ticket-007: 调度器 ↔ SQLite 任务表集成(§42 状态机 + §46 隐私)。

- 异步任务生命周期: PENDING → RUNNING → COMPLETED(store 行);
- 重试耗尽 → FAILED(store 行 error 记录);
- privacy.store_source_text=false → 不落原文(§46 只保留 hash 级信息);
- 崩溃恢复语义: 调度器崩溃时 RUNNING 行由 §42 recover_crashed 重排队。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.orchestrator import Orchestrator  # noqa: E402
from df_fanyi.core.queue import JobStatus, Priority, TranslationScheduler  # noqa: E402
from df_fanyi.database.store import JobStatus as StoreStatus  # noqa: E402
from df_fanyi.database.store import SQLiteStore  # noqa: E402

CN_REPLY_8 = "古老的矮人厅中回荡着镐子敲击石头的声音。" * 8


def long_text(seed: str, reps: int = 8) -> str:
    return (f"The {seed} echo with the sound of pickaxes striking against ancient stone. " * reps).strip()


class RecorderLLM:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def __call__(self, text: str) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("llm down")
        return CN_REPLY_8


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "engine.db")


def test_async_job_lifecycle_persisted(store: SQLiteStore) -> None:
    orch = Orchestrator(llm=RecorderLLM(), store=store)
    sched = TranslationScheduler(orch, workers=1, store=store, max_attempts=3)
    try:
        text = long_text("vaulted halls")
        sub = sched.submit(text, priority=Priority.IMPORTANT)
        job = sub.handle.job
        row_before = store.job_get(job.id)
        if row_before is None:  # worker 已抢先完成
            sub.wait(timeout=15)
            row_before = store.job_get(job.id)
        assert row_before is not None
        assert row_before["source_hash"] == job.text_hash
        assert row_before["priority"] == int(Priority.IMPORTANT)
        sub.wait(timeout=15)
        row = store.job_get(job.id)
        assert row["status"] == StoreStatus.COMPLETED
        assert row["started_at"] is not None
        assert row["completed_at"] is not None
        assert row["error"] is None
    finally:
        sched.shutdown()


def test_failed_job_persists_error_and_status(store: SQLiteStore) -> None:
    orch = Orchestrator(llm=RecorderLLM(fail=True), store=store)
    sched = TranslationScheduler(orch, workers=1, store=store, max_attempts=3)
    try:
        text = long_text("haunted mines")
        sub = sched.submit(text)
        sub.wait(timeout=30)
        row = store.job_get(sub.handle.job.id)
        assert row is not None
        assert row["status"] == StoreStatus.FAILED
        assert "llm down" in (row["error"] or "")
        assert row["completed_at"] is not None
    finally:
        sched.shutdown()


def test_privacy_off_stores_no_source_text(store: SQLiteStore) -> None:
    """§46: store_source_text=false → 调度器不落原文(任务行缺省, 只留内存)。"""
    orch = Orchestrator(llm=RecorderLLM(), store=store)
    sched = TranslationScheduler(orch, workers=1, store=store, store_source_text=False)
    try:
        text = long_text("secret vaults")
        sub = sched.submit(text)
        sub.wait(timeout=15)
        jobs = store.job_list()
        assert all(job["source_text"] != text for job in jobs)  # 原文绝不落库
    finally:
        sched.shutdown()