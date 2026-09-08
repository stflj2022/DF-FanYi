"""Validator 完整版(ticket-005, 工程书 §24)。

至少检查: variables / markup / numbers / names / line count / placeholder count。
本实现硬校验六类(不变量守恒, 失败 → valid=False + errors, 编排器回退原文):

1. 非空
2. 数字多重集合守恒(源文数字必须原样出现在译文, 不得增删重复)
3. 变量多重集合守恒 —— 原始变量({COUNT})与保护后占位符(VAR_001)双格式
4. markup 多重集合守恒 —— 原始标记(<color=red>)与占位符(MARKUP_001)双格式
5. 行数一致
6. 长度比异常拒绝(>3x 或 <0.3x, 按信息长度)

names(人名)刻意不做硬校验: 中译习惯音译(乌里斯特), 硬性要求英文人名原样
出现在译文中会误杀合理译文; 人名保全由提示词规则 4 + 词典 locked 词条 +
Test 5(标记内人名不被误翻)兜底。

数字用多重集合(Counter)而非集合: 译文把 "12" 写成 "12 12" 同样算引入额外数字。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

MIN_LENGTH_RATIO = 0.3
MAX_LENGTH_RATIO = 3.0


_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+")
_NUMBER = re.compile(r"\d+")

# 变量: 原始 {COUNT} + 保护后 VAR_001
# 占位符模式不用 \b 或 ASCII 边界: 占位符常紧贴其它词(如 MARKUP_001UristMARKUP_002
# 或 …乌里斯特…), 边界断言会漏检; 裸匹配对原文含字面占位符位串同样保守正确
# (译文必须原样保留, 否则就是信息丢失 → 拒绝)。
_VARIABLE = re.compile(r"\{[^{}\n]+\}")
_PROTECTED_VARIABLE = re.compile(r"VAR_\d+")
# 标记: 原始 <color=red> + 保护后 MARKUP_001
_MARKUP = re.compile(r"</?[A-Za-z][A-Za-z0-9_=:./ -]*>")
_PROTECTED_MARKUP = re.compile(r"MARKUP_\d+")


def _info_length(text: str) -> int:
    """信息长度: 每个汉字计 1, 每串英文/数字词元计 1。

    英文→中文天然压缩(40 字符英文 ≈ 9 汉字), 用原始字符数比较会把合理译文
    误判为过短; 用信息单位(汉字数 + 词元数)比较才与 >3x / <0.3x 语义一致。
    """
    return len(_CJK.findall(text)) + len(_WORD.findall(text))


def _tokens(text: str, raw_re: re.Pattern[str], protected_re: re.Pattern[str]) -> Counter[str]:
    """提取 token 多重集合: 原始格式 + 保护后占位符双通道。"""
    return Counter(raw_re.findall(text) + protected_re.findall(text))


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    score: float
    errors: list[str] = field(default_factory=list)


class Validator:
    """完整校验器: 不合格译文 → 拒绝回退原文(绝不阻塞调用方)。"""

    def validate(self, source: str, translation: str) -> ValidationResult:
        errors: list[str] = []

        # 1. 非空
        if not translation or not translation.strip():
            errors.append("译文为空")

        # 2. 数字多重集合守恒
        src_numbers = Counter(_NUMBER.findall(source))
        tgt_numbers = Counter(_NUMBER.findall(translation))
        _report_deltas(errors, "数字", src_numbers, tgt_numbers, "译文丢失数字", "译文引入额外数字")

        # 3. 变量多重集合守恒(原始 + 保护后双格式)
        _report_deltas(
            errors,
            "变量",
            _tokens(source, _VARIABLE, _PROTECTED_VARIABLE),
            _tokens(translation, _VARIABLE, _PROTECTED_VARIABLE),
            "译文丢失变量",
            "译文引入额外变量",
        )

        # 4. markup 多重集合守恒(原始 + 保护后双格式)
        _report_deltas(
            errors,
            "标记",
            _tokens(source, _MARKUP, _PROTECTED_MARKUP),
            _tokens(translation, _MARKUP, _PROTECTED_MARKUP),
            "译文丢失标记",
            "译文引入额外标记",
        )

        # 5. 行数一致(§24 line count)
        if source and translation:
            src_lines = len(source.splitlines())
            tgt_lines = len(translation.splitlines())
            if src_lines != tgt_lines:
                errors.append(f"行数不一致: 原文 {src_lines} 行, 译文 {tgt_lines} 行")

        # 6. 长度比异常(>3x 或 <0.3x, 按信息长度); 空译文已在第 1 步标记
        if translation and translation.strip() and source:
            ratio = _info_length(translation) / _info_length(source)
            if ratio > MAX_LENGTH_RATIO or ratio < MIN_LENGTH_RATIO:
                errors.append(
                    f"长度比异常: {ratio:.2f} 超出 [{MIN_LENGTH_RATIO}, {MAX_LENGTH_RATIO}]"
                )

        score = max(0.0, 1.0 - 0.2 * len(errors))
        return ValidationResult(valid=not errors, score=score, errors=errors)


def _report_deltas(
    errors: list[str],
    kind: str,
    src: Counter[str],
    tgt: Counter[str],
    missing_msg: str,
    extra_msg: str,
) -> None:
    """对比两个多重集合, 把差异追加进 errors(中文消息, 与基础版风格一致)。"""
    missing = list((src - tgt).elements())
    extra = list((tgt - src).elements())
    if missing:
        errors.append(f"{missing_msg}: {sorted(set(missing))}")
    if extra:
        errors.append(f"{extra_msg}: {sorted(set(extra))}")