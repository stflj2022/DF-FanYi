"""翻译编排器 —— ticket-003 骨架(占位管线)。

真实管线(工程书 §11): normalize → cache → 词典 → 规则 → 本地 LLM → 验证,
由 ticket-004 实现。本骨架只固定两件事:

1. 接口形状: translate(text) -> TranslationResult(与 §10 Response 字段对齐);
2. 失败铁律: LLM 任何异常/空输出 → 回退原文 + confidence=0, 绝不抛异常
   (§2.3 翻译系统故障不导致游戏/调用方崩溃; §30 Fallback)。

LLM 通过构造参数注入 —— 测试用假 LLM(录制回放), 不依赖真实 ollama。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("df_fanyi.orchestrator")

LLMTranslator = Callable[[str], str]


@dataclass(frozen=True)
class TranslationResult:
    """单句翻译结果(与工程书 §10 Response 字段对齐)。

    text: 译文; 失败回退时 = 原文。
    confidence: 0.0-1.0; 骨架期未验证恒 0.0(ticket-005 起真实置信)。
    is_placeholder: 骨架期为 True; ticket-004 真管线后为 False。
    """

    text: str
    source_text: str
    model: str = "placeholder"
    provider: str = "placeholder"
    confidence: float = 0.0
    latency_ms: int = 0
    cache_hit: bool = False
    is_placeholder: bool = True
    error: str | None = None


def placeholder_translator(text: str) -> str:
    """骨架默认“假 LLM”: 返回占位译文, 零网络调用。

    保证无 ollama / 断网环境下 CLI 与测试完全确定性(工程书 Test 7 精神)。
    """
    return f"[占位译文] {text}"


class Orchestrator:
    """ticket-003 骨架编排器: 单句 translate, LLM 可注入, 永不抛异常。"""

    def __init__(
        self,
        llm: LLMTranslator | None = None,
        *,
        model_name: str = "placeholder",
    ) -> None:
        self._llm = llm or placeholder_translator
        self._model_name = model_name

    def translate(self, text: str) -> TranslationResult:
        start = time.perf_counter()
        text = (text or "").strip()
        if not text:
            return TranslationResult("", "", model=self._model_name, error="empty input")

        try:
            translated = self._llm(text)
        except Exception as exc:  # noqa: BLE001 — 铁律: 任何 LLM 故障都回退原文
            logger.warning("LLM 调用失败, 回退原文: %s", exc)
            return TranslationResult(
                text,
                text,
                model=self._model_name,
                latency_ms=_ms_since(start),
                error=str(exc),
            )
        if not translated or not translated.strip():
            logger.warning("LLM 返回空译文, 回退原文")
            return TranslationResult(
                text,
                text,
                model=self._model_name,
                latency_ms=_ms_since(start),
                error="empty translation",
            )
        return TranslationResult(
            translated.strip(),
            text,
            model=self._model_name,
            latency_ms=_ms_since(start),
        )


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
