"""翻译编排器 —— ticket-004/005 真管线(保护→缓存→词典→规则→本地LLM→验证→还原)。

工程书 §11 伪代码顺序 + ticket-005 §22-24:
    normalize → protect(markup/变量占位) → cache.lookup(hash) → terminology.lookup
    → rule_engine → LLM → validator → restore(还原占位符)
失败铁律 §2.3/§30: LLM 任何异常/空输出/验证不过 → 回退原文 + confidence=0,
绝不抛异常阻塞调用方; 失败结果不写缓存(§56 禁止无效缓存污染)。

保护器把 <color=red>/ {COUNT} 换成 MARKUP_001/VAR_001 占位符再进各阶段:
- 缓存 key 基于保护后文本(确定性变换, 同一原文始终同一 key);
- 词典/规则/LLM 只见安全文本(§21 注入防护: 游戏文本是不可信内容, 只翻译不执行);
- 验证器核对占位符守恒(§24), 不合格回退原文(原文自带完整标记/变量, 不丢信息);
- 成功译文在验证后还原占位符再返回/写缓存。

LLM 通过构造参数注入 —— 测试用录制回放假实现, 不依赖真实 ollama;
llm=None 表示无 LLM(离线), LLM 阶段直接回退原文(仍走词典/规则)。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from df_fanyi.core.cache import CacheEntry, LRUCache
from df_fanyi.core.parser import normalize_text, source_hash
from df_fanyi.core.protector import Protector
from df_fanyi.core.rules import RULE_THRESHOLD, RuleEngine
from df_fanyi.core.validator import Validator
from df_fanyi.database.terminology import Terminology

logger = logging.getLogger("df_fanyi.orchestrator")

LLMTranslator = Callable[[str], str]


@dataclass(frozen=True)
class TranslationResult:
    """单句翻译结果(与工程书 §10 Response 字段对齐)。

    text: 译文; 失败回退时 = 原文。
    confidence: 0.0-1.0; 词典=1.0, 规则=0.95, LLM=验证得分, 回退=0.0。
    """

    text: str
    source_text: str
    model: str = "unknown"
    provider: str = "local"
    confidence: float = 0.0
    latency_ms: int = 0
    cache_hit: bool = False
    is_placeholder: bool = False
    error: str | None = None


class Orchestrator:
    """翻译管线编排器(§11): 单句 translate, LLM 可注入, 永不抛异常。"""

    def __init__(
        self,
        llm: LLMTranslator | None = None,
        *,
        terminology: Terminology | None = None,
        rules: RuleEngine | None = None,
        validator: Validator | None = None,
        cache: LRUCache | None = None,
        protector: Protector | None = None,
        model_name: str = "gemma-4b-trans",
        provider: str = "ollama",
        rule_threshold: float = RULE_THRESHOLD,
        cache_capacity: int = 512,
    ) -> None:
        self._llm = llm
        self._terminology = terminology or Terminology.load_seed()
        self._rules = rules or RuleEngine(self._terminology, threshold=rule_threshold)
        self._validator = validator or Validator()
        self._protector = protector or Protector()
        self._cache = cache or LRUCache(cache_capacity)
        self._model_name = model_name
        self._provider = provider

    def translate(self, text: str, *, context: dict[str, Any] | None = None) -> TranslationResult:
        start = time.perf_counter()
        normalized = normalize_text(text)
        if not normalized:
            return TranslationResult(
                "", "", model=self._model_name, provider=self._provider, error="empty input"
            )

        # §22/§23: 先保护 markup/变量为占位符, 后续各阶段只见安全文本(§21)
        protected, restore_map = self._protector.protect(normalized)
        digest = source_hash(protected, context)  # §37: hash 含相关上下文

        # L1 缓存(§9)
        entry = self._cache.get(digest)
        if entry is not None:
            return TranslationResult(
                entry.text,
                entry.source_text,
                model=entry.model,
                provider=entry.provider,
                confidence=entry.confidence,
                latency_ms=_ms_since(start),
                cache_hit=True,
            )

        # 术语词典: 精确匹配直返(§11)
        term = self._terminology.lookup(protected)
        if term is not None:
            return self._finalize(
                TranslationResult(
                    term.target,
                    normalized,
                    model="dictionary",
                    provider="local",
                    confidence=1.0,
                    latency_ms=_ms_since(start),
                ),
                digest,
                normalized,
            )

        # 规则引擎: 模板命中且置信度 ≥ 阈值才接受(§11)
        rule = self._rules.translate(protected)
        if rule is not None:
            return self._finalize(
                TranslationResult(
                    rule.text,
                    normalized,
                    model="rule",
                    provider="local",
                    confidence=rule.confidence,
                    latency_ms=_ms_since(start),
                ),
                digest,
                normalized,
            )

        # 本地 LLM(§14/§15 E4B); 无 LLM 配置 → 离线回退
        if self._llm is None:
            logger.info("无 LLM 配置, 回退原文")
            return TranslationResult(
                normalized,
                normalized,
                model=self._model_name,
                provider=self._provider,
                latency_ms=_ms_since(start),
                error="no LLM configured",
            )
        try:
            translated = self._llm(protected)
        except Exception as exc:  # noqa: BLE001 — 铁律: 任何 LLM 故障都回退原文
            logger.warning("LLM 调用失败, 回退原文: %s", exc)
            return TranslationResult(
                normalized,
                normalized,
                model=self._model_name,
                provider=self._provider,
                latency_ms=_ms_since(start),
                error=str(exc),
            )
        if not translated or not translated.strip():
            logger.warning("LLM 返回空译文, 回退原文")
            return TranslationResult(
                normalized,
                normalized,
                model=self._model_name,
                provider=self._provider,
                latency_ms=_ms_since(start),
                error="empty translation",
            )

        # 验证(§24): 占位符/数字/行数守恒不过 → 回退原文(原文自带完整标记/变量)
        validation = self._validator.validate(protected, translated)
        if not validation.valid:
            logger.warning("验证不过, 回退原文: %s", validation.errors)
            return TranslationResult(
                normalized,
                normalized,
                model=self._model_name,
                provider=self._provider,
                latency_ms=_ms_since(start),
                error=f"验证失败: {'; '.join(validation.errors)}",
            )

        # 还原占位符(§22/§23: 验证通过才还原, 还原永不丢信息)
        restored = self._protector.restore(translated.strip(), restore_map)
        return self._finalize(
            TranslationResult(
                restored,
                normalized,
                model=self._model_name,
                provider=self._provider,
                confidence=validation.score,
                latency_ms=_ms_since(start),
            ),
            digest,
            normalized,
        )

    def _finalize(
        self,
        result: TranslationResult,
        digest: str,
        normalized: str,
    ) -> TranslationResult:
        """成功结果写入 L1 缓存(失败回退不写, 见 §56/§9)。"""
        self._cache.put(
            digest,
            CacheEntry(
                source_text=normalized,
                text=result.text,
                model=result.model,
                provider=result.provider,
                confidence=result.confidence,
            ),
        )
        return result


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
