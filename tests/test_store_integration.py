"""ticket-006 验收: L2 SQLite 持久层与编排器集成(§9 两级缓存 / §38 高频提升 / §42 崩溃恢复)。

验收标准:
- [x] 同句第二次翻译 cache_hit=true 且不调 LLM(假 LLM 计数为证) —— 跨进程(L2 命中)
- [ ] usage_count 随命中增长, >10 高频文本启动预热 L1 常驻(§38)
- [ ] 内核集成: L2 查询(cache_hit 标记)、译文写回(model/provider/confidence)、
      高频提升; 失败回退不写 L2(§56 禁止无效缓存污染)

只测外部行为(translate 返回值 + store 查询), 不 mock 内部实现。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.orchestrator import Orchestrator  # noqa: E402
from df_fanyi.database.store import SQLiteStore  # noqa: E402

SENTENCE = "Stray dog has given birth to puppies."
REPLY = "流浪狗生下了一窝小狗。"


class CountingLLM:
    """假 LLM: 计数每次调用(验收: L2 命中不再调 LLM)。"""

    def __init__(self, reply: str = REPLY) -> None:
        self.calls = 0
        self.reply = reply

    def __call__(self, text: str) -> str:
        self.calls += 1
        return self.reply


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "engine.db"


# --- 验收 1: 同句第二次翻译 cache_hit=true 且不调 LLM -----------------------

def test_second_translation_hits_l2_without_llm(db: Path) -> None:
    """第一次翻译调 LLM 写 L2; 第二次(新进程模拟: 新 Orchestrator + 同库)
    直接 L2 命中, cache_hit=true, LLM 调用数为 0。"""
    s1 = SQLiteStore(db)
    llm1 = CountingLLM()
    orch1 = Orchestrator(llm=llm1, store=s1, model_name="fake-e2e")
    first = orch1.translate(SENTENCE)
    assert llm1.calls == 1
    assert first.cache_hit is False
    s1.close()  # 模拟进程退出

    s2 = SQLiteStore(db)  # 重启: 重新打开同一库
    try:
        llm2 = CountingLLM()
        orch2 = Orchestrator(llm=llm2, store=s2, model_name="fake-e2e")
        second = orch2.translate(SENTENCE)
        assert second.cache_hit is True
        assert llm2.calls == 0, "L2 命中不得再调 LLM"
        assert second.text == first.text
        assert second.model == "fake-e2e"  # 从 L2 行恢复 model/provider/confidence
        assert second.confidence == first.confidence
    finally:
        s2.close()


def test_l1_hit_within_process_also_bumps_usage(db: Path) -> None:
    """L1 命中(同进程第二次)同样写穿计数: L2 usage_count 随命中增长(§38 计数器)。"""
    s = SQLiteStore(db)
    try:
        llm = CountingLLM()
        orch = Orchestrator(llm=llm, store=s)
        orch.translate(SENTENCE)
        assert s.tm_all()[0]["usage_count"] == 1, "首次写回 usage_count=1"
        orch.translate(SENTENCE)  # L1 命中
        assert s.tm_all()[0]["usage_count"] == 2
        assert llm.calls == 1, "L1 命中不得再调 LLM"
    finally:
        s.close()


# --- 验收 2: 高频提升(§38) -------------------------------------------------

def test_frequent_entries_warm_l1_on_startup(db: Path) -> None:
    """usage_count > 10 → 启动预热 L1(常驻): 重启后首次翻译即 L1 命中。"""
    s1 = SQLiteStore(db)
    llm = CountingLLM()
    orch = Orchestrator(llm=llm, store=s1)
    orch.translate(SENTENCE)  # 写 L2, usage=1
    for _ in range(10):
        orch.translate(SENTENCE)  # 10 次 L1 命中, usage → 11
    assert s1.tm_all()[0]["usage_count"] == 11
    s1.close()

    s2 = SQLiteStore(db)
    try:
        orch2 = Orchestrator(llm=CountingLLM(), store=s2)
        warmed = orch2.warm_l1(threshold=10)
        assert warmed >= 1, "高频文本应被预热进 L1"
        r = orch2.translate(SENTENCE)  # 预热后首次翻译应直接 L1 命中
        assert r.cache_hit is True
        assert r.text == REPLY
    finally:
        s2.close()


# --- 内核集成: 写回 / 不写污染 ----------------------------------------------

def test_success_writeback_includes_metadata(db: Path) -> None:
    """译文写回含 model/provider/confidence(§8.2 列)。"""
    s = SQLiteStore(db)
    try:
        orch = Orchestrator(llm=CountingLLM(), store=s, model_name="gemma-e2e", provider="ollama")
        orch.translate(SENTENCE)
        row = s.tm_all()[0]
        assert row["model"] == "gemma-e2e"
        assert row["provider"] == "ollama"
        assert row["confidence"] > 0.0
        assert row["source_text"] == SENTENCE
    finally:
        s.close()


def test_dictionary_hit_is_persisted_to_l2(db: Path) -> None:
    """词典/规则命中同样写回 L2(持久缓存, §9)。"""
    s = SQLiteStore(db)
    try:
        orch = Orchestrator(store=s)
        r = orch.translate("Dwarf")
        assert r.model == "dictionary"
        rows = s.tm_all()
        assert len(rows) == 1
        assert rows[0]["model"] == "dictionary"
        assert rows[0]["translated_text"] == "矮人"
    finally:
        s.close()


def test_failure_fallback_not_written_to_l2(db: Path) -> None:
    """§56: LLM 失败回退原文 → 不写 L2(避免无效缓存污染)。"""
    s = SQLiteStore(db)

    def boom(text: str) -> str:
        raise RuntimeError("llm down")

    try:
        orch = Orchestrator(llm=boom, store=s)
        r = orch.translate(SENTENCE)
        assert r.text == SENTENCE  # 回退原文
        assert r.confidence == 0.0
        assert s.tm_all() == [], "失败回退不得写入 L2"
    finally:
        s.close()


# --- 内核集成: 术语表(§8.1)与 CLI term add 联动 -----------------------------

def test_store_terminology_used_by_orchestrator(db: Path) -> None:
    """term add 的词条立即参与管线(整句精确匹配 → dictionary 命中, 零 LLM)。"""
    s = SQLiteStore(db)
    try:
        s.term_add("Goblin", "哥布林", locked=True)
        llm = CountingLLM()
        orch = Orchestrator(llm=llm, store=s)
        r = orch.translate("Goblin")
        assert r.text == "哥布林"
        assert r.model == "dictionary"
        assert r.confidence == 1.0
        assert llm.calls == 0, "术语命中不得调 LLM"
    finally:
        s.close()


def test_orchestrator_without_store_still_works() -> None:
    """store=None(离线/无持久层)保持 ticket-004/005 全行为。"""
    orch = Orchestrator(llm=CountingLLM())
    r = orch.translate(SENTENCE)
    assert r.text == REPLY
    assert r.cache_hit is False