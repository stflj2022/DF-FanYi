"""Validator 基础版(ticket-004)。

工程书 §24: 至少检查 variables/markup/numbers/names/line count/placeholder count;
ticket-004 基础版实现三件套:
1. 非空
2. 数字集合守恒(源文数字必须在译文中出现, 译文不得引入新数字)
3. 长度比异常拒绝(>3x 或 <0.3x)

变量/标记/人名校验由 ticket-005(Markup/Variable Protector + 完整 Validator)补齐。
失败 → valid=False + errors, 编排器据此回退原文(§24/§30)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_LENGTH_RATIO = 0.3
MAX_LENGTH_RATIO = 3.0


_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+")


def _info_length(text: str) -> int:
    """信息长度: 每个汉字计 1, 每串英文/数字词元计 1。

    英文→中文天然压缩(40 字符英文 ≈ 9 汉字), 用原始字符数比较会把合理译文
    误判为过短; 用信息单位(汉字数 + 词元数)比较才与 >3x / <0.3x 语义一致。
    """
    return len(_CJK.findall(text)) + len(_WORD.findall(text))


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    score: float
    errors: list[str] = field(default_factory=list)


class Validator:
    """基础校验器: 不合格译文 → 拒绝回退原文(绝不阻塞调用方)。"""

    def validate(self, source: str, translation: str) -> ValidationResult:
        errors: list[str] = []

        # 1. 非空
        if not translation or not translation.strip():
            errors.append("译文为空")

        # 2. 数字集合守恒(连续数字串的集合)
        src_numbers = set(re.findall(r"\d+", source))
        tgt_numbers = set(re.findall(r"\d+", translation))
        missing = src_numbers - tgt_numbers
        extra = tgt_numbers - src_numbers
        if missing:
            errors.append(f"译文丢失数字: {sorted(missing)}")
        if extra:
            errors.append(f"译文引入额外数字: {sorted(extra)}")

        # 3. 长度比异常(>3x 或 <0.3x, 按信息长度); 空译文已在第 1 步标记, 不再重复计比
        if translation and translation.strip() and source:
            ratio = _info_length(translation) / _info_length(source)
            if ratio > MAX_LENGTH_RATIO or ratio < MIN_LENGTH_RATIO:
                errors.append(
                    f"长度比异常: {ratio:.2f} 超出 [{MIN_LENGTH_RATIO}, {MAX_LENGTH_RATIO}]"
                )

        score = max(0.0, 1.0 - 0.2 * len(errors))
        return ValidationResult(valid=not errors, score=score, errors=errors)
