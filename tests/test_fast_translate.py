"""ticket-007: 编排器快路径 fast_translate(§34, 绝不调 LLM)。

调度器同步快路径的判定基础: L1/L2 缓存 → 词典 → 规则; 未命中 → None(入队异步)。
与后台 worker 并发安全(共享组件线程安全)。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.orchestrator import Orchestrator  # noqa: E402


class Recorder:
    def __init__(self, reply: str = "预设译文") -> None:
        self.calls: list[str] = []
        self.reply = reply

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        return self.reply


def test_dict_hit_returns_result_no_llm() -> None:
    orch = Orchestrator(llm=Recorder())
    r = orch.fast_translate("Dwarf")
    assert r is not None
    assert r.text == "矮人"
    assert r.model == "dictionary"


def test_rule_hit_returns_result_no_llm() -> None:
    orch = Orchestrator(llm=Recorder())
    r = orch.fast_translate("Urist cancels Make Wooden Barrel.")
    assert r is not None
    assert r.text == "Urist 取消制作木桶。"
    assert r.model == "rule"


def test_miss_returns_none_and_never_calls_llm() -> None:
    rec = Recorder()
    orch = Orchestrator(llm=rec)
    assert orch.fast_translate("The ancient dwarven halls echo with the sound of pickaxes.") is None
    assert rec.calls == []


def test_empty_and_blank_return_none() -> None:
    orch = Orchestrator(llm=Recorder())
    assert orch.fast_translate("") is None
    assert orch.fast_translate("   ") is None


def test_cache_hit_served_without_llm() -> None:
    """已缓存译文走 L1, 快路径直接命中(§9 两级缓存)。"""
    rec = Recorder()
    orch = Orchestrator(llm=rec)
    first = orch.translate("A strange noise wakes you up.")
    assert first.error is None
    second = orch.fast_translate("A strange noise wakes you up.")
    assert second is not None
    assert second.text == first.text
    assert rec.calls == ["A strange noise wakes you up."]  # 第二次不再调 LLM


def test_fast_path_does_not_write_failed_results() -> None:
    """miss 不写任何缓存(§56 禁止无效缓存污染)。"""
    rec = Recorder()
    orch = Orchestrator(llm=rec)
    text = "The ancient dwarven halls echo with the sound of pickaxes."
    assert orch.fast_translate(text) is None
    # 后续 translate 仍走全管线(未被污染的缓存干扰)
    r = orch.translate(text)
    assert r.text == "预设译文"