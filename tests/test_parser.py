"""ticket-004: normalize + source_hash(工程书 §11/§37)。

§37: 上下文敏感文本的 hash 必须包含 relevant context,
否则相同文本、不同上下文会错误命中缓存产生歧义译文。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.parser import normalize_text, source_hash  # noqa: E402


def test_normalize_strips_and_collapses_whitespace() -> None:
    assert normalize_text("   Urist   cancels  Make  ") == "Urist cancels Make"
    assert normalize_text("\nwooden barrel\t\n") == "wooden barrel"


def test_normalize_empty() -> None:
    assert normalize_text("   ") == ""
    assert normalize_text(None) == ""  # type: ignore[arg-type]


def test_source_hash_is_sha256_hex() -> None:
    h = source_hash("Dwarf")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_source_hash_deterministic() -> None:
    assert source_hash("Urist cancels Make Wooden Barrel.") == source_hash(
        "Urist cancels Make Wooden Barrel."
    )


def test_source_hash_differs_between_texts() -> None:
    assert source_hash("Dwarf") != source_hash("dwarf")


def test_source_hash_includes_context_when_given() -> None:
    """§37: 相同文本 + 不同相关上下文 → 不同 hash, 不共享缓存。"""
    ctx_a = {"screen": "announcement", "character": "Urist McDwarf"}
    ctx_b = {"screen": "announcement", "character": "Urist McAdam"}
    assert source_hash("Urist cancels Make Wooden Barrel.", ctx_a) != source_hash(
        "Urist cancels Make Wooden Barrel.", ctx_b
    )


def test_source_hash_without_context_equals_empty_context() -> None:
    """空上下文等价于无上下文: 避免语义上等价的两个 key 分叉缓存。"""
    assert source_hash("Dwarf") == source_hash("Dwarf", {})
    assert source_hash("Dwarf") == source_hash("Dwarf", None)
