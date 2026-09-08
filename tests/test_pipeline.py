"""ticket-004: 完整编排管线(缓存→词典→规则→本地LLM→验证)。

工程书 §11 伪代码顺序; LLM 用录制回放假实现(不依赖真实 ollama);
失败铁律 §2.3/§30: 任何阶段失败 → 回退原文 + confidence=0, 绝不抛异常。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.cache import LRUCache  # noqa: E402
from df_fanyi.core.orchestrator import Orchestrator  # noqa: E402


class Recorder:
    """录制回放假 LLM: 记录调用, 返回预设译文或抛异常。"""

    def __init__(self, reply: str | None = "预设译文", exc: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.reply = reply
        self.exc = exc

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return self.reply or ""


@pytest.fixture
def orch() -> Orchestrator:
    return Orchestrator()


# --- 词典层(Test 1/2) -----------------------------------------------------

def test_dictionary_hit_dwarf(orch: Orchestrator) -> None:
    r = orch.translate("Dwarf")
    assert r.text == "矮人"
    assert r.model == "dictionary"
    assert r.confidence == 1.0
    assert r.is_placeholder is False


def test_dictionary_hit_wooden_barrel(orch: Orchestrator) -> None:
    r = orch.translate("wooden barrel")
    assert r.text == "木桶"


def test_dictionary_hit_does_not_call_llm(orch: Orchestrator) -> None:
    rec = Recorder()
    orch = Orchestrator(llm=rec)
    orch.translate("Dwarf")
    assert rec.calls == []


# --- 规则层(Test 3) --------------------------------------------------------

def test_rule_hit_cancels_template(orch: Orchestrator) -> None:
    r = orch.translate("Urist cancels Make Wooden Barrel.")
    assert r.text == "Urist 取消制作木桶。"
    assert r.model == "rule"
    assert r.confidence >= 0.9
    assert r.error is None


# --- LLM 层 -----------------------------------------------------------------

def test_llm_used_for_unknown_text() -> None:
    rec = Recorder(reply="流浪狗生了一窝小狗。")
    orch = Orchestrator(llm=rec)
    r = orch.translate("Stray dog has given birth to puppies.")
    assert rec.calls == ["Stray dog has given birth to puppies."]
    assert r.text == "流浪狗生了一窝小狗。"
    assert r.model == "gemma-4b-trans"
    assert r.provider == "ollama"
    assert r.confidence == 1.0
    assert r.is_placeholder is False


def test_llm_output_passes_validator_with_numbers() -> None:
    rec = Recorder(reply="2 名矮人抵达。")
    r = Orchestrator(llm=rec).translate("2 dwarves arrived.")
    assert r.text == "2 名矮人抵达。"


def test_llm_output_missing_number_falls_back() -> None:
    """验证不过(数字丢失) → 回退原文 + confidence=0。"""
    rec = Recorder(reply="矮人")
    r = Orchestrator(llm=rec).translate("12 dwarves arrived.")
    assert r.text == "12 dwarves arrived."
    assert r.confidence == 0.0
    assert r.error is not None
    assert "验证" in r.error


def test_llm_output_too_long_falls_back() -> None:
    rec = Recorder(reply="矮人" * 20)
    r = Orchestrator(llm=rec).translate("Stray dog has given birth to puppies.")
    assert r.text == "Stray dog has given birth to puppies."
    assert r.confidence == 0.0


def test_llm_failure_falls_back() -> None:
    rec = Recorder(exc=RuntimeError("ollama down"))
    r = Orchestrator(llm=rec).translate("Stray dog has given birth to puppies.")
    assert r.text == "Stray dog has given birth to puppies."
    assert r.confidence == 0.0
    assert "ollama down" in (r.error or "")


def test_no_llm_configured_falls_back_offline() -> None:
    """无 LLM 配置(离线) → 非词典/规则文本回退原文, 不抛异常。"""
    r = Orchestrator(llm=None).translate("Stray dog has given birth to puppies.")
    assert r.text == "Stray dog has given birth to puppies."
    assert r.confidence == 0.0
    assert r.error is not None


# --- 缓存层 -----------------------------------------------------------------

def test_second_call_hits_cache_and_skips_llm() -> None:
    rec = Recorder(reply="流浪狗生了一窝小狗。")
    orch = Orchestrator(llm=rec)
    first = orch.translate("Stray dog has given birth to puppies.")
    assert first.cache_hit is False
    second = orch.translate("Stray dog has given birth to puppies.")
    assert second.cache_hit is True
    assert len(rec.calls) == 1, "缓存命中不应再次调用 LLM(§56 禁止每次重复调用 LLM)"


def test_cache_respects_context_keying() -> None:
    """§37: 相同文本不同上下文 → 不共享缓存。"""
    rec = Recorder(reply="流浪狗生了一窝小狗。")
    orch = Orchestrator(llm=rec)
    ctx_a = {"screen": "announcement", "character": "A"}
    ctx_b = {"screen": "announcement", "character": "B"}
    orch.translate("Stray dog has given birth to puppies.", context=ctx_a)
    orch.translate("Stray dog has given birth to puppies.", context=ctx_b)
    assert len(rec.calls) == 2
    # 相同上下文 → 命中
    orch.translate("Stray dog has given birth to puppies.", context=ctx_a)
    assert len(rec.calls) == 2


def test_cache_capacity_respected() -> None:
    rec = Recorder(reply="译文")
    orch = Orchestrator(llm=rec, cache_capacity=1)
    orch.translate("sentence one")
    orch.translate("sentence two")
    orch.translate("sentence one")
    assert len(rec.calls) == 3, "容量 1 时第一条应已被淘汰"


def test_failed_results_are_not_cached() -> None:
    """失败回退不写缓存: 重试仍走完整管线。"""
    rec = Recorder(exc=RuntimeError("down"))
    orch = Orchestrator(llm=rec)
    orch.translate("Stray dog has given birth to puppies.")
    orch.translate("Stray dog has given birth to puppies.")
    assert len(rec.calls) == 2


def test_dictionary_and_rule_results_are_cached() -> None:
    orch = Orchestrator()
    assert orch.translate("Dwarf").cache_hit is False
    assert orch.translate("Dwarf").cache_hit is True
    assert orch.translate("Urist cancels Make Wooden Barrel.").cache_hit is False
    assert orch.translate("Urist cancels Make Wooden Barrel.").cache_hit is True


# --- 健壮性 -----------------------------------------------------------------

def test_orchestrator_never_raises() -> None:
    """铁律 §2.3: 任何输入/故障不得让调用方崩溃。"""
    orch = Orchestrator()
    for text in ("", "   ", "Dwarf", "Urist cancels Make Wooden Barrel."):
        r = orch.translate(text)
        assert r is not None


def test_empty_input_returns_empty_result() -> None:
    r = Orchestrator().translate("   ")
    assert r.text == ""
    assert r.error is not None


def test_long_text_still_translates_without_llm() -> None:
    """词典 + 规则路径对 >500 字符文本不崩溃(LLM 异步是 ticket-007 的事)。"""
    long_text = ("Urist cancels Make Wooden Barrel. " * 40).strip()
    r = Orchestrator().translate(long_text)
    assert r.text == long_text  # 无模板命中 → 无 LLM → 回退原文
    assert r.confidence == 0.0
