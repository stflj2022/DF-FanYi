"""ticket-007 验收: TranslationScheduler —— 异步基础设施(工程书 §25-30)。

验收标准:
- 500+ 字符长句提交后立即返回(原文占位), 后台完成后回调携带中文(Test 6);
- P0 恒先于 P3 被处理(假 LLM 控制完成顺序验证);
- ollama 超时场景: 任务重试 3 次后 FAILED, 队列不死锁;
- §29: realtime 任务超时 → 降级 background; §26/§27: worker 数=1 可配。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.orchestrator import Orchestrator  # noqa: E402
from df_fanyi.core.queue import (  # noqa: E402
    JobStatus,
    Priority,
    TranslationJob,
    TranslationScheduler,
)

CN_REPLY_8 = "古老的矮人厅中回荡着镐子敲击石头的声音。" * 8  # info≈152, 与 500+ 源文同量级


def long_text(seed: str, reps: int = 8) -> str:
    text = (f"The {seed} echo with the sound of pickaxes striking against ancient stone. " * reps).strip()
    assert len(text) > 500
    return text


class RecorderLLM:
    """录制回放假 LLM: 记录调用顺序/次数; 默认生成与源文信息长度成比例的译文
    (校验器长度比 0.3-3x, §24), 可显式 reply 覆盖; 可设置失败或门控。"""

    def __init__(
        self,
        reply: str | None = None,
        *,
        fail: bool = False,
        gate: threading.Event | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.reply = reply
        self.fail = fail
        self.gate = gate

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            raise RuntimeError("ollama timeout simulated")
        if self.reply is not None:
            return self.reply
        return _proportional_reply(text)


def _proportional_reply(text: str) -> str:
    """生成与源文信息长度(汉字+词元)同量级的假译文, 使 §24 长度比校验通过。"""
    import re as _re

    info = len(_re.findall(r"[\u4e00-\u9fff]", text)) + len(_re.findall(r"[A-Za-z0-9]+", text))
    repeat = max(1, round(info / 4))
    return "中文译文。" * repeat


@pytest.fixture
def orch() -> Orchestrator:
    """假 LLM 默认按源文比例生成译文(§24 长度比校验通过)。"""
    return Orchestrator(llm=RecorderLLM())


@pytest.fixture
def orch_cn() -> Orchestrator:
    """假 LLM 固定返回 CN_REPLY_8(仅用于信息长度同量级的长文本场景)。"""
    return Orchestrator(llm=RecorderLLM(reply=CN_REPLY_8))


@pytest.fixture
def scheduler(orch: Orchestrator) -> TranslationScheduler:
    return TranslationScheduler(orch, workers=1)


def test_long_sentence_returns_placeholder_immediately(orch_cn: Orchestrator) -> None:
    """Test 6 验收: 500+ 字符长句 → 立即返回原文占位, 后台完成回调携带中文。"""
    text = long_text("ancient dwarven halls")
    done: list[tuple[TranslationJob, object]] = []
    sched = TranslationScheduler(orch_cn, workers=1, max_attempts=3)
    sched.subscribe_done(lambda job, result: done.append((job, result)))
    start = time.monotonic()
    sub = sched.submit(text, priority=Priority.REALTIME)
    elapsed = time.monotonic() - start

    assert sub.is_async is True
    assert sub.result.text == text  # 原文占位
    assert sub.result.is_placeholder is True
    assert elapsed < 1.0  # 主流程不等待 LLM

    final = sub.wait(timeout=15)
    assert final.text == CN_REPLY_8
    assert final.text != text
    assert len(done) == 1
    job, result = done[0]
    assert result.text == CN_REPLY_8
    assert job.status == JobStatus.COMPLETED
    sched.shutdown()


def test_short_sentence_takes_sync_fast_path(orch: Orchestrator) -> None:
    """复杂度 ≤ 词典/规则阈值 → 同步快路径, 结果立即可用(不排队)。"""
    sched = TranslationScheduler(orch, workers=1)
    try:
        sub = sched.submit("Dwarf")
        assert sub.is_async is False
        assert sub.result.text == "矮人"
        sub2 = sched.submit("Urist cancels Make Wooden Barrel.")
        assert sub2.is_async is False
        assert sub2.result.text == "Urist 取消制作木桶。"
    finally:
        sched.shutdown()


def test_sync_miss_escalates_to_async(orch: Orchestrator) -> None:
    """短句快路径未命中(非词典/规则) → 不调 LLM, 升级为异步(默认 P3)。"""
    orch2 = Orchestrator(llm=RecorderLLM())
    sched = TranslationScheduler(orch2, workers=1)
    try:
        sub = sched.submit("A strange noise wakes you up.")
        assert sub.is_async is True
        final = sub.wait(timeout=15)
        assert final.text.startswith("中文译文。")  # 短句配短译文, 校验通过(非回退)
    finally:
        sched.shutdown()


def test_p0_always_before_p3_in_processing_order() -> None:
    """验收: P0 恒先于 P3 被处理(假 LLM 记录调用顺序)。"""
    rec = RecorderLLM()
    # 两个不同长句(同复杂度) → 顺序由优先级决定
    sched = TranslationScheduler(Orchestrator(llm=rec), workers=1, autostart=False)
    try:
        p3_text = long_text("dwarven halls")
        p0_text = long_text("deep mines")
        sched.submit(p3_text, priority=Priority.BACKGROUND)
        sub0 = sched.submit(p0_text, priority=Priority.REALTIME)
        sched.start()
        assert sub0.wait(timeout=15).text.startswith("中文译文。")
        time.sleep(0.3)  # 等 P3 也完成
        assert len(rec.calls) == 2
        assert rec.calls[0] == p0_text  # P0 先被 LLM 处理
        assert rec.calls[1] == p3_text
    finally:
        sched.shutdown()


def test_retry_three_times_then_failed_queue_not_deadlocked() -> None:
    """验收: LLM 持续失败 → 重试 3 次后 FAILED; 队列不死锁, 后续任务正常完成。"""
    rec = RecorderLLM(fail=True)
    sched = TranslationScheduler(Orchestrator(llm=rec), workers=1, max_attempts=3, autostart=False)
    failed_jobs: list[TranslationJob] = []
    sched.subscribe_failed(failed_jobs.append)
    text = long_text("cursed mines")
    try:
        sub = sched.submit(text, priority=Priority.BACKGROUND)
        sched.start()
        final = sub.wait(timeout=20)
        job = sub.handle.job
        assert job.status == JobStatus.FAILED
        assert job.attempt == 3  # 3 次尝试后耗尽
        assert job.error is not None
        assert failed_jobs == [job]
        assert final.text == text  # 回退原文
        assert final.error is not None

        # 队列不死锁: 修好 LLM 后, 新任务正常完成
        rec.fail = False
        sub2 = sched.submit(long_text("gold vaults"), priority=Priority.IMPORTANT)
        assert sub2.wait(timeout=15).text.startswith("中文译文。")
    finally:
        sched.shutdown()


def test_realtime_job_deadline_demotes_to_background(orch_cn: Orchestrator) -> None:
    """§29: realtime 任务等待超过 deadline → 降级 background(后续重试不抢实时队列)。"""
    sched = TranslationScheduler(orch_cn, workers=1, autostart=False)
    try:
        sub = sched.submit(
            long_text("echoing caverns"),
            priority=Priority.REALTIME,
            deadline_ms=50,
        )
        time.sleep(0.12)  # 让 deadline 在出队前过期
        assert sub.handle.ready is False
        sched.start()
        final = sub.wait(timeout=15)
        job = sub.handle.job
        assert job.was_demoted is True
        assert job.priority == Priority.BACKGROUND
        assert job.status == JobStatus.COMPLETED
        assert final.text == CN_REPLY_8  # 结果仍正常交付
    finally:
        sched.shutdown()


def test_queue_full_returns_fallback_without_blocking(orch: Orchestrator) -> None:
    """§27 max_queue 满员: 拒绝新任务 → 原文回退, 不阻塞主流程。"""
    rec = RecorderLLM()
    sched = TranslationScheduler(Orchestrator(llm=rec), workers=1, max_queue=1, autostart=False)
    try:
        sched.submit(long_text("first shaft"), priority=Priority.BACKGROUND)
        sub = sched.submit(long_text("second shaft"), priority=Priority.BACKGROUND)
        assert sub.is_async is False
        assert sub.result.text == long_text("second shaft")
        assert "queue full" in (sub.result.error or "")
        sched.start()
        time.sleep(0.5)  # 第一个任务正常完成, 队列不卡
        assert len(rec.calls) >= 1
    finally:
        sched.shutdown()


def test_empty_input_returns_sync_empty(orch: Orchestrator) -> None:
    sched = TranslationScheduler(orch, workers=1, autostart=False)
    try:
        sub = sched.submit("   ")
        assert sub.is_async is False
        assert sub.result.text == ""
    finally:
        sched.shutdown()


def test_workers_config_counts(orch: Orchestrator) -> None:
    """§26/§27: worker 数可配(默认 1, 不允许多路并发抬起)。"""
    sched = TranslationScheduler(orch, workers=1, autostart=True)
    try:
        assert len(sched._workers) == 1
    finally:
        sched.shutdown()