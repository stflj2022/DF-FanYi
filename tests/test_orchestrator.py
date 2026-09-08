"""ticket-003 验收: 编排器骨架。

工程书依据:
- §10 Translation API 字段对齐(text/source/model/confidence/latency/cache_hit)
- §2.3/§30 失败铁律: LLM 任何异常 → 回退原文 + confidence=0, 绝不抛异常阻塞
- 假 LLM 录制回放: 测试不依赖真实 ollama(工程书 Testing Decisions)

只测外部行为(translate 返回值), 不 mock 内部实现。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.orchestrator import (  # noqa: E402
    Orchestrator,
    TranslationResult,
)


class _Boom(Exception):
    pass


def test_placeholder_translator_marks_placeholder() -> None:
    result = Orchestrator().translate("Dwarf")
    assert isinstance(result, TranslationResult)
    assert "Dwarf" in result.text
    assert result.is_placeholder is True
    assert result.error is None


def test_fake_llm_output_is_returned() -> None:
    orch = Orchestrator(llm=lambda text: "矮人", model_name="fake")
    result = orch.translate("Dwarf")
    assert result.text == "矮人"
    assert result.source_text == "Dwarf"
    assert result.model == "fake"


def test_result_has_spec_contract_fields() -> None:
    """工程书 §10 Response 字段。"""
    result = Orchestrator().translate("Dwarf")
    assert hasattr(result, "text")
    assert hasattr(result, "source_text")
    assert hasattr(result, "model")
    assert hasattr(result, "provider")
    assert hasattr(result, "confidence")
    assert hasattr(result, "latency_ms")
    assert hasattr(result, "cache_hit")


def test_llm_failure_falls_back_to_original_text() -> None:
    def bad(text: str) -> str:
        raise _Boom("llm down")

    result = Orchestrator(llm=bad).translate("Urist cancels.")
    assert result.text == "Urist cancels."
    assert result.confidence == 0.0
    assert result.error is not None
    assert "llm down" in result.error


def test_empty_translation_falls_back_to_original() -> None:
    orch = Orchestrator(llm=lambda text: "   ")
    result = orch.translate("Dwarf")
    assert result.text == "Dwarf"
    assert result.confidence == 0.0
    assert result.error is not None


def test_empty_input_returns_empty_result() -> None:
    result = Orchestrator().translate("   ")
    assert result.text == ""
    assert result.error is not None


def test_orchestrator_never_raises() -> None:
    """铁律 §2.3: 翻译系统故障不得导致调用方崩溃。"""
    def weird(text: str) -> str:
        raise RuntimeError("any error")

    result = Orchestrator(llm=weird).translate("text")
    assert result.text == "text"
    # 空 LLM 输出也不抛
    result2 = Orchestrator(llm=lambda t: None).translate("text")  # type: ignore[return-value]
    assert result2.text == "text"


@pytest.mark.parametrize("text", ["Dwarf", "Urist cancels Make Wooden Barrel."])
def test_placeholder_is_deterministic(text: str) -> None:
    orch = Orchestrator()
    assert orch.translate(text).text == orch.translate(text).text
