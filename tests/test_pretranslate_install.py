"""ticket-011 验收: 翻译包装入 L2/术语层 + 引擎零 LLM 命中预翻译包。

验收标准(工单): install 后, 引擎对包内句子的查询命中 pretranslate 且
零 LLM 调用(测试为证)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.orchestrator import Orchestrator  # noqa: E402
from df_fanyi.core.parser import normalize_text, source_hash  # noqa: E402
from df_fanyi.core.protector import Protector  # noqa: E402
from df_fanyi.database.store import SQLiteStore  # noqa: E402
from df_fanyi.database.terminology import Terminology  # noqa: E402
from df_fanyi.pretranslate.batch import BatchTranslator  # noqa: E402
from df_fanyi.pretranslate.install import (  # noqa: E402
    InstallStats,
    install_from_progress,
    load_progress_file,
)
from df_fanyi.pretranslate.scanner import Segment  # noqa: E402
from df_fanyi.providers.router_client import RouterChatClient  # noqa: E402

BIRD_DESC = (
    "A small blue-crested bird living in temperate woodlands, known for its harsh chirps."
)


def seg(text: str, kind: str, tag: str) -> Segment:
    text = normalize_text(text)
    protected, _ = Protector().protect(text)
    return Segment(
        hash=source_hash(protected), text=text, kind=kind, tag=tag,
        rel_path="f.txt", line=1,
    )


def openai_resp(translations: list[str]) -> dict:
    return {
        "model": "glm-5.3-flash",
        "choices": [
            {"message": {"role": "assistant",
                         "content": __import__("json").dumps(translations, ensure_ascii=False)}}
        ],
    }


def build_package(tmp_path: Path, *, store_source_text: bool = True):
    """跑一遍真批量器(脚本回放)产出真实格式的翻译包, 返回 (包dict, segments)。"""
    segments = [
        seg(BIRD_DESC, "description", "DESCRIPTION"),
        seg("blue jay", "name", "NAME"),
        seg("raven", "name", "NAME"),
    ]
    script = [
        {"translations": [
            "一种生活在温带林地的小型蓝冠鸟，以其刺耳的鸣叫声而闻名。",
            "冠蓝鸦",
            "渡鸦",
        ]},
    ]

    calls: list = []

    def transport(endpoint: str, payload: dict) -> dict:
        calls.append(payload)
        return openai_resp(script[0]["translations"])

    client = RouterChatClient(transport=transport)
    translator = BatchTranslator(
        client,
        progress_path=tmp_path / "progress.json",
        batch_size=3,
        sleep_fn=lambda _s: None,
        store_source_text=store_source_text,
    )
    stats = translator.run(segments)
    assert stats.ok and stats.translated == 3
    return load_progress_file(tmp_path / "progress.json"), segments, calls


class TestInstall:
    def test_tm_and_locked_terms_written(self, tmp_path) -> None:
        package, segments, _ = build_package(tmp_path)
        store = SQLiteStore(tmp_path / "engine.db")
        stats = install_from_progress(package, store)

        assert stats.tm_rows == 3
        assert stats.terms_locked == 2  # blue jay / raven → locked 术语
        assert isinstance(stats, InstallStats)

        # TM 行: provider=cloud-pretranslate, confidence=验证得分
        row = store.tm_lookup(segments[0].hash)
        assert row["provider"] == "cloud-pretranslate"
        assert row["translated_text"].startswith("一种生活在温带林地")
        assert 0.0 < row["confidence"] <= 1.0
        assert row["model"] == "glm-5.3-flash"
        assert row["source_text"] == BIRD_DESC

        # 名称 → terminology locked=true; 描述不进术语
        term = store.term_lookup("blue jay")
        assert term is not None and term["target"] == "冠蓝鸦"
        assert int(term["locked"]) == 1
        assert term["source_type"] == "cloud-pretranslate"
        assert store.term_lookup(BIRD_DESC) is None
        store.close()

    def test_description_kind_not_in_terminology(self, tmp_path) -> None:
        package, _, _ = build_package(tmp_path)
        store = SQLiteStore(tmp_path / "engine.db")
        stats = install_from_progress(package, store)
        assert stats.terms_locked == 2
        store.close()

    def test_install_is_idempotent(self, tmp_path) -> None:
        package, _, _ = build_package(tmp_path)
        store = SQLiteStore(tmp_path / "engine.db")
        first = install_from_progress(package, store)
        second = install_from_progress(package, store)
        assert second.tm_rows == first.tm_rows
        assert store.tm_count() == 3
        assert len(store.term_list(locked_only=True)) == 2
        store.close()

    def test_privacy_package_names_skipped_but_tm_installed(self, tmp_path) -> None:
        package, segments, _ = build_package(tmp_path, store_source_text=False)
        assert all("source" not in e for e in package["translations"].values())
        store = SQLiteStore(tmp_path / "engine.db")
        stats = install_from_progress(package, store)
        assert stats.tm_rows == 3
        assert stats.terms_locked == 0
        assert stats.skipped_no_source == 2
        assert store.term_lookup("blue jay") is None
        # TM 行仍可命中(source_text 为空但译文在)
        row = store.tm_lookup(segments[1].hash)
        assert row["translated_text"] == "冠蓝鸦"
        store.close()


class TestEngineZeroLLM:
    def test_package_sentence_hits_l2_with_zero_llm(self, tmp_path) -> None:
        package, segments, _ = build_package(tmp_path)
        store = SQLiteStore(tmp_path / "engine.db")
        install_from_progress(package, store)

        llm_calls: list[str] = []

        def exploding_llm(text: str) -> str:
            llm_calls.append(text)
            raise AssertionError("包内句子绝不允许走到 LLM")

        orch = Orchestrator(
            exploding_llm,
            terminology=Terminology(),  # 空 seed, 隔离内置词典
            store=store,
            model_name="glm-5.3-flash",
            provider="cloud-pretranslate",
        )
        result = orch.translate(BIRD_DESC)
        assert result.cache_hit is True
        assert llm_calls == []  # 零 LLM 调用
        assert result.provider == "cloud-pretranslate"
        assert result.model == "glm-5.3-flash"
        assert result.text.startswith("一种生活在温带林地")
        assert result.confidence > 0
        store.close()

    def test_package_name_hits_terminology_with_zero_llm(self, tmp_path) -> None:
        package, _, _ = build_package(tmp_path)
        store = SQLiteStore(tmp_path / "engine.db")
        install_from_progress(package, store)

        llm_calls: list[str] = []

        def exploding_llm(text: str) -> str:
            llm_calls.append(text)
            raise AssertionError("包内名称绝不允许走到 LLM")

        orch = Orchestrator(
            exploding_llm,
            terminology=Terminology(),
            store=store,
        )
        result = orch.translate("blue jay")
        assert llm_calls == []
        assert result.text == "冠蓝鸦"
        # 包同时装入 TM 与术语层: L2 TM 先命中(缓存链在词典之前, §11);
        # 若 TM 未装则术语层同样命中 —— 两条路径都零 LLM。
        assert result.confidence > 0
        assert result.provider in {"cloud-pretranslate", "local"}
        store.close()

    def test_unpackaged_sentence_falls_through_to_llm(self, tmp_path) -> None:
        """包外句子不受影响: 正常下探 LLM(此处用记录型假 LLM 证明可达)。"""
        package, _, _ = build_package(tmp_path)
        store = SQLiteStore(tmp_path / "engine.db")
        install_from_progress(package, store)

        llm_calls: list[str] = []
        orch = Orchestrator(
            lambda text: (llm_calls.append(text) or "它是一个测试句。"),
            terminology=Terminology(),
            store=store,
        )
        result = orch.translate("It is a test sentence.")
        assert llm_calls == ["It is a test sentence."]
        assert result.text == "它是一个测试句。"
        store.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
