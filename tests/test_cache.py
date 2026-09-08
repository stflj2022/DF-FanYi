"""ticket-004: L1 内存 LRU 缓存(工程书 §9: Memory LRU)。

只测外部行为(容量/淘汰/命中/并发安全), 不 mock。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.cache import CacheEntry, LRUCache  # noqa: E402


def _entry(text: str, source: str = "src") -> CacheEntry:
    return CacheEntry(source_text=source, text=text, model="m", provider="p", confidence=1.0)


def test_get_miss_returns_none() -> None:
    c = LRUCache(capacity=4)
    assert c.get("nope") is None


def test_put_get_roundtrip() -> None:
    c = LRUCache(capacity=4)
    c.put("a", _entry("矮人"))
    assert c.get("a").text == "矮人"


def test_capacity_evicts_oldest() -> None:
    c = LRUCache(capacity=2)
    c.put("a", _entry("一"))
    c.put("b", _entry("二"))
    c.put("c", _entry("三"))  # 淘汰 a
    assert c.get("a") is None
    assert c.get("b").text == "二"
    assert c.get("c").text == "三"


def test_get_refreshes_recency() -> None:
    c = LRUCache(capacity=2)
    c.put("a", _entry("一"))
    c.put("b", _entry("二"))
    c.get("a")  # 刷新 a → b 成为最旧
    c.put("c", _entry("三"))
    assert c.get("b") is None
    assert c.get("a").text == "一"


def test_len_counts_entries() -> None:
    c = LRUCache(capacity=2)
    assert len(c) == 0
    c.put("a", _entry("一"))
    c.put("b", _entry("二"))
    assert len(c) == 2


def test_put_same_key_overwrites() -> None:
    c = LRUCache(capacity=2)
    c.put("a", _entry("旧"))
    c.put("a", _entry("新"))
    assert len(c) == 1
    assert c.get("a").text == "新"


def test_concurrent_access_is_safe() -> None:
    c = LRUCache(capacity=64)
    errors: list[Exception] = []

    def worker(base: int) -> None:
        try:
            for i in range(200):
                key = f"{base}-{i % 8}"
                c.put(key, _entry(str(i)))
                c.get(key)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(c) <= 64
