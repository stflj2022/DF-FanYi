"""SQLite 持久层(ticket-006) —— 工程书 §8 四表 + §9 L2 缓存 + §42 崩溃恢复 + §38 高频提升。

四表(§8.1-8.4):
    terminology        用户/导入术语(locked 默认 false, locked 优先)
    translation_memory L2 持久缓存(source_hash UNIQUE=§37 上下文感知哈希;
                       写回含 model/provider/confidence; usage_count 高频计数 §38)
    translation_jobs   翻译任务(§42 状态机 PENDING/RUNNING/COMPLETED/FAILED/
                       RETRY/CANCELLED/INTERRUPTED; 启动恢复 RUNNING→INTERRUPTED→重排队)
    feedback           用户反馈(§39/§40, 第一版只建表+写入, 学习/反哺 out of scope)

工程书 §8: WAL 模式(多连接读写不互相阻塞, ticket-007 worker 复用);
             第一版只使用 SQLite, 不上 PostgreSQL。
工程书 §2.4/§56: 库内不存密钥; 失败译文不写缓存(由编排器保证, 本层只提供接口)。

线程安全: sqlite3 连接 check_same_thread=False + 内部互斥锁,
         供主线程与后台 worker(§27 并发=1, ticket-007)共用。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from df_fanyi.config import REPO_ROOT, Config

logger = logging.getLogger("df_fanyi.store")

# ---- DF 颜色/格式标记剥离(2026-09-11) -------------------------------------
# 矮人要塞字符串携带游戏渲染标记: [C:r:g:b] 颜色 / [B] 粗体 / [VAR:KEY:VAL]
# / [P:n:TOKEN]。LLM 常把源文的标记原样带进译文(系统提示词“保留标记”
# 本意只是保 {COUNT} 这类变量占位符)。字幕条/overlay 是纯文本渲染, 标记不
# 剥离会直接显示成 [C:7:0:1] 乱字。字面方括号文本(如 [需要燃料])不受影响。
import re as _re

_DF_MARKUP_RE = _re.compile(r"\[(?:C:\d+:\d+:\d+|B|VAR:[^\[\]]*|P:\d+(?::[^\[\]]*)*)\]")


def strip_df_markup(text: str | None) -> str | None:
    """剥离 DF 颜色/格式标记, 保留字面方括号文本。None 原样回。"""
    if not text:
        return text
    return _DF_MARKUP_RE.sub("", text)


class JobStatus:
    """工程书 §42 job 状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY = "RETRY"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


# §38: usage_count > 10 视为高频文本 → 启动预热 L1 常驻
FREQUENT_THRESHOLD = 10


# 工程书 §8.1-8.4 建表语句(列与工程书逐字一致)
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terminology (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    category TEXT,
    priority INTEGER DEFAULT 100,
    locked INTEGER DEFAULT 0,
    source_type TEXT,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS translation_memory (
    id INTEGER PRIMARY KEY,

    source_hash TEXT UNIQUE NOT NULL,

    source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL,

    context_hash TEXT,

    model TEXT,
    provider TEXT,

    quality_score REAL,
    confidence REAL,

    usage_count INTEGER DEFAULT 0,

    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS translation_jobs (
    id TEXT PRIMARY KEY,

    source_hash TEXT NOT NULL,

    status TEXT NOT NULL,

    priority INTEGER DEFAULT 50,

    source_text TEXT NOT NULL,

    context_json TEXT,

    attempts INTEGER DEFAULT 0,

    selected_model TEXT,

    provider TEXT,

    created_at INTEGER,
    started_at INTEGER,
    completed_at INTEGER,

    error TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,

    source_hash TEXT NOT NULL,

    original TEXT NOT NULL,

    machine_translation TEXT,

    user_translation TEXT,

    action TEXT,

    created_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON translation_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON translation_jobs(source_hash);
CREATE INDEX IF NOT EXISTS idx_tm_usage ON translation_memory(usage_count);
CREATE INDEX IF NOT EXISTS idx_terms_source ON terminology(source);
"""


def resolve_store_path(cfg: Config) -> Path:
    """库文件路径: cache.l2_path 可覆盖, 相对路径解析到仓库根(与日志同规则)。"""
    raw = cfg.cache.get("l2_path", "data/engine.db")
    path = Path(str(raw))
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def store_from_config(cfg: Config) -> "SQLiteStore":
    """按配置打开持久层(线上入口; 测试一律显式传临时路径)。"""
    return SQLiteStore(resolve_store_path(cfg))


class SQLiteStore:
    """四表 DAO, 线程安全, WAL 模式。库文件路径必须显式给定, 绝不隐式落用户目录。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")  # §8: WAL 模式
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    # ---- 基础 --------------------------------------------------------------

    def raw_exec(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """执行查询并返回 dict 行(测试/审计用; 写语句返回空列表)。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            if cur.description is None:
                self._conn.commit()
            return rows

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _now() -> int:
        return int(time.time())

    # ---- §8.1 terminology ----------------------------------------------------

    def term_add(
        self,
        source: str,
        target: str,
        *,
        category: str | None = None,
        priority: int = 100,
        locked: bool = False,  # 验收: locked 默认 false
        source_type: str | None = None,
    ) -> None:
        """有则更新, 无则插入(CLI 重复 add 不产生重复词条)。"""
        source = (source or "").strip()
        target = (target or "").strip()
        if not source or not target:
            raise ValueError("term add 需要非空 source/target")
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM terminology WHERE lower(source)=lower(?)", (source,)
            ).fetchone()
            if row:
                self._conn.execute(
                    """UPDATE terminology SET target=?, category=?, priority=?,
                       locked=?, source_type=?, updated_at=? WHERE id=?""",
                    (target, category, priority, int(locked), source_type, now, row["id"]),
                )
            else:
                self._conn.execute(
                    """INSERT INTO terminology
                       (source, target, category, priority, locked, source_type, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (source, target, category, priority, int(locked), source_type, now, now),
                )
            self._conn.commit()

    def term_list(
        self,
        *,
        category: str | None = None,
        locked_only: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM terminology"
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append("category=?")
            params.append(category)
        if locked_only:
            clauses.append("locked=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority, source"
        return self.raw_exec(sql, tuple(params))

    def term_lookup(self, text: str) -> dict[str, Any] | None:
        """整句精确匹配(与内存 Terminology 同语义): 大小写/首尾空白不敏感, locked 优先。"""
        key = " ".join((text or "").split())
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM terminology WHERE lower(source)=lower(?)
                   ORDER BY locked DESC, priority ASC, id ASC LIMIT 1""",
                (key,),
            ).fetchone()
            return dict(row) if row else None

    # ---- §8.2 translation_memory(§9 L2 / §38 高频提升) ------------------------

    def tm_upsert(
        self,
        source_hash: str,
        source_text: str,
        translated_text: str,
        *,
        context_hash: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        quality_score: float | None = None,
        confidence: float | None = None,
    ) -> None:
        """写回译文: 新行 usage_count=1; 同 hash 更新译文/元数据但不重置计数(§38)。

        translated_text 落库前剥离 DF 颜色/格式标记([C:r:g:b]/[B]/[VAR:..]/
        [P:..])—— LLM 常把源文的游戏标记原样带进译文(提示词“保留标记”
        本意只是保 {COUNT} 变量), 字幕条是纯文本 overlay, 标记不剥离会
        显示成 [C:7:0:1] 乱字(2026-09-11 实锤)。字面方括号(如 [需要燃料])
        不受影响。
        """
        translated_text = strip_df_markup(translated_text)
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM translation_memory WHERE source_hash=?", (source_hash,)
            ).fetchone()
            if row:
                self._conn.execute(
                    """UPDATE translation_memory SET source_text=?, translated_text=?,
                       context_hash=?, model=?, provider=?, quality_score=?, confidence=?,
                       updated_at=? WHERE id=?""",
                    (
                        source_text, translated_text, context_hash, model, provider,
                        quality_score, confidence, now, row["id"],
                    ),
                )
            else:
                self._conn.execute(
                    """INSERT INTO translation_memory
                       (source_hash, source_text, translated_text, context_hash,
                        model, provider, quality_score, confidence, usage_count,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                    (
                        source_hash, source_text, translated_text, context_hash,
                        model, provider, quality_score, confidence, now, now,
                    ),
                )
            self._conn.commit()

    def tm_lookup(self, source_hash: str) -> dict[str, Any] | None:
        """L2 查询: 命中返回完整行(含 model/provider/confidence/usage_count)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM translation_memory WHERE source_hash=?", (source_hash,)
            ).fetchone()
            return dict(row) if row else None

    def tm_bump_usage(self, source_hash: str, delta: int = 1) -> int:
        """命中计数 +delta(§38); 无此行 → 0(no-op)。返回新计数。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE translation_memory SET usage_count=usage_count+?, updated_at=? WHERE source_hash=?",
                (delta, self._now(), source_hash),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return 0
            row = self._conn.execute(
                "SELECT usage_count FROM translation_memory WHERE source_hash=?", (source_hash,)
            ).fetchone()
            return int(row["usage_count"])

    def tm_frequent(self, threshold: int = FREQUENT_THRESHOLD) -> list[dict[str, Any]]:
        """§38: usage_count > threshold 的高频文本(严格大于)。"""
        return self.raw_exec(
            "SELECT * FROM translation_memory WHERE usage_count>? ORDER BY usage_count DESC",
            (threshold,),
        )

    def tm_all(self) -> list[dict[str, Any]]:
        """全部 TM 行(审计/测试用)。"""
        return self.raw_exec("SELECT * FROM translation_memory ORDER BY id")

    def tm_count(self) -> int:
        return int(self.raw_exec("SELECT COUNT(*) AS n FROM translation_memory")[0]["n"])

    # ---- §8.3 translation_jobs(§42 崩溃恢复) ----------------------------------

    @staticmethod
    def _new_job_id() -> str:
        return uuid.uuid4().hex

    def job_enqueue(
        self,
        source_hash: str,
        source_text: str,
        *,
        priority: int = 50,
        context_json: str | None = None,
        selected_model: str | None = None,
        provider: str | None = None,
        job_id: str | None = None,
    ) -> str:
        """记录任务(§42 崩溃恢复依据)。job_id 由调用方指定时使用(内存队列同 id),\n        否则自动生成。"""
        jid = job_id or self._new_job_id()
        with self._lock:
            self._conn.execute(
                """INSERT INTO translation_jobs
                   (id, source_hash, status, priority, source_text, context_json,
                    attempts, selected_model, provider, created_at)
                   VALUES (?,?,?,?,?,?,0,?,?,?)""",
                (jid, source_hash, JobStatus.PENDING, priority, source_text,
                 context_json, selected_model, provider, self._now()),
            )
            self._conn.commit()
        return jid

    def job_get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._job_get_locked(job_id)

    def _job_get_locked(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM translation_jobs WHERE id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def job_list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            return self.raw_exec("SELECT * FROM translation_jobs ORDER BY created_at")
        return self.raw_exec(
            "SELECT * FROM translation_jobs WHERE status=? ORDER BY created_at", (status,)
        )

    def job_start(self, job_id: str) -> dict[str, Any] | None:
        """PENDING → RUNNING(幂等: 非 PENDING 返回 None)。返回更新后的行。"""
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE translation_jobs SET status=?, started_at=? WHERE id=? AND status=?",
                (JobStatus.RUNNING, now, job_id, JobStatus.PENDING),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return self._job_get_locked(job_id)

    def job_complete(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE translation_jobs SET status=?, completed_at=? WHERE id=?",
                (JobStatus.COMPLETED, self._now(), job_id),
            )
            self._conn.commit()

    def job_fail(self, job_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE translation_jobs SET status=?, completed_at=?, error=? WHERE id=?",
                (JobStatus.FAILED, self._now(), error, job_id),
            )
            self._conn.commit()

    def recover_crashed(self, *, max_attempts: int = 3) -> tuple[int, int]:
        """§42 启动恢复: RUNNING → INTERRUPTED → attempts 未超限的重排队(PENDING)。

        返回 (interrupted, requeued)。中断行保留原 attempts 供审计;
        重排队 job 为全新 id、attempts=old+1(重试计数受 max_attempts 约束, 防死循环)。
        """
        now = self._now()
        with self._lock:
            running = self._conn.execute(
                "SELECT * FROM translation_jobs WHERE status=?", (JobStatus.RUNNING,)
            ).fetchall()
            interrupted = len(running)
            requeued = 0
            for row in running:
                self._conn.execute(
                    "UPDATE translation_jobs SET status=?, error=? WHERE id=?",
                    (JobStatus.INTERRUPTED, "interrupted by crash recovery", row["id"]),
                )
                if row["attempts"] < max_attempts:
                    jid = self._new_job_id()
                    self._conn.execute(
                        """INSERT INTO translation_jobs
                           (id, source_hash, status, priority, source_text, context_json,
                            attempts, selected_model, provider, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (jid, row["source_hash"], JobStatus.PENDING, row["priority"],
                         row["source_text"], row["context_json"], row["attempts"] + 1,
                         row["selected_model"], row["provider"], now),
                    )
                    requeued += 1
            self._conn.commit()
        return interrupted, requeued

    # ---- §8.4 feedback ----------------------------------------------------------

    def feedback_add(
        self,
        source_hash: str,
        original: str,
        *,
        machine_translation: str | None = None,
        user_translation: str | None = None,
        action: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO feedback
                   (source_hash, original, machine_translation, user_translation, action, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (source_hash, original, machine_translation, user_translation,
                 action, self._now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def feedback_count(self) -> int:
        return int(self.raw_exec("SELECT COUNT(*) AS n FROM feedback")[0]["n"])