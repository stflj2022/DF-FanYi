"""复杂度启发式(ticket-007, 工程书 §34)。

初版启发式: 长度 + 变量/标记数 − 模板句减分, 归一 0.0-1.0。
档位(§34): 词典 ≈0.1 / 规则 ≈0.25 / 本地 LLM 0.45-0.65 / 云端 ≥0.8。

调度用途(§34 + ticket-007): 复杂度 ≤ 词典/规则档阈值(async_threshold, 默认 0.25)
→ 走同步快路径(缓存/词典/规则), 否则入队异步(LLM 才能解决)。
阈值来自实际调试, 不写死理论值(§34) —— 默认 0.25 为规则档上界, config 可调。
"""
from __future__ import annotations

import re

# 词典≈0.1 / 规则≈0.25 的上界(§34); config queue.async_threshold 可覆盖
ASYNC_THRESHOLD_DEFAULT = 0.25

# 变量/标记 token(与 protector 同风格: {COUNT}、<color=red>)
_VAR_TOKEN = re.compile(r"\{[^{}\n]+\}|</?[A-Za-z][^>]*>")

# 规则引擎可处理的模板句形状(命中 → 减分归入规则档)
_TEMPLATE_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[A-Z][A-Za-z' -]+ cancels [^.]+\.$"),
    re.compile(r"^[A-Z][A-Za-z' -]+ has become a [A-Za-z' -]+\.$"),
    re.compile(r"^[A-Z][A-Za-z' -]+ is now a [A-Za-z' -]+\.$"),
    re.compile(r"^[A-Z][A-Za-z' -]+ has died\.$"),
)

_MAX_LENGTH_FACTOR = 0.50   # 长度分量封顶(500+ 字符)
_PER_VAR_WEIGHT = 0.05      # 每个变量/标记 +0.05(封顶 0.20)
_TEMPLATE_PENALTY = 0.15    # 模板句减分 → 归入规则档


def _is_template_like(text: str) -> bool:
    return any(pattern.match(text) for pattern in _TEMPLATE_SHAPES)


def complexity_score(text: str) -> float:
    """§34 复杂度初版启发式(0.0-1.0 归一, 确定性无状态)。

    - 长度: 每 100 字符 +0.10, 500+ 字符封顶 0.50(§29 长句异步);
    - 变量/标记: 每个 +0.05 封顶 0.20(变量需 LLM 才能保真翻译);
    - 模板句: −0.15(规则引擎可处理, 归入词典/规则档);
    - 结果截断到 [0.0, 1.0], 末尾保留 3 位小数。
    """
    if not text or not text.strip():
        return 0.0
    length_factor = min(_MAX_LENGTH_FACTOR, len(text) / 500.0 * _MAX_LENGTH_FACTOR)
    var_factor = min(0.20, len(_VAR_TOKEN.findall(text)) * _PER_VAR_WEIGHT)
    template_penalty = _TEMPLATE_PENALTY if _is_template_like(text) else 0.0
    score = max(0.0, min(1.0, length_factor + var_factor - template_penalty))
    return round(score, 3)