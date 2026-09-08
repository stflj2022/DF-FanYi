"""ticket-004: 规则引擎(模板句 → 中文模板, 置信度阈值 0.9)。

工程书 §11: rule_result.confidence >= RULE_THRESHOLD 才接受;
§34: 词典≈0.1 → 规则≈0.25 复杂度档位。模板命中视为高置信(0.95)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.rules import RULE_THRESHOLD, RuleEngine  # noqa: E402
from df_fanyi.database.terminology import Terminology  # noqa: E402


@pytest.fixture(scope="module")
def engine() -> RuleEngine:
    terms = Terminology.from_terms(
        [
            {"source": "wooden barrel", "target": "木桶"},
            {"source": "carpenter", "target": "木匠"},
            {"source": "mason", "target": "石匠"},
            {"source": "barrel", "target": "桶"},
        ]
    )
    return RuleEngine(terms)


def test_rule_threshold_is_0_9() -> None:
    assert RULE_THRESHOLD == 0.9


def test_cancels_template(engine: RuleEngine) -> None:
    """Test 3 核心模板: {name} cancels {job}. → 中文, job 经词典短语翻译。"""
    result = engine.translate("Urist cancels Make Wooden Barrel.")
    assert result is not None
    assert result.text == "Urist 取消制作木桶。"
    assert result.confidence >= RULE_THRESHOLD


def test_has_become_template(engine: RuleEngine) -> None:
    result = engine.translate("Urist has become a carpenter.")
    assert result is not None
    assert result.text == "Urist 成为木匠。"
    assert result.confidence >= RULE_THRESHOLD


def test_is_now_template(engine: RuleEngine) -> None:
    result = engine.translate("Urist is now a mason.")
    assert result is not None
    assert result.text == "Urist 现在是一名石匠。"


def test_has_died_template(engine: RuleEngine) -> None:
    result = engine.translate("Urist has died.")
    assert result is not None
    assert result.text == "Urist 死了。"


def test_name_is_preserved_in_template(engine: RuleEngine) -> None:
    result = engine.translate("Urist McDwarf cancels Make Wooden Barrel.")
    assert result is not None
    assert result.text.startswith("Urist McDwarf ")


def test_unknown_sentence_returns_none(engine: RuleEngine) -> None:
    """非模板文本不得误命中。"""
    assert engine.translate("The quick brown fox jumps over the lazy dog.") is None
    assert engine.translate("Dwarf") is None  # 单词典词走词典, 不走规则


def test_below_threshold_returns_none(engine: RuleEngine) -> None:
    """阈值可配: 高于阈值才接受(工单: 置信度阈值 0.9)。"""
    engine_99 = RuleEngine(engine.terminology, threshold=0.99)
    assert engine_99.translate("Urist has died.") is None


def test_job_verb_mapping(engine: RuleEngine) -> None:
    """job 短语动词映射: Make→制作(规则内建), 名词短语走词典。"""
    assert engine.translate("Urist cancels Build Wooden Barrel.").text == "Urist 取消建造木桶。"  # type: ignore[union-attr]
