"""翻译编排器 —— ticket-004/005 真管线 + ticket-006 L2 SQLite 持久层。

管线: 保护→缓存(L1 内存 → L2 SQLite)→词典→规则→本地LLM→验证→还原。

L2 持久层(§9/§8.2, ticket-006):
- L1 未命中 → L2 查询; 命中 cache_hit=true 且绝不再调 LLM(验收: 假 LLM 计数为证);
- 成功译文写回 L2(含 model/provider/confidence, usage_count 首写=1);
- L1 命中同样写穿计数(tm_bump_usage) → §38 高频提升依据;
- usage_count>10 的高频文本启动时 warm_l1 预热进 L1(§38 常驻);
- 失败回退不写 L2(§56 禁止无效缓存污染);
- store.term_lookup 提供数据库术语(term add 后立即生效, §8.1 与内存 seed 合并查询)。

工程书 §11 伪代码顺序 + ticket-005 §22-24:

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
from typing import TYPE_CHECKING, Any, Callable

from df_fanyi.core.cache import CacheEntry, LRUCache
from df_fanyi.core.parser import normalize_text, source_hash
from df_fanyi.core.protector import Protector
from df_fanyi.core.rules import RULE_THRESHOLD, RuleEngine
from df_fanyi.core.validator import Validator
from df_fanyi.database.terminology import Terminology

if TYPE_CHECKING:
    from df_fanyi.database.store import SQLiteStore

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


# 2026-09-12 全中文化质量门: 检测译文中残留的英文功能词
# 常见功能词清单(都是英文中除专有名词外必译的小词)
_ENGLISH_FUNCTION_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'but', 'nor', 'yet', 'so',
    'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did',
    'will', 'would', 'shall', 'should', 'can', 'could', 'may', 'might', 'must',
    'of', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'from', 'as', 'into',
    'through', 'during', 'before', 'after', 'above', 'below', 'between',
    'that', 'which', 'who', 'whom', 'whose', 'what', 'where', 'when', 'why', 'how',
    'this', 'these', 'those', 'there', 'here',
    'i', 'you', 'he', 'she', 'it', 'we', 'they',
    'my', 'your', 'his', 'her', 'its', 'our', 'their',
    'me', 'him', 'them', 'us',
    'if', 'then', 'because', 'since', 'while', 'although', 'though',
    'very', 'much', 'many', 'more', 'most', 'some', 'any', 'all', 'no', 'not',
    'just', 'only', 'also', 'too', 'even',
})
# 阈值: 译文里残留这么多英文功能词 → 判定为“中英混杂”, 回退原文
_MAX_ENGLISH_RESIDUE = 2


def _count_english_function_words(text: str) -> int:
    """统计译文中保留的英文功能词个数。跳过专有名词检测。

    实现: 按空格/标点分词, 检查词是否在 _ENGLISH_FUNCTION_WORDS 里。
    变量占位符 {COUNT} 会被分词器看作 {COUNT}, 不计入。
    """
    import re as _re
    # 去掉变量占位符 (避免误判 {ITEM} 等为英文)
    cleaned = _re.sub(r'\{[^}]*\}', ' ', text)
    # 提取所有英文单词 (a-zA-Z)
    words = _re.findall(r'[A-Za-z]+', cleaned.lower())
    return sum(1 for w in words if w in _ENGLISH_FUNCTION_WORDS)


# 2026-09-12 中文吉卜赛检测: LLM 输出无语义中文(如 "沉默翻译这我觉得手腕")
# 原则: 严谨只检测几乎不会出现的中文模式, 避免误杀正常译文
# 启发: 检测“虚词连续堆叠” >= 4 个, 这是最不易误判的标志
_CHINESE_FILLER_CHARS = frozenset({
    '的', '了', '是', '我', '这', '那', '你', '他', '她', '它',
    '们', '在', '有', '和', '与', '或', '而', '但', '为', '因',
    '于', '上', '下', '中', '外', '里', '也', '都', '要', '把',
    '会', '给', '让', '从', '向', '对', '之', '以', '被', '着',
    '到', '去', '来', '又', '只', '还', '并', '且', '或', '但',
    '就', '可', '能', '将', '该', '并', '且', '或', '但',
})


def _looks_like_chinese_gibberish(text: str, min_chars: int = 8) -> bool:
    """检测中文吉卜赛。无意义中文 → True。

    仅采用最严谨启发: 连续 5+ 虚词 (如 "的了一的是")。
    阈值 4 误判率高("会在这里做工" 会连中), 5 才能保持准确。
    """
    import re as _re
    cjk = _re.findall(r'[\u4e00-\u9fff]', text)
    if len(cjk) < min_chars:
        return False
    longest_run = 0
    current_run = 0
    for c in cjk:
        if c in _CHINESE_FILLER_CHARS:
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    return longest_run >= 5


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
        store: "SQLiteStore | None" = None,  # ticket-006: L2 SQLite 持久层(可空)
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
        self._store = store
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

        # 快路径(缓存/词典/规则, 不调 LLM): 命中即返回; 未命中 → LLM 阶段
        fast = self._fast_result(normalized, protected, digest, start, context)
        if fast is not None:
            return fast

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

        # 2026-09-12 硬性:全中文化质量门 (用户指令 "都翻译, 不要出现外文")
        # 1) 英文功能词残留检测: 检输出中残留的英文功能词 (are/is/the/a/of/in/which/you 等)
        english_residue = _count_english_function_words(translated)
        if english_residue > _MAX_ENGLISH_RESIDUE:
            logger.warning(
                "译文残留 %d 个英文功能词 (门限 %d), 回退原文: %s",
                english_residue, _MAX_ENGLISH_RESIDUE,
                translated[:80],
            )
            return TranslationResult(
                normalized,
                normalized,
                model=self._model_name,
                provider=self._provider,
                latency_ms=_ms_since(start),
                error=f"quality_english_leak: {english_residue} function words",
            )

        # 2) 中文吉卜赛检测: LLM 有时输出无意义中文(如 "沉默翻译这我觉得手腕黑下压键怎么办?")
        #    启发: 纯中文里出现三个连续“的/了/我/是/这”但词性完全不配合, 判定为幻觉。
        if _looks_like_chinese_gibberish(translated):
            logger.warning(
                "译文为中文吉卜赛, 回退原文: %s", translated[:80],
            )
            return TranslationResult(
                normalized,
                normalized,
                model=self._model_name,
                provider=self._provider,
                latency_ms=_ms_since(start),
                error="quality_chinese_gibberish",
            )

        # 3) 长度守卫: 仅拒过长, 过短允许 (LLM 可能主动省略冗余描述)
        #    中文信息密度 ≈ 英文 1.5–2.0 倍; 上限 3.0 涵盉 LLM 失控重复
        src_len = len(normalized)
        tgt_len = len(translated)
        if src_len > 0 and tgt_len > 0:
            ratio = tgt_len / src_len
            if ratio > 3.0:
                logger.warning(
                    "译文过长比异常 %.2f (src=%d tgt=%d), 回退原文: %s",
                    ratio, src_len, tgt_len, translated[:80],
                )
                return TranslationResult(
                    normalized,
                    normalized,
                    model=self._model_name,
                    provider=self._provider,
                    latency_ms=_ms_since(start),
                    error=f"quality_length_ratio: {ratio:.2f}",
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
            context_hash=_context_hash(context),
        )

    def fast_translate(
        self, text: str, *, context: dict[str, Any] | None = None
    ) -> "TranslationResult | None":
        """§34 快路径(缓存 L1+L2 → 词典 → 规则), 绝不调 LLM。

        ticket-007 调度器路由用: 命中 → 同步返回可立即使用的结果;
        未命中 → None(由调度器决定入队异步, 主线程不等待 LLM)。
        与后台 worker 并发安全(共享组件: LRU/持久层均带锁, 词典/规则只读)。
        """
        start = time.perf_counter()
        normalized = normalize_text(text)
        if not normalized:
            return None
        protected, _restore = self._protector.protect(normalized)
        digest = source_hash(protected, context)
        return self._fast_result(normalized, protected, digest, start, context)

    def _fast_result(
        self,
        normalized: str,
        protected: str,
        digest: str,
        start: float,
        context: dict[str, Any] | None,
    ) -> "TranslationResult | None":
        """快路径各阶段(L1→L2→词典→规则): 命中返回结果, 未命中 None。

        translate() 与 fast_translate() 共用 —— 保证两条路径对缓存/词典/规则的
        判定完全一致; translate() 在 None 时继续下探 LLM。
        """
        # L1 缓存(§9) —— 命中: 写穿计数(高频提升依据 §38), 不调 LLM
        entry = self._cache.get(digest)
        if entry is not None:
            if self._store is not None:
                self._store.tm_bump_usage(digest)
            return TranslationResult(
                entry.text,
                entry.source_text,
                model=entry.model,
                provider=entry.provider,
                confidence=entry.confidence,
                latency_ms=_ms_since(start),
                cache_hit=True,
            )

        # L2 SQLite 持久缓存(§9, ticket-006): L1 miss → L2 查询
        if self._store is not None:
            tm_row = self._store.tm_lookup(digest)
            if tm_row is not None:
                self._store.tm_bump_usage(digest)  # §38 命中计数
                self._cache.put(
                    digest,
                    CacheEntry(
                        source_text=tm_row["source_text"],
                        text=tm_row["translated_text"],
                        model=tm_row.get("model") or self._model_name,
                        provider=tm_row.get("provider") or self._provider,
                        confidence=float(tm_row.get("confidence") or 0.0),
                    ),
                )
                return TranslationResult(
                    tm_row["translated_text"],
                    tm_row["source_text"],
                    model=tm_row.get("model") or self._model_name,
                    provider=tm_row.get("provider") or self._provider,
                    confidence=float(tm_row.get("confidence") or 0.0),
                    latency_ms=_ms_since(start),
                    cache_hit=True,
                )

        # 术语词典: 内存 seed 精确匹配直返(§11); miss 时下探数据库术语(§8.1)
        term = self._terminology.lookup(protected)
        if term is None and self._store is not None:
            tm_term = self._store.term_lookup(protected)
        else:
            tm_term = None
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
                context_hash=_context_hash(context),
            )
        if tm_term is not None:
            return self._finalize(
                TranslationResult(
                    tm_term["target"],
                    normalized,
                    model="dictionary",
                    provider="local",
                    confidence=1.0,  # 术语精确命中 = 高置信(§34 ≈0.1 档)
                    latency_ms=_ms_since(start),
                ),
                digest,
                normalized,
                context_hash=_context_hash(context),
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
                context_hash=_context_hash(context),
            )

        # 快路径未能解决 → None(translate() 下探 LLM, fast_translate() 由调度器决定入队)
        return None

    def warm_l1(self, threshold: int = 10) -> int:
        """§38 高频提升: usage_count > threshold 的译文预热进 L1(常驻), 启动时调用。

        返回预热条数。预热后这些高频句首次翻译直接 L1 命中(不落 L2/不调 LLM)。
        """
        if self._store is None:
            return 0
        warmed = 0
        for row in self._store.tm_frequent(threshold=threshold):
            self._cache.put(
                row["source_hash"],
                CacheEntry(
                    source_text=row["source_text"],
                    text=row["translated_text"],
                    model=row.get("model") or self._model_name,
                    provider=row.get("provider") or self._provider,
                    confidence=float(row.get("confidence") or 0.0),
                ),
            )
            warmed += 1
        if warmed:
            logger.info("§38 高频文本预热 L1: %d 条", warmed)
        return warmed

    def _finalize(
        self,
        result: TranslationResult,
        digest: str,
        normalized: str,
        *,
        context_hash: str | None = None,
    ) -> TranslationResult:
        """成功结果写入 L1(+L2 SQLite)缓存; 失败回退不写(§56/§9)。"""
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
        if self._store is not None:
            # §8.2/§38: 译文写回(含 model/provider/confidence, 首写 usage_count=1)
            self._store.tm_upsert(
                digest,
                normalized,
                result.text,
                context_hash=context_hash,
                model=result.model,
                provider=result.provider,
                confidence=result.confidence,
            )
        return result


def _context_hash(context: dict[str, Any] | None) -> str | None:
    """上下文的独立哈希(§8.2 context_hash 列, 记录用; 主 key 仍是含上下文的 source_hash)。"""
    if not context:
        return None
    return source_hash("", context)


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
