"""ticket-007 验收: 集成 —— 提交 50 句混合复杂度。

快路径全同步返回(词典/规则), 慢路径均异步完成且主流程不等待。
回调总数 = 异步任务数, 每个回调携带中文译文。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.orchestrator import Orchestrator  # noqa: E402
from df_fanyi.core.queue import TranslationScheduler  # noqa: E402

CN_REPLY_10 = "古老的矮人厅中回荡着镐子敲击石头的声音。" * 10  # info≈190


class RecorderLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        return CN_REPLY_10


ADJECTIVES = [
    "ancient", "mossy", "echoing", "forgotten", "gleaming",
    "haunted", "gilded", "sunless", "rickety", "crystal",
    "dwarven", "smithy", "barren", "silent", "dusty",
    "sacred", "tangled", "frozen", "fiery", "misty",
    "jagged", "ceramic", "iron", "bronze", "velvet",
    "carven", "sunless", "many", "dark", "bright",
    "old", "new", "high", "low", "wide",
    "deep", "short", "long", "cold", "warm",
]


def _slow_text(index: int) -> str:
    """40 个互不相同且长度 >500 字符的长句(避免 L1 缓存去重干扰 LLM 计数)。"""
    adj = ADJECTIVES[index]
    noun = "mines" if index % 2 == 0 else "halls"
    seed = ("The %s %s echo with the sound of pickaxes striking against ancient stone. " % (adj, noun))
    return (seed * 8).strip()


FAST_CORPUS = [
    "Dwarf",                      # 词典
    "wooden barrel",              # 词典
    "Urist cancels Make Wooden Barrel.",   # 规则
    "Urist has become a carpenter.",       # 规则
    "Urist is now a mason.",              # 规则
    "Urist has died.",                    # 规则
    "Urist McDwarf cancels Make Wooden Barrel.",  # 规则(带姓)
    "Urist cancels Build Wooden Barrel.",         # 规则(建造)
    "Helga has become a carpenter.",             # 规则
    "Borin cancels Make Wooden Barrel.",         # 规则
]


def test_fifty_sentences_mixed_complexity() -> None:
    rec = RecorderLLM()
    done: list[object] = []
    sched = TranslationScheduler(Orchestrator(llm=rec), workers=1, max_attempts=3, max_queue=64)
    sched.subscribe_done(lambda job, result: done.append(result))

    try:
        # 慢路径 40 句(长文本, 全部异步)
        async_subs = []
        for i in range(40):
            sub = sched.submit(_slow_text(i), priority=3)  # P3 background
            assert sub.is_async is True, f"长句 #{i} 应走异步"
            assert sub.result.is_placeholder is True
            async_subs.append(sub)

        # 快路径 10 句(词典/规则, 全同步)
        for phrase in FAST_CORPUS:
            sub = sched.submit(phrase)
            assert sub.is_async is False, f"快路径句应同步返回: {phrase!r}"
            assert sub.result.text != phrase, f"快路径必须产生译文: {phrase!r}"

        # 慢路径全部完成(不等待具体某句, 主流程正常轮询)
        start = time.monotonic()
        results = [sub.wait(timeout=120) for sub in async_subs]
        elapsed = time.monotonic() - start

        assert all(r.text == CN_REPLY_10 for r in results)
        assert len(done) == 40
        assert all(getattr(r, "text", None) == CN_REPLY_10 for r in done)
        # 慢路径每条实际调过一次 LLM(40 句互不相同 → 40 次); 快路径 0 次
        expected = {_slow_text(i) for i in range(40)}
        assert len([c for c in rec.calls if c in expected]) == 40
        assert elapsed < 120
    finally:
        sched.shutdown()