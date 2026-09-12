"""段落缓存(ticket-015) —— 跨进程 SQLite 持久化 + 内存 LRU 热路径。

设计动机:
- 013 inline_translate 已有进程内 OrderedDict LRU(256 条), 进程重启即失忆;
- 长段落(如教程页 1000+ 字)在重新进入 textviewer 时仍触发 LLM,
  因为内存缓存冷启。015 把命中层下沉到 SQLite, 跨进程持久;
- 内存 LRU 仍是热路径(无锁外查找, μs 级), SQLite 是冷持久层。

数据结构:
- key (TEXT PK): `sha256(normalize(text))[:16]`  ─── 归一化: 小写 + 去标点
                                                       + collapse whitespace
- value (dict): translated_text / confidence / model / provider / context / ts

§2 验收: 启动加载最近 1000 条 → LRU; LRU 容量上限 1000; gc 7d 过期。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any


# 段落缓存默认上限(§2 / §风险: LRU 1000 条 + 7 天清理 → < 10MB)
DEFAULT_CAPACITY = 1000
DEFAULT_MAX_AGE_DAYS = 7

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS paragraph_cache (
    key TEXT PRIMARY KEY,
    translated_text TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paragraph_cache_ts ON paragraph_cache(ts DESC);
"""

# 标点符号剥离(段落归一化): 保留中文汉字 / ASCII 字母数字 / 空格
_PUNCT_RE = re.compile(r"[^\w\s\u4e00-\u9fff]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


class ParagraphCache:
    """段落级缓存: 内存 LRU(快路径) + SQLite 持久层(跨进程)。

    线程安全: OrderedDict 由 self._lock 保护; SQLite 连接
    check_same_thread=False + 内部锁。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        capacity: int = DEFAULT_CAPACITY,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"capacity 必须 ≥ 1, 得到 {capacity}")
        if max_age_days < 0:
            raise ValueError(f"max_age_days 必须 ≥ 0, 得到 {max_age_days}")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._capacity = capacity
        self._max_age_days = max_age_days
        self._lock = threading.Lock()
        self._lru: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._db_lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._db_lock:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    # ---- 归一化 / 哈希 -------------------------------------------------

    @staticmethod
    def normalize(text: str) -> str:
        """小写 + 去标点 + collapse whitespace(§2)。"""
        if not text:
            return ""
        # 小写(对 ASCII; 中文无大小写概念)
        t = text.lower()
        # 剥离标点, 保留 \w(unicode 字母数字下划线) + 中文 + 空白
        t = _PUNCT_RE.sub(" ", t)
        # collapse whitespace
        t = _WS_RE.sub(" ", t).strip()
        return t

    @staticmethod
    def hash_text(text: str) -> str:
        """sha256(normalize(text))[:16] —— 16 hex = 64 bit, 冲突概率极低(§风险)。"""
        norm = ParagraphCache.normalize(text)
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]

    # ---- 基础读写 ------------------------------------------------------

    def get(self, key: str) -> dict[str, Any] | None:
        """查 key → 命中 LRU 移动到末尾; LRU miss 走 SQLite, 命中写回 LRU。"""
        with self._lock:
            rec = self._lru.get(key)
            if rec is not None:
                self._lru.move_to_end(key)
                # 返回前复制一份, 避免外部修改污染内部状态
                return dict(rec)
        # LRU miss → SQLite
        rec = self._db_lookup(key)
        if rec is None:
            return None
        # 写回 LRU
        with self._lock:
            self._lru[key] = rec
            self._lru.move_to_end(key)
            while len(self._lru) > self._capacity:
                self._lru.popitem(last=False)
        return dict(rec)

    def put(
        self,
        key: str,
        translated_text: str,
        *,
        confidence: float = 0.0,
        model: str = "",
        provider: str = "",
        context: str = "",
        ts: int | None = None,
    ) -> None:
        """写入 LRU + SQLite 持久层。"""
        if ts is None:
            ts = int(time.time())
        rec: dict[str, Any] = {
            "translated_text": translated_text,
            "confidence": float(confidence),
            "model": model,
            "provider": provider,
            "context": context,
            "ts": ts,
        }
        with self._lock:
            self._lru[key] = rec
            self._lru.move_to_end(key)
            while len(self._lru) > self._capacity:
                self._lru.popitem(last=False)
        self._db_upsert(key, rec)

    def load_top(self, limit: int = DEFAULT_CAPACITY) -> dict[str, dict[str, Any]]:
        """启动预热: 从 SQLite 拉最近 limit 条到内存 LRU。

        返回 {key: record}; 已存在的 key 跳过(load_top 不覆盖, 避免重置访问顺序)。
        """
        if limit < 1:
            return {}
        with self._db_lock:
            cur = self._conn.execute(
                "SELECT key, translated_text, confidence, model, provider, context, ts "
                "FROM paragraph_cache ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for r in rows:
                if r["key"] in self._lru:
                    continue  # 已有, 跳过(保访问顺序)
                self._lru[r["key"]] = r
                self._lru.move_to_end(r["key"])
                while len(self._lru) > self._capacity:
                    self._lru.popitem(last=False)
                out[r["key"]] = dict(r)
        return out

    def gc(self) -> int:
        """清理 ts < (now - max_age_days*86400) 的过期记录。返回清理条数。"""
        if self._max_age_days <= 0:
            cutoff = int(time.time())  # 0 天 → 全部过期
        else:
            cutoff = int(time.time()) - self._max_age_days * 86400
        with self._db_lock:
            cur = self._conn.execute("DELETE FROM paragraph_cache WHERE ts < ?", (cutoff,))
            self._conn.commit()
            removed = cur.rowcount or 0
        # 同步清 LRU
        with self._lock:
            stale = [k for k, v in self._lru.items() if v["ts"] < cutoff]
            for k in stale:
                self._lru.pop(k, None)
        return removed

    def keys(self) -> list[str]:
        """当前 LRU 中所有 key(Lua 启动加载用, 不暴露 LRU 顺序细节)。"""
        with self._lock:
            return list(self._lru.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._lru)

    def close(self) -> None:
        with self._db_lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def __enter__(self) -> "ParagraphCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 私有: SQLite IO ------------------------------------------------

    def _db_lookup(self, key: str) -> dict[str, Any] | None:
        with self._db_lock:
            cur = self._conn.execute(
                "SELECT key, translated_text, confidence, model, provider, context, ts "
                "FROM paragraph_cache WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def _db_upsert(self, key: str, rec: dict[str, Any]) -> None:
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO paragraph_cache (key, translated_text, confidence, model, "
                "provider, context, ts) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "translated_text=excluded.translated_text, "
                "confidence=excluded.confidence, "
                "model=excluded.model, "
                "provider=excluded.provider, "
                "context=excluded.context, "
                "ts=excluded.ts",
                (
                    key,
                    rec["translated_text"],
                    rec["confidence"],
                    rec["model"],
                    rec["provider"],
                    rec["context"],
                    rec["ts"],
                ),
            )
            self._conn.commit()