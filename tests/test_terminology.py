"""ticket-004: 术语词典(database/ 内置 seed 词典, 精确匹配直返)。

- seed ≥ 50 条常用 DF 术语(工单要求 dwarf/wooden barrel/carpenter/cancel/fortress/migrate 等)
- 精确匹配(整句), 大小写不敏感
- locked=true 词条优先
- 数据来源署名(CC BY-NC 4.0, 见 REUSE_PLAN §6)必须在 seed 文件内
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.config import REPO_ROOT  # noqa: E402
from df_fanyi.database.terminology import Terminology  # noqa: E402

SEED_PATH = REPO_ROOT / "database" / "seed_terms.yaml"


@pytest.fixture(scope="module")
def seed() -> Terminology:
    return Terminology.load_seed(SEED_PATH)


def test_seed_file_exists() -> None:
    assert SEED_PATH.exists(), f"缺少 seed 词典: {SEED_PATH}"


def test_seed_has_at_least_50_terms(seed: Terminology) -> None:
    assert len(seed) >= 50, f"seed 词典应 ≥50 条, 实际 {len(seed)}"


def test_seed_contains_required_terms(seed: Terminology) -> None:
    """工单点名: dwarf=矮人, wooden barrel=木桶, carpenter=木匠,
    cancel=取消, fortress=要塞, migrate=迁徙。"""
    required = {
        "dwarf": "矮人",
        "wooden barrel": "木桶",
        "carpenter": "木匠",
        "cancel": "取消",
        "fortress": "要塞",
        "migrate": "迁徙",
    }
    for source, target in required.items():
        hit = seed.lookup(source)
        assert hit is not None, f"缺少必需词条: {source}"
        assert hit.target == target, f"{source} 译文应为 {target}, 实际 {hit.target}"


def test_lookup_exact_hit(seed: Terminology) -> None:
    hit = seed.lookup("Dwarf")
    assert hit is not None and hit.target == "矮人"


def test_lookup_case_insensitive(seed: Terminology) -> None:
    assert seed.lookup("Dwarf").target == "矮人"  # type: ignore[union-attr]
    assert seed.lookup("dwarf").target == "矮人"  # type: ignore[union-attr]
    assert seed.lookup("WOODEN BARREL").target == "木桶"  # type: ignore[union-attr]


def test_lookup_ignores_surrounding_whitespace(seed: Terminology) -> None:
    assert seed.lookup("  wooden barrel  ").target == "木桶"  # type: ignore[union-attr]


def test_lookup_miss_returns_none(seed: Terminology) -> None:
    assert seed.lookup("Urist cancels Make Wooden Barrel.") is None
    assert seed.lookup("") is None
    assert seed.lookup("   ") is None


def test_required_terms_are_locked(seed: Terminology) -> None:
    """工单: locked=true 优先 —— 点名术语必须 locked。"""
    for source in ("dwarf", "wooden barrel", "carpenter", "cancel", "fortress", "migrate"):
        hit = seed.lookup(source)
        assert hit is not None and hit.locked, f"{source} 应为 locked 词条"


def test_locked_wins_on_conflict() -> None:
    """同一 source 两条(一条 locked): locked 优先。"""
    terms = Terminology.from_terms(
        [
            {"source": "fish", "target": "鱼", "locked": False},
            {"source": "fish", "target": "捕鱼", "locked": True},
        ]
    )
    hit = terms.lookup("fish")
    assert hit is not None and hit.target == "捕鱼"


def test_seed_file_carries_attribution() -> None:
    """复用数据必须记录来源与许可(REUSE_PLAN §6: CC BY-NC 4.0)。"""
    text = SEED_PATH.read_text(encoding="utf-8")
    assert "CC BY-NC" in text, "seed 文件缺少许可署名"
    assert "REUSE_PLAN" in text or "dfi18n" in text.lower(), "seed 文件缺少来源记录"


def test_from_terms_accepts_raw_mappings() -> None:
    t = Terminology.from_terms([{"source": "cat", "target": "猫"}])
    assert t.lookup("Cat").target == "猫"  # type: ignore[union-attr]


def test_replace_phrases_longest_match_first() -> None:
    """规则引擎短语替换: 长词条优先(wooden barrel 而非 wood+barrel 分别替换)。"""
    t = Terminology.from_terms(
        [
            {"source": "wood", "target": "木材"},
            {"source": "barrel", "target": "桶"},
            {"source": "wooden barrel", "target": "木桶"},
        ]
    )
    out = t.replace_phrases("Make Wooden Barrel")
    assert out == "Make 木桶"


def test_replace_phrases_does_not_touch_chinese() -> None:
    t = Terminology.from_terms([{"source": "wood", "target": "木材"}])
    assert t.replace_phrases("制作 木桶 wood") == "制作 木桶 木材"
