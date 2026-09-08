"""ticket-004: Validator 基础版(数字集合守恒 / 非空 / 长度比异常)。

工程书 §24: 至少检查 variables/markup/numbers/names/line count/placeholder count;
ticket-004 只要求基础版三件套(数字/非空/长度比), 变量与 markup 由 ticket-005 补齐。
不合格 → 拒绝(回退原文)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


# --- ticket-005: 完整 Validator(§24: 变量/markup/数字/行数守恒) ------------


def test_variable_preserved_valid() -> None:
    """原始变量 {COUNT} 在译文中完整保留 → 通过。"""
    r = Validator().validate("{COUNT} dwarves", "{COUNT} 名矮人")
    assert r.valid is True
    assert r.errors == []


def test_variable_missing_rejected() -> None:
    """译文丢失变量 → 拒绝(Test 4 守护)。"""
    r = Validator().validate("{COUNT} dwarves", "名矮人")
    assert r.valid is False
    assert any("COUNT" in e for e in r.errors)


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("{COUNT} dwarves", "{COUNT} 名 {COUNT} 矮人"),  # 重复变量
        ("{COUNT} dwarves", "{ITEM} 名矮人"),  # 变量被替换
        ("plain {A} {B} text", "plain {B} text"),  # 少一个变量
    ],
)
def test_variable_multiset_mismatch_rejected(source: str, translation: str) -> None:
    """变量多重集合不一致(计数/身份) → 拒绝。"""
    r = Validator().validate(source, translation)
    assert r.valid is False
    assert any("变量" in e for e in r.errors)


def test_variable_extra_rejected() -> None:
    """译文引入源文没有的变量 → 拒绝。"""
    r = Validator().validate("dwarves", "{COUNT} 名矮人")
    assert r.valid is False
    assert any("变量" in e for e in r.errors)


def test_protected_placeholder_preserved_valid() -> None:
    """管线保护后的占位符形式(VAR_001/MARKUP_001)同样受校验。"""
    r = Validator().validate("VAR_001 dwarves", "VAR_001 名矮人")
    assert r.valid is True


def test_protected_placeholder_missing_rejected() -> None:
    r = Validator().validate("VAR_001 dwarves", "名矮人")
    assert r.valid is False
    assert any("VAR_001" in e for e in r.errors)


def test_markup_preserved_valid() -> None:
    """标记完整保留 → 通过(Test 5 守护)。"""
    r = Validator().validate("<color=red>Urist</color>", "<color=red>乌里斯特</color>")
    assert r.valid is True


def test_markup_missing_rejected() -> None:
    """译文丢失标记 → 拒绝。"""
    r = Validator().validate("<color=red>Urist</color>", "乌里斯特")
    assert r.valid is False
    assert any("color" in e for e in r.errors)


def test_markup_extra_rejected() -> None:
    """译文引入源文没有的标记 → 拒绝。"""
    r = Validator().validate("Urist", "<b>乌里斯特</b>")
    assert r.valid is False
    assert any("标记" in e for e in r.errors)


def test_markup_multiset_mismatch_rejected() -> None:
    """标记数量不一致(如把 </color> 写两遍) → 拒绝。"""
    r = Validator().validate(
        "<color=red>Urist</color>", "<color=red>乌里斯特</color></color>"
    )
    assert r.valid is False


def test_mixed_variable_and_markup_valid() -> None:
    r = Validator().validate(
        "<color=red>{COUNT} dwarves</color>", "<color=red>{COUNT} 名矮人</color>"
    )
    assert r.valid is True


def test_line_count_mismatch_rejected() -> None:
    """行数必须一致(§24 line count)。"""
    r = Validator().validate("line one\nline two", "第一行")
    assert r.valid is False
    assert any("行数" in e for e in r.errors)


def test_line_count_equal_valid() -> None:
    r = Validator().validate("line one\nline two", "第一行\n第二行")
    assert r.valid is True


def test_number_multiset_duplication_rejected() -> None:
    """数字多重集合: 译文把数字重复一遍也算引入额外数字。"""
    r = Validator().validate("12 dwarves", "12 12 名矮人")
    assert r.valid is False


def test_full_validator_combined_reject_reports_errors() -> None:
    """多种缺陷并存 → 全部列出。"""
    r = Validator().validate("{COUNT} <b>12</b> dwarves", "矮人")
    assert r.valid is False
    assert r.score < 1.0
    assert any("变量" in e for e in r.errors)
    assert any("标记" in e for e in r.errors)
    assert any("12" in e for e in r.errors)
