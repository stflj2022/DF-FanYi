"""2026-09-12 全中文化质量门 (用户指令 "都翻译, 不要出现外文")

测试 orchestrator._count_english_function_words 对译文中残留英文功能词的检测。
超阈值(默认 2)→ 触发 quality_english_leak 回退原文。
"""
import pytest

from df_fanyi.core.orchestrator import (
    _count_english_function_words,
    _MAX_ENGLISH_RESIDUE,
    _ENGLISH_FUNCTION_WORDS,
)


class TestEnglishFunctionWordSet:
    def test_set_includes_common_function_words(self):
        assert "the" in _ENGLISH_FUNCTION_WORDS
        assert "and" in _ENGLISH_FUNCTION_WORDS
        assert "are" in _ENGLISH_FUNCTION_WORDS
        assert "of" in _ENGLISH_FUNCTION_WORDS
        assert "in" in _ENGLISH_FUNCTION_WORDS
        assert "to" in _ENGLISH_FUNCTION_WORDS
        assert "with" in _ENGLISH_FUNCTION_WORDS
        assert "that" in _ENGLISH_FUNCTION_WORDS
        assert "which" in _ENGLISH_FUNCTION_WORDS
        assert "you" in _ENGLISH_FUNCTION_WORDS
        assert "is" in _ENGLISH_FUNCTION_WORDS
        assert "there" in _ENGLISH_FUNCTION_WORDS

    def test_set_excludes_nouns_and_verbs(self):
        # 这些是名词/动词, 不在功能词集合里
        assert "dragon" not in _ENGLISH_FUNCTION_WORDS
        assert "attack" not in _ENGLISH_FUNCTION_WORDS
        assert "citizen" not in _ENGLISH_FUNCTION_WORDS
        assert "dwarf" not in _ENGLISH_FUNCTION_WORDS

    def test_threshold_is_two(self):
        # 阈值默认 2, 用户硬指令"不要出现外文"应非常严格
        assert _MAX_ENGLISH_RESIDUE == 2


class TestCountEnglishFunctionWords:
    def test_pure_chinese_zero(self):
        assert _count_english_function_words("纯中文译文。") == 0
        assert _count_english_function_words("秋天来了。") == 0
        assert _count_english_function_words("流浪狗想念灰叶猴女！") == 0

    def test_one_function_word_below_threshold(self):
        # 1 个功能词 → 0 leak (低于阈值 2, 接受)
        # "秋天来了" 没英文功能词 → 0
        assert _count_english_function_words("秋天来了 The") == 1  # 1 个 the

    def test_multiple_function_words_above_threshold(self):
        # "There are several kinds" → There + are + several(不在) + of(在) + ...
        # 这里测一个明显超阈值的
        result = _count_english_function_words(
            "There are several kinds of 功能区, which you can see in the panel."
        )
        assert result >= 3  # There + are + of + which + you + the = 6

    def test_proper_noun_not_counted(self):
        # "Dwarf Fortress" 是专有名词, 不应该被计为功能词
        assert _count_english_function_words("欢迎来到 Dwarf Fortress") == 0
        # "Dwarf Fortress is a game" → is+a = 2 (都属功能词)
        assert _count_english_function_words("Dwarf Fortress is a game") == 2

    def test_variable_placeholder_not_counted(self):
        # {COUNT} {ITEM} 等占位符应该被忽略, 不会算成英文
        assert _count_english_function_words("你有 {COUNT} 个 {ITEM}") == 0
        assert _count_english_function_words("{ITEM} is ready") == 1  # is

    def test_case_insensitive(self):
        # 大小写不敏感
        a = _count_english_function_words("The and ARE with")
        b = _count_english_function_words("the AND are WITH")
        assert a == b == 4

    def test_mixed_punctuation(self):
        # 标点不影响分词
        assert _count_english_function_words("功能区, which, 你-designate") == 1  # which

    def test_no_english_at_all(self):
        assert _count_english_function_words("") == 0
        assert _count_english_function_words("你好，世界。") == 0