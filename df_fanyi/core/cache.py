"""L1 内存 LRU 缓存(工程书 §9: Memory LRU)。

容量可配(cache.l1_size); 线程安全(后台 worker 与主线程共用, ticket-007 起)。
只缓存成功译文, 失败回退结果不写缓存(避免污染)。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheEntry:
    """缓存条目: 翻译结果的可序列化核心字段(§10 Response 子集)。"""

    source_text: str
    text: str
    model: str
    provider: str
    confidence: float


class LRUCache:
    """容量受限的线程安全 LRU。get 刷新最近使用; 超出容量淘汰最久未用。"""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(f"LRU 容量必须 ≥1: {capacity}")
        self._capacity = capacity
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            self._data.move_to_end(key)  # 刷新 recency
            return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        with self._lock:
            self._data[key] = entry
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
