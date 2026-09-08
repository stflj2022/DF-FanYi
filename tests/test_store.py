"""ticket-006 验收: SQLite 持久层(工程书 §8 四表 / §9 L2 / §42 崩溃恢复 / §38 高频提升)。

验收标准:
- [ ] 四表(terminology/translation_memory/translation_jobs/feedback)建库, WAL 模式
- [ ] DAO 层: 术语增改查(locked 默认 false)、TM 查询/写回/usage_count、任务生命周期
- [ ] 崩溃恢复: RUNNING → INTERRUPTED → 重排队(PENDING, attempts+1, 受 max_attempts 约束)
- [ ] 测试一律用 tmp 临时库, 绝不落用户目录

只测外部行为(建库后查询/读写结果), 不 mock。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.parser import source_hash  # noqa: E402
from df_fanyi.database.store import JobStatus, SQLiteStore  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    """每个测试独立临时库(测试用时临时库, 不落用户目录)。"""
    s = SQLiteStore(tmp_path / "engine.db")
    yield s
    s.close()


# --- §8 建库 --------------------------------------------------------------

def test_four_tables_created(store: SQLiteStore) -> None:
    """工程书 §8.1-8.4 四表必须全部存在。"""
    rows = store.raw_exec("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    names = {r["name"] for r in rows}
    assert {"terminology", "translation_memory", "translation_jobs", "feedback"} <= names


def test_schema_columns_match_spec(store: SQLiteStore) -> None:
    """§8.1-8.4 建表列与工程书逐字一致。"""
    expected = {
        "terminology": {"id", "source", "target", "category", "priority", "locked", "source_type", "created_at", "updated_at"},
        "translation_memory": {"id", "source_hash", "source_text", "translated_text", "context_hash", "model", "provider", "quality_score", "confidence", "usage_count", "created_at", "updated_at"},
        "translation_jobs": {"id", "source_hash", "status", "priority", "source_text", "context_json", "attempts", "selected_model", "provider", "created_at", "started_at", "completed_at", "error"},
        "feedback": {"id", "source_hash", "original", "machine_translation", "user_translation", "action", "created_at"},
    }
    for table, cols in expected.items():
        rows = store.raw_exec(f"PRAGMA table_info({table})")
        actual = {r["name"] for r in rows}
        assert actual == cols, f"{table} 列与 §8 不符: {actual ^ cols}"


def test_wal_mode_enabled(store: SQLiteStore) -> None:
    """工程书 §8: WAL 模式(并发读写不互相阻塞, ticket-007 起多 worker)。"""
    rows = store.raw_exec("PRAGMA journal_mode")
    assert rows[0]["journal_mode"].lower() == "wal"


def test_tm_source_hash_unique(store: SQLiteStore) -> None:
    """§8.2: source_hash TEXT UNIQUE NOT NULL —— 写回同 hash 是更新不是重复插入。"""
    store.tm_upsert("h1", "a", "甲", model="m", provider="p", confidence=1.0)
    store.tm_upsert("h1", "a", "乙", model="m", provider="p", confidence=1.0)
    assert store.tm_count() == 1
    assert store.tm_lookup("h1")["translated_text"] == "乙"


# --- §8.1 terminology DAO ---------------------------------------------------

def test_term_add_defaults_locked_false(store: SQLiteStore) -> None:
    """验收: term add locked 默认 false。"""
    store.term_add("Goblin", "哥布林")
    rows = store.term_list()
    assert len(rows) == 1
    assert rows[0]["locked"] == 0
    assert rows[0]["priority"] == 100


def test_term_add_locked_and_priority(store: SQLiteStore) -> None:
    store.term_add("Goblin", "哥布林", locked=True, priority=10, category="creature")
    row = store.term_list()[0]
    assert row["locked"] == 1
    assert row["priority"] == 10
    assert row["category"] == "creature"


def test_term_add_same_source_updates(store: SQLiteStore) -> None:
    """add 语义 = 有则更新, 无则插入(CLI 重复 add 不产生重复词条)。"""
    store.term_add("Goblin", "哥布林")
    store.term_add("Goblin", "地精", locked=True)
    rows = store.term_list()
    assert len(rows) == 1
    assert rows[0]["target"] == "地精"
    assert rows[0]["locked"] == 1


def test_term_list_filters(store: SQLiteStore) -> None:
    store.term_add("Goblin", "哥布林", category="creature")
    store.term_add("Dwarf", "矮人", category="race")
    store.term_add("Apple", "苹果", category="plant", locked=True)
    assert len(store.term_list()) == 3
    assert len(store.term_list(category="creature")) == 1
    assert {r["source"] for r in store.term_list(locked_only=True)} == {"Apple"}


def test_term_lookup_case_insensitive_locked_first(store: SQLiteStore) -> None:
    """与内存 Terminology 一致: 大小写不敏感, locked 优先。"""
    store.term_add("goblin", "地精")
    store.term_add("Goblin", "哥布林", locked=True)
    row = store.term_lookup("GOBLIN")
    assert row is not None
    assert row["target"] == "哥布林"
    assert store.term_lookup("  Goblin  ")["target"] == "哥布林"
    assert store.term_lookup("Orc") is None


# --- §8.2 translation_memory DAO(§9 L2 / §38 高频提升) -----------------------

def test_tm_upsert_inserts_usage_one(store: SQLiteStore) -> None:
    store.tm_upsert("h2", "text", "译文", model="m", provider="p", confidence=0.9)
    row = store.tm_lookup("h2")
    assert row["usage_count"] == 1
    assert row["confidence"] == pytest.approx(0.9)


def test_tm_upsert_reupdates_keeps_usage(store: SQLiteStore) -> None:
    """同 hash 写回: 更新译文/元数据, usage_count 不重置(§38 计数积累)。"""
    store.tm_upsert("h3", "text", "旧译", model="m", provider="p", confidence=0.5)
    store.tm_bump_usage("h3")
    store.tm_upsert("h3", "text", "新译", model="m2", provider="p", confidence=0.99)
    row = store.tm_lookup("h3")
    assert row["translated_text"] == "新译"
    assert row["model"] == "m2"
    assert row["usage_count"] == 2


def test_tm_bump_usage_increments(store: SQLiteStore) -> None:
    store.tm_upsert("h4", "text", "译文", model="m", provider="p", confidence=1.0)
    assert store.tm_bump_usage("h4") == 2
    assert store.tm_bump_usage("h4") == 3
    assert store.tm_lookup("h4")["usage_count"] == 3


def test_tm_bump_missing_hash_is_noop(store: SQLiteStore) -> None:
    assert store.tm_bump_usage("nope") == 0


def test_tm_frequent_strictly_above_threshold(store: SQLiteStore) -> None:
    """工程书 §38: usage_count > 10 才算高频(==10 不算)。"""
    store.tm_upsert("lo", "text", "译文", model="m", provider="p", confidence=1.0)
    for _ in range(9):
        store.tm_bump_usage("lo")  # → 10
    assert store.tm_frequent(threshold=10) == []

    store.tm_upsert("hi", "text2", "译文2", model="m", provider="p", confidence=1.0)
    for _ in range(10):
        store.tm_bump_usage("hi")  # → 11
    freq = store.tm_frequent(threshold=10)
    assert [r["source_hash"] for r in freq] == ["hi"]


# --- §8.3 translation_jobs DAO(§42 崩溃恢复) ---------------------------------

def test_job_enqueue_pending(store: SQLiteStore) -> None:
    sha = source_hash("text")
    jid = store.job_enqueue(sha, "text", priority=80)
    row = store.job_get(jid)
    assert row["status"] == JobStatus.PENDING
    assert row["attempts"] == 0
    assert row["priority"] == 80
    assert row["source_hash"] == sha


def test_job_lifecycle_pending_running_completed(store: SQLiteStore) -> None:
    jid = store.job_enqueue(source_hash("text"), "text")
    assert store.job_start(jid)["status"] == JobStatus.RUNNING
    store.job_complete(jid)
    assert store.job_get(jid)["status"] == JobStatus.COMPLETED
    assert store.job_get(jid)["completed_at"] is not None


def test_job_start_only_from_pending(store: SQLiteStore) -> None:
    jid = store.job_enqueue(source_hash("text"), "text")
    store.job_start(jid)
    assert store.job_start(jid) is None, "RUNNING 不能再 start"


def test_job_fail_records_error(store: SQLiteStore) -> None:
    jid = store.job_enqueue(source_hash("text"), "text")
    store.job_start(jid)
    store.job_fail(jid, "llm down")
    row = store.job_get(jid)
    assert row["status"] == JobStatus.FAILED
    assert row["error"] == "llm down"


def test_recover_crashed_marks_interrupted_and_requeues(tmp_path: Path) -> None:
    """验收: 杀进程模拟崩溃后重启 → RUNNING job 被 INTERRUPTED 并重排队。"""
    db = tmp_path / "jobs.db"
    s1 = SQLiteStore(db)
    sha = source_hash("text")
    jid = s1.job_enqueue(sha, "text")
    s1.job_start(jid)
    s1.close()  # 模拟崩溃: 未 complete/fail 直接关闭连接

    s2 = SQLiteStore(db)  # 重启: 重新打开同一库文件
    try:
        interrupted, requeued = s2.recover_crashed()
        assert interrupted == 1
        assert requeued == 1

        old = s2.job_get(jid)
        assert old["status"] == JobStatus.INTERRUPTED
        assert old["attempts"] == 0  # §42: 中断行保留原 attempts(审计)

        pending = [r for r in s2.job_list(status=JobStatus.PENDING) if r["source_hash"] == sha]
        assert len(pending) == 1, "应产生一条重排队的 PENDING job"
        new_job = pending[0]
        assert new_job["id"] != jid
        assert new_job["attempts"] == 1
        assert new_job["source_text"] == "text"

        # 重排队后的 job 可以正常走完整生命周期(重试成功)
        assert s2.job_start(new_job["id"]) is not None
        s2.job_complete(new_job["id"])
        assert s2.job_get(new_job["id"])["status"] == JobStatus.COMPLETED
    finally:
        s2.close()


def test_recover_crashed_respects_max_attempts(tmp_path: Path) -> None:
    """§42: attempts 达到 max_attempts 后不再重排队(防死循环)。"""
    db = tmp_path / "jobs2.db"

    s = SQLiteStore(db)
    sha = source_hash("text")
    jid = s.job_enqueue(sha, "text")
    try:
        for expected_requeue in (1, 1, 0):  # attempts 0→1→2, 第 3 轮 2=2 不再重排队
            s.job_start(jid)
            interrupted, requeued = s.recover_crashed(max_attempts=2)
            assert (interrupted, requeued) == (1, expected_requeue)
            pending = [r for r in s.job_list(status=JobStatus.PENDING) if r["source_hash"] == sha]
            jid = pending[-1]["id"] if pending else jid
        assert s.job_get(jid)["attempts"] == 2
    finally:
        s.close()


def test_recover_crashed_no_running(store: SQLiteStore) -> None:
    assert store.recover_crashed() == (0, 0)


# --- §8.4 feedback DAO -------------------------------------------------------

def test_feedback_add_roundtrip(store: SQLiteStore) -> None:
    sha = source_hash("text")
    fid = store.feedback_add(
        sha,
        "text",
        machine_translation="译文",
        user_translation="修正",
        action="accept",
    )
    assert isinstance(fid, int)
    assert store.feedback_count() == 1
    rows = store.raw_exec("SELECT * FROM feedback")
    assert rows[0]["action"] == "accept"
    assert rows[0]["user_translation"] == "修正"


def test_store_requires_explicit_path_not_home(tmp_path: Path) -> None:
    """验收: 库文件路径必须显式给定/配置, 绝不隐式落用户目录。"""
    db = tmp_path / "sub" / "engine.db"
    s = SQLiteStore(db)
    s.close()
    assert db.exists()