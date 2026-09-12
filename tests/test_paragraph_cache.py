"""ticket-015: 段落缓存(ParagraphCache) —— 跨进程持久化 + 内存 LRU。

覆盖:
1. normalize: 小写 + 去标点 + collapse whitespace;
2. hash_text: sha256[:16] 稳定 + 同归一化文本 → 同 hash;
3. 内存 LRU put/get: 顺序写入 → 命中;
4. LRU 容量淘汰: 超过 capacity → 最久未访问淘汰;
5. SQLite 持久化: put 后跨 ParagraphCache 实例(同 db_path)仍命中;
6. load_top: 启动时拉最近 N 条到 LRU;
7. gc: ts > max_age_days 的记录被清理;
8. 线程安全: 多线程并发 put/get 不破坏 OrderedDict;
9. context 字段不影响 hash(只用于调试 / 不参与缓存键)。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from df_fanyi.bridge.inline_cache import ParagraphCache


def test_normalize_lowers_and_strips_punct() -> None:
    p = ParagraphCache(Path("/tmp/_n_test.db"))
    try:
        assert p.normalize("Hello, World!") == "hello world"
        assert p.normalize("  Foo\tbar\nbaz  ") == "foo bar baz"
        assert p.normalize("THE END.") == "the end"
    finally:
        p.close()


def test_hash_text_stable_and_collapses_diff_text_to_same_hash() -> None:
    p = ParagraphCache(Path("/tmp/_h_test.db"))
    try:
        assert p.hash_text("abc") == p.hash_text("ABC")
        assert p.hash_text("Hello, World!") == p.hash_text("hello world")
        # 不同文本 → 不同 hash(高位概率)
        assert p.hash_text("foo") != p.hash_text("bar")
    finally:
        p.close()


def test_put_get_lru_basic(tmp_path: Path) -> None:
    cache = ParagraphCache(tmp_path / "p.db")
    try:
        cache.put("k1", "译文1", confidence=0.9, model="m", provider="p", context="textviewer")
        rec = cache.get("k1")
        assert rec is not None
        assert rec["translated_text"] == "译文1"
        assert rec["confidence"] == 0.9
        assert rec["model"] == "m"
        assert rec["provider"] == "p"
        assert rec["context"] == "textviewer"
        # 不存在的 key
        assert cache.get("nonexistent") is None
    finally:
        cache.close()


def test_lru_capacity_evicts_oldest(tmp_path: Path) -> None:
    cache = ParagraphCache(tmp_path / "p.db", capacity=3)
    try:
        for i in range(5):
            cache.put(f"k{i}", f"t{i}")
        # LRU 容量=3 → 内存中只剩最近 3 条(k2,k3,k4)
        assert len(cache) == 3
        keys = set(cache.keys())
        assert keys == {"k2", "k3", "k4"}
        # 持久层仍在 → get 仍能拿到(SQLite 命中,不写回 LRU 因容量满)
        # 但 LRU 顺序反映了"最近写入的优先"语义
    finally:
        cache.close()


def test_lru_get_refreshes_order(tmp_path: Path) -> None:
    """get 命中后,该 key 应在 LRU 中(LRU miss→SQLite 命中仍返回)。

    SQLite 兜底语义下 'b 仍能 get 到'(持久层未删),但 'b' 不在 LRU 中
    (被 d 挤出);关键验收: 'a' 仍在 LRU 中(访问刷新顺序)。
    """
    cache = ParagraphCache(tmp_path / "p.db", capacity=3)
    try:
        cache.put("a", "A")
        cache.put("b", "B")
        cache.put("c", "C")
        # 访问 a → a 在 LRU 中被刷新到末尾
        cache.get("a")
        # 写新 key d → LRU 满, 淘汰最久未访问的 'b'
        cache.put("d", "D")
        keys = set(cache.keys())
        assert keys == {"a", "c", "d"}, f"a 应保留, b 应被挤出: {keys}"
        # 持久层 SQLite 仍有 b → get b 仍命中(SQLite 兜底)
        assert cache.get("b") is not None
        assert cache.get("b")["translated_text"] == "B"
        # 'a' 在 LRU 中(刚访问过), get 不应走 SQLite
        rec_a = cache.get("a")
        assert rec_a is not None and rec_a["translated_text"] == "A"
    finally:
        cache.close()


def test_sqlite_persistence_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "persist.db"
    c1 = ParagraphCache(db)
    c1.put("k_persist", "持久化译文")
    c1.close()
    # 新实例同路径应命中(走 LRU miss → SQLite hit)
    c2 = ParagraphCache(db)
    try:
        rec = c2.get("k_persist")
        assert rec is not None
        assert rec["translated_text"] == "持久化译文"
    finally:
        c2.close()


def test_load_top_warms_lru(tmp_path: Path) -> None:
    db = tmp_path / "warm.db"
    c1 = ParagraphCache(db)
    for i in range(20):
        c1.put(f"k{i:02d}", f"t{i}", confidence=float(i))
    c1.close()
    # 新实例: 启动预热 5 条 → LRU 命中
    c2 = ParagraphCache(db)
    try:
        loaded = c2.load_top(limit=5)
        assert len(loaded) == 5
        # 加载后 LRU 应命中(不再走 SQLite)
        for k, v in loaded.items():
            assert c2.get(k) == v
    finally:
        c2.close()


def test_gc_removes_expired(tmp_path: Path) -> None:
    cache = ParagraphCache(tmp_path / "gc.db", max_age_days=0)  # 0 天 → 全部过期
    try:
        cache.put("k_old", "old", ts=int(time.time()) - 86400 * 30)  # 30 天前
        cache.put("k_new", "new")  # 当前
        removed = cache.gc()
        assert removed >= 1
        assert cache.get("k_old") is None
        assert cache.get("k_new") is not None
    finally:
        cache.close()


def test_concurrent_put_get_thread_safe(tmp_path: Path) -> None:
    cache = ParagraphCache(tmp_path / "conc.db", capacity=500)
    errors: list[Exception] = []

    def writer(start: int) -> None:
        try:
            for i in range(100):
                cache.put(f"k-{start}-{i}", f"v-{start}-{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader() -> None:
        try:
            for _ in range(100):
                cache.get("k-0-0")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发读写报错: {errors}"
    assert len(cache) <= 500
    cache.close()


def test_context_does_not_affect_cache_key(tmp_path: Path) -> None:
    """context 字段仅作调试/审计用,不应影响缓存命中(键 = normalize(text) 单一维度)。"""
    cache = ParagraphCache(tmp_path / "ctx.db")
    try:
        cache.put("samekey", "译文", context="textviewer")
        # 同一 key 不同 context 写入 → 后写覆盖
        cache.put("samekey", "新译文", context="announcement")
        rec = cache.get("samekey")
        assert rec is not None and rec["translated_text"] == "新译文"
        assert rec["context"] == "announcement"
    finally:
        cache.close()


def test_keys_for_lua_startup_load(tmp_path: Path) -> None:
    """Lua 端启动加载需要 LRU 当前 keys → 暴露 keys() 接口。"""
    cache = ParagraphCache(tmp_path / "lua.db")
    try:
        cache.put("k1", "t1")
        cache.put("k2", "t2")
        keys = set(cache.keys())
        assert keys == {"k1", "k2"}
    finally:
        cache.close()