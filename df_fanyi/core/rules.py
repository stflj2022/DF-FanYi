"""规则引擎(ticket-004): 模板句 → 中文模板, 置信度阈值 0.9。

工程书 §11: rule_result.confidence >= RULE_THRESHOLD 才接受(否则继续下探 LLM);
§34 复杂度档位: 规则 ≈0.25, 模板命中高置信(0.95)。
§2.2 REUSE_PLAN: 不直接引入 dfi18n/dfzh 的 TOML 规则集, 只参考其格式
(`"{name} cancels {job}." → 中文模板`), 本引擎自建少量高价值 DF 公告模板。

模板捕获的 job/profession 短语经两级翻译:
1. 首词动词映射(make/build/mine/... 规则内建);
2. 剩余英文短语走术语词典最长匹配短语替换(如 "Wooden Barrel"→木桶)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Pattern

from df_fanyi.database.terminology import Terminology

# 工单: 模板句置信度阈值 0.9(高于才接受; 阈值可由构造参数覆盖以便实测调优, §34)
RULE_THRESHOLD = 0.9

# 模板命中置信度
_TEMPLATE_CONFIDENCE = 0.95

# job 首词动词映射(DF 任务动词 → 中文)
_JOB_VERBS: dict[str, str] = {
    "make": "制作",
    "craft": "制作",
    "build": "建造",
    "construct": "建造",
    "mine": "开采",
    "dig": "挖掘",
    "fell": "砍伐",
    "chop": "砍伐",
    "plant": "种植",
    "gather": "采集",
    "harvest": "收获",
    "cook": "烹饪",
    "brew": "酿造",
    "butcher": "屠宰",
    "tan": "鞣制",
    "spin": "纺纱",
    "weave": "编织",
    "sew": "缝纫",
    "smelt": "冶炼",
    "forge": "锻造",
    "smith": "锻造",
    "engrave": "雕刻",
    "cut": "切割",
    "prepare": "准备",
    "haul": "搬运",
    "clean": "清洁",
    "train": "训练",
    "hunt": "狩猎",
    "fish": "钓鱼",
    "milk": "挤奶",
    "shear": "剪毛",
    "slaughter": "屠宰",
    "leather": "鞣制",
    "process": "加工",
    "dye": "染色",
    "pave": "铺路",
    "study": "研习",
    "pray": "祈祷",
    "sleep": "睡觉",
    "eat": "进食",
    "drink": "饮水",
}

_NAME = r"[A-Z][A-Za-z' -]+"  # DF 人名(大写字头, 含空格与连字符)
_WORD = r"[A-Za-z' -]+"
_SENTENCE = r"[^.]+"


@dataclass(frozen=True)
class RuleResult:
    text: str
    confidence: float


class RuleEngine:
    """DF 公告模板规则引擎: translate() 未命中返回 None(下探 LLM)。"""

    def __init__(self, terminology: Terminology, threshold: float = RULE_THRESHOLD) -> None:
        self.terminology = terminology
        self.threshold = threshold
        self._templates: list[tuple[Pattern[str], Callable[[re.Match[str]], RuleResult]]] = [
            (
                re.compile(rf"^(?P<name>{_NAME}) cancels (?P<job>{_SENTENCE})\.$"),
                self._apply_cancels,
            ),
            (
                re.compile(rf"^(?P<name>{_NAME}) has become a (?P<profession>{_WORD})\.$"),
                self._apply_become,
            ),
            (
                re.compile(rf"^(?P<name>{_NAME}) is now a (?P<profession>{_WORD})\.$"),
                self._apply_is_now,
            ),
            (
                re.compile(rf"^(?P<name>{_NAME}) has died\.$"),
                self._apply_died,
            ),
        ]

    def translate(self, text: str) -> RuleResult | None:
        """模板匹配 → 中文模板; 置信度 < 阈值或不匹配 → None。"""
        if not text:
            return None
        for pattern, apply in self._templates:
            match = pattern.match(text)
            if match:
                result = apply(match)
                if result.confidence >= self.threshold:
                    return result
        return None

    # --- 模板应用 -----------------------------------------------------------

    def _apply_cancels(self, m: re.Match[str]) -> RuleResult:
        job = self._translate_phrase(m.group("job"))
        return RuleResult(f"{m.group('name')} 取消{job}。", _TEMPLATE_CONFIDENCE)

    def _apply_become(self, m: re.Match[str]) -> RuleResult:
        prof = self.terminology.replace_phrases(m.group("profession"))
        return RuleResult(f"{m.group('name')} 成为{prof}。", _TEMPLATE_CONFIDENCE)

    def _apply_is_now(self, m: re.Match[str]) -> RuleResult:
        prof = self.terminology.replace_phrases(m.group("profession"))
        return RuleResult(f"{m.group('name')} 现在是一名{prof}。", _TEMPLATE_CONFIDENCE)

    def _apply_died(self, m: re.Match[str]) -> RuleResult:
        return RuleResult(f"{m.group('name')} 死了。", _TEMPLATE_CONFIDENCE)

    # --- 短语翻译 -----------------------------------------------------------

    def _translate_phrase(self, phrase: str) -> str:
        """job 短语翻译: 首词动词映射 + 词典最长匹配短语替换。"""
        words = phrase.split()
        if words and words[0].lower() in _JOB_VERBS:
            words[0] = _JOB_VERBS[words[0].lower()]
        partial = " ".join(words)
        translated = self.terminology.replace_phrases(partial)
        return re.sub(r"\s+", "", translated)  # 去空格: 制作 木桶 → 制作木桶
