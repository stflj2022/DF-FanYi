"""ticket-004: Validator 基础版(数字集合守恒 / 非空 / 长度比异常)。

工程书 §24: 至少检查 variables/markup/numbers/names/line count/placeholder count;
ticket-004 只要求基础版三件套(数字/非空/长度比), 变量与 markup 由 ticket-005 补齐。
不合格 → 拒绝(回退原文)。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.validator import Validator  # noqa: E402


def test_valid_translation() -> None:
    r = Validator().validate("2 dwarves arrived.", "2 名矮人抵达。")
    assert r.valid is True
    assert r.score == 1.0
    assert r.errors == []


def test_empty_translation_rejected() -> None:
    r = Validator().validate("Dwarf", "")
    assert r.valid is False
    assert any("空" in e or "empty" in e.lower() for e in r.errors)


def test_whitespace_only_rejected() -> None:
    r = Validator().validate("Dwarf", "   ")
    assert r.valid is False


def test_missing_number_rejected() -> None:
    """源文数字在译文中缺失 → 拒绝。"""
    r = Validator().validate("12 dwarves", "矮人")
    assert r.valid is False
    assert any("12" in e for e in r.errors)


def test_unexpected_number_rejected() -> None:
    """译文引入源文没有的数字 → 拒绝。"""
    r = Validator().validate("Dwarf", "3 名矮人")
    assert r.valid is False
    assert any("3" in e for e in r.errors)


def test_length_ratio_too_long_rejected() -> None:
    """>3x 长度比异常 → 拒绝。"""
    r = Validator().validate("Dwarf", "矮人" * 10)
    assert r.valid is False
    assert any("长度" in e for e in r.errors)


def test_length_ratio_too_short_rejected() -> None:
    """<0.3x 长度比异常 → 拒绝(但不得与空译文检查重复计数时矛盾)。"""
    r = Validator().validate("Urist cancels Make Wooden Barrel.", "好")
    assert r.valid is False
    assert any("长度" in e for e in r.errors)


def test_boundary_ratios_accepted() -> None:
    """长度比边界 [0.3, 3.0] 内应通过(按信息长度: 汉字计 1, 词元计 1)。"""
    v = Validator()
    assert v.validate("A B C D E", "一二三四").valid is True  # 4/5=0.8
    assert v.validate("A", "一二三").valid is True  # 3/1=3.0 上边界
    assert v.validate("ABCDEFGHIJ", "一二三").valid is True  # 3/10=0.3 下边界


def test_score_decreases_with_errors() -> None:
    r = Validator().validate("Dwarf 12", "")
    assert r.valid is False
    assert r.score < 1.0
