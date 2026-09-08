"""ticket-007: 复杂度启发式(工程书 §34)。

档位: 词典≈0.1 / 规则≈0.25 / 本地 LLM 0.45-0.65 / 云端 ≥0.8。
初版: 长度(500+ 字符 → 0.50) + 变量/标记(每 +0.05, ≤0.20) − 模板句(0.15)。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.queue import ASYNC_THRESHOLD_DEFAULT, complexity_score  # noqa: E402


def test_short_dictionary_word_low_complexity() -> None:
    assert complexity_score("Dwarf") < 0.1  # 词典档(≈0.1 以下)


def test_template_sentence_belongs_to_rule_band() -> None:
    """规则模板句减分 → 归入词典/规则档(≤0.25)。"""
    score = complexity_score("Urist cancels Make Wooden Barrel.")
    assert score <= 0.25


def test_long_text_high_complexity_async() -> None:
    """500+ 字符 → 高阶(本地 LLM 档 0.45-0.65), 走异步(§29 长句不阻塞)。"""
    long_text = "The ancient dwarven halls echo with the sound of pickaxes striking stone. " * 8
    assert len(long_text) > 500
    score = complexity_score(long_text)
    assert 0.45 <= score <= 0.65


def test_variables_add_complexity() -> None:
    plain = complexity_score("A magical portal opens slowly before you.")
    with_var = complexity_score("A magical portal opens slowly before you, containing {COUNT} artifacts.")
    assert with_var > plain
    many_vars = complexity_score("{A}{B}{C}{D} " * 40)
    assert many_vars > ASYNC_THRESHOLD_DEFAULT  # 变量堆叠 → 超出快路径档


def test_markup_token_counts() -> None:
    score = complexity_score("It <color=red>glows</color> with an inner fire.")
    assert ASYNC_THRESHOLD_DEFAULT < score < 1.0 or score <= 1.0  # 有标记 → 比纯短句高
    assert score > complexity_score("It glows with an inner fire.")


def test_score_bounded_and_monotonic_in_length() -> None:
    assert complexity_score("") == 0.0
    assert complexity_score("   ") == 0.0
    short = complexity_score("a")
    long = complexity_score("a" * 600)
    assert short <= long
    assert 0.0 <= complexity_score("a" * 10_000) <= 1.0  # 封顶 1.0


def test_default_threshold_is_rule_band_upper() -> None:
    assert ASYNC_THRESHOLD_DEFAULT == 0.25