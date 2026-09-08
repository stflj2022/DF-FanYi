"""ticket-011 验收: 批量云端翻译器 —— 断点续跑/503 退避/不合格句丢弃/围栏容错。

约定与 ollama_client 测试一致: 用真实录制/线格式响应回放, 不 mock 内部实现。
router_batch_real.json 录制自 Pi Model Router router/L2(glm-5.3-flash)真实响应。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.parser import normalize_text, source_hash  # noqa: E402
from df_fanyi.core.protector import Protector  # noqa: E402
from df_fanyi.pretranslate.batch import (  # noqa: E402
    BatchTranslator,
    parse_batch_content,
)
from df_fanyi.pretranslate.scanner import Segment  # noqa: E402
from df_fanyi.providers.router_client import (  # noqa: E402
    RouterChatClient,
    RouterUnavailable,
    TRANSLATION_SYSTEM_PROMPT,
)

FIXTURES = REPO / "tests" / "fixtures" / "pretranslate"

BIRD_DESC = (
    "A small blue-crested bird living in temperate woodlands, known for its harsh chirps."
)


def seg(text: str, kind: str = "description", tag: str = "DESCRIPTION") -> Segment:
    text = normalize_text(text)
    protected, _ = Protector().protect(text)
    return Segment(
        hash=source_hash(protected), text=text, kind=kind, tag=tag,
        rel_path="f.txt", line=1,
    )


def openai_response(content: Any, *, model: str = "glm-5.3-flash") -> dict:
    """OpenAI /v1/chat/completions 线格式响应(content 自动 JSON 序列化数组)。"""
    if isinstance(content, list):
        content = json.dumps(content, ensure_ascii=False)
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


class ScriptedRouter:
    """脚本化回放 transport: 断言批量提示词契约, 按脚本吐响应/异常。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, endpoint: str, payload: dict) -> dict:
        self.calls.append(payload)
        assert endpoint == "/chat/completions"
        assert payload["model"] == "router/L2"
        assert payload["stream"] is False
        assert payload["max_tokens"] == 4096  # 批量输出 + 可能的思维链预算
        # §19 契约: 系统提示 = translation_system_v1 + 批量 JSON 格式约束
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][0]["content"].startswith(TRANSLATION_SYSTEM_PROMPT)
        assert "JSON 字符串数组" in payload["messages"][0]["content"]
        assert payload["messages"][1]["role"] == "user"
        sentences = json.loads(payload["messages"][1]["content"])
        assert isinstance(sentences, list)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if item.get("expect_sentences") is not None:
            assert sentences == item["expect_sentences"], "批量提示词契约被破坏"
        content = item.get("content")
        if content is None:
            content = json.dumps(item["translations"], ensure_ascii=False)
        if item.get("fenced"):
            content = f"```json\n{content}\n```"
        resp = openai_response(content, model=item.get("model", "glm-5.3-flash"))
        if item.get("reasoning"):
            resp["choices"][0]["message"]["reasoning"] = item["reasoning"]
        return resp


def make_translator(tmp_path: Path, transport, *, sleep_log: list | None = None, **kw) -> BatchTranslator:
    client = RouterChatClient(transport=transport)
    def _sleep(seconds: float) -> None:
        if sleep_log is not None:
            sleep_log.append(seconds)
    return BatchTranslator(
        client,
        progress_path=tmp_path / "progress.json",
        sleep_fn=_sleep,
        request_interval_s=kw.pop("request_interval_s", 2.0),
        **kw,
    )


SEGMENTS = [
    seg(BIRD_DESC),
    seg("It is a large predator."),
    seg("It hunts at night!"),
    seg("blue jay", kind="name", tag="NAME"),
    seg("raven", kind="name", tag="NAME"),
]
OK_TRANSLATIONS = [
    "一种生活在温带林地的小型蓝冠鸟，以其刺耳的鸣叫声而闻名。",
    "它是一种大型捕食者。",
    "它在夜间狩猎。",
    "冠蓝鸦",
    "渡鸦",
]


class TestParseBatchContent:
    def test_plain_array(self) -> None:
        assert parse_batch_content('["甲", "乙"]', 2) == ["甲", "乙"]

    def test_fenced_json_stripped(self) -> None:
        assert parse_batch_content('```json\n["甲"]\n```', 1) == ["甲"]

    def test_fenced_with_prose(self) -> None:
        assert parse_batch_content('好的：```json\n["甲"]\n``` 以上。', 1) == ["甲"]

    def test_reasoning_field_is_not_content(self) -> None:
        # reasoning 由 RouterChatClient 剥离; content 里的思考文本不是译文数组
        with pytest.raises(ValueError):
            parse_batch_content('让我想想：["甲"', 1)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="数量不齐"):
            parse_batch_content('["甲"]', 2)

    def test_non_string_array_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_batch_content("[1, 2]", 2)

    def test_dict_translations_key_tolerated(self) -> None:
        assert parse_batch_content('{"translations": ["甲"]}', 1) == ["甲"]


class TestBatchRun:
    def test_translates_in_batches_with_alignment_and_progress(self, tmp_path) -> None:
        transport = ScriptedRouter([
            {"expect_sentences": [BIRD_DESC, "It is a large predator.", "It hunts at night!"],
             "translations": OK_TRANSLATIONS[:3]},
            {"expect_sentences": ["blue jay", "raven"],
             "translations": OK_TRANSLATIONS[3:]},
        ])
        sleeps: list[float] = []
        t = make_translator(tmp_path, transport, batch_size=3, sleep_log=sleeps)
        stats = t.run(SEGMENTS)

        assert stats.ok and stats.total == 5
        assert stats.translated == 5 and stats.dropped == 0
        assert stats.batches == 2 and stats.requests == 2 and stats.retries == 0
        # 节流: 仅批次间一次 sleep, 且 ≥ request_interval_s
        assert sleeps == [2.0]

        progress = t.load_progress()
        assert len(progress["translations"]) == 5
        entry = progress["translations"][SEGMENTS[3].hash]
        assert entry["translation"] == "冠蓝鸦"
        assert entry["kind"] == "name" and entry["tag"] == "NAME"
        assert entry["model"] == "glm-5.3-flash"
        assert 0.0 < entry["score"] <= 1.0
        assert entry["source"] == "blue jay"

    def test_real_recorded_fixture_contract(self, tmp_path) -> None:
        """真实云端录制回放: 请求句序对齐, 云端数组与输入一一对应。"""
        fixture = json.loads((FIXTURES / "router_batch_real.json").read_text(encoding="utf-8"))

        def replay(endpoint: str, payload: dict) -> dict:
            assert json.loads(payload["messages"][1]["content"]) == fixture["request_sentences"]
            assert payload["messages"][0]["content"].startswith(TRANSLATION_SYSTEM_PROMPT)
            return fixture["response"]

        t = make_translator(tmp_path, replay)
        segments = [seg(s) for s in fixture["request_sentences"]]
        stats = t.run(segments)
        assert stats.ok and stats.translated == 3
        progress = t.load_progress()
        first = progress["translations"][segments[0].hash]
        assert "蓝冠鸟" in first["translation"]  # 真实云端译文非占位
        assert first["model"] == "glm-5.3-flash"

    def test_resume_skips_already_translated(self, tmp_path) -> None:
        transport = ScriptedRouter([
            {"expect_sentences": [BIRD_DESC], "translations": [OK_TRANSLATIONS[0]]},
        ])
        t = make_translator(tmp_path, transport, batch_size=1)
        stats1 = t.run(SEGMENTS[:1])
        assert stats1.translated == 1

        # 第二轮: 换新 transport(若被调用会因空脚本而炸), 进度全命中 → 零请求
        transport2 = ScriptedRouter([])
        t2 = make_translator(tmp_path, transport2)
        stats2 = t2.run(SEGMENTS[:1])
        assert stats2.ok and stats2.total == 0 and stats2.skipped_done == 1
        assert transport2.calls == []

    def test_limit_processes_first_n_segments(self, tmp_path) -> None:
        transport = ScriptedRouter([
            {"expect_sentences": [BIRD_DESC, "It is a large predator."],
             "translations": OK_TRANSLATIONS[:2]},
        ])
        t = make_translator(tmp_path, transport, batch_size=2)
        stats = t.run(SEGMENTS, limit=2)
        assert stats.total == 2 and stats.translated == 2
        # 其余句子下轮可续(只补缺: transport 只收到剩余 3 句)
        transport2 = ScriptedRouter([
            {"expect_sentences": ["It hunts at night!", "blue jay", "raven"],
             "translations": OK_TRANSLATIONS[2:]},
        ])
        stats2 = make_translator(tmp_path, transport2, batch_size=3).run(SEGMENTS)
        assert stats2.total == 3 and stats2.translated == 3

    def test_backoff_on_router_unavailable_then_success(self, tmp_path) -> None:
        transport = ScriptedRouter([
            RouterUnavailable("HTTP 503 Service Unavailable"),
            RouterUnavailable("智谱额度窗口耗尽"),
            {"expect_sentences": [BIRD_DESC], "translations": [OK_TRANSLATIONS[0]]},
        ])
        sleeps: list[float] = []
        t = make_translator(
            tmp_path, transport, batch_size=1,
            sleep_log=sleeps, backoff_initial_s=2.0, backoff_max_s=60.0,
        )
        stats = t.run(SEGMENTS[:1])
        assert stats.ok and stats.translated == 1
        assert stats.retries == 2
        # 指数退避: 2.0 → 4.0
        assert stats.backoff_sleeps_s == [2.0, 4.0]
        assert stats.request_latencies_ms and stats.request_latencies_ms[0] >= 0

    def test_retries_exhausted_aborts_run_keeps_progress(self, tmp_path) -> None:
        transport = ScriptedRouter([
            RouterUnavailable("503"),
            RouterUnavailable("503"),
            {"translations": ["渡鸦"]},  # 第二批本可成功, 但本轮已中止
        ])
        t = make_translator(tmp_path, transport, batch_size=1, max_retries=1,
                            backoff_initial_s=1.0)
        stats = t.run(SEGMENTS[:2])
        assert not stats.ok
        assert stats.error and "续传" in stats.error
        assert stats.translated == 0 and stats.failed_batches == 1
        # 进度已保存(空但结构完整), 重跑可续
        assert t._progress_path.exists()
        stats2 = make_translator(
            tmp_path,
            ScriptedRouter([
                RouterUnavailable("503"),
                {"translations": ["一种生活在温带林地的小型蓝冠鸟，以其刺耳的鸣叫声而闻名。"]},
            ]),
            batch_size=1, max_retries=1, backoff_initial_s=1.0,
        ).run(SEGMENTS[:1])
        assert stats2.ok and stats2.translated == 1

    def test_invalid_sentence_dropped_with_reason(self, tmp_path) -> None:
        transport = ScriptedRouter([
            # 第 1 句译文信息长度比 <0.3 → §24 拒绝; 第 2 句正常
            {"translations": ["好", "它是一种大型捕食者。"]},
        ])
        t = make_translator(tmp_path, transport, batch_size=2)
        stats = t.run(SEGMENTS[:2])
        assert stats.ok
        assert stats.translated == 1 and stats.dropped == 1
        progress = t.load_progress()
        assert SEGMENTS[0].hash in progress["dropped"]
        assert "长度比" in progress["dropped"][SEGMENTS[0].hash]["reason"]
        assert SEGMENTS[1].hash in progress["translations"]
        # 丢弃句不进 translations(绝不污染 TM)
        assert SEGMENTS[0].hash not in progress["translations"]

    def test_unparseable_content_retried_then_stops(self, tmp_path) -> None:
        transport = ScriptedRouter([
            {"content": "抱歉，我无法按要求输出。"},
            {"content": "抱歉，我无法按要求输出。"},
            {"translations": OK_TRANSLATIONS[:3]},
        ])
        t = make_translator(tmp_path, transport, batch_size=3, max_retries=1,
                            backoff_initial_s=1.0)
        stats = t.run(SEGMENTS[:3])
        assert not stats.ok
        assert stats.retries == 2  # 两次请求各计一次重试

    def test_fenced_and_reasoning_tolerated(self, tmp_path) -> None:
        transport = ScriptedRouter([
            {
                "fenced": True,
                "translations": ["冠蓝鸦"],
                "reasoning": "用户要求批量翻译，我应输出 JSON 数组……",
            },
        ])
        t = make_translator(tmp_path, transport, batch_size=1)
        stats = t.run([seg("blue jay", kind="name", tag="NAME")])
        assert stats.ok and stats.translated == 1
        assert t.load_progress()["translations"][SEGMENTS[3].hash]["translation"] == "冠蓝鸦"

    def test_privacy_off_omits_source_text(self, tmp_path) -> None:
        transport = ScriptedRouter([
            {"expect_sentences": ["blue jay"], "translations": ["冠蓝鸦"]},
        ])
        t = make_translator(tmp_path, transport, store_source_text=False)
        stats = t.run([seg("blue jay", kind="name", tag="NAME")])
        assert stats.ok
        entry = t.load_progress()["translations"][SEGMENTS[3].hash]
        assert "source" not in entry
        assert entry["translation"] == "冠蓝鸦"

    def test_progress_stats_accumulate(self, tmp_path) -> None:
        transport = ScriptedRouter([
            {"translations": ["冠蓝鸦"]},
            {"translations": ["渡鸦"]},
        ])
        t = make_translator(tmp_path, transport, batch_size=1)
        t.run([seg("blue jay", kind="name", tag="NAME")])
        t.run([seg("raven", kind="name", tag="NAME")])
        pstats = t.load_progress()["stats"]
        assert pstats["requests"] == 2
        assert len(pstats["latencies_ms"]) == 2


class TestStatus:
    def test_status_empty_when_no_progress(self, tmp_path) -> None:
        t = make_translator(tmp_path, ScriptedRouter([]))
        s = t.status()
        assert s["exists"] is False and s["translated"] == 0

    def test_status_counts(self, tmp_path) -> None:
        transport = ScriptedRouter([{"translations": ["冠蓝鸦"]}])
        t = make_translator(tmp_path, transport)
        t.run([seg("blue jay", kind="name", tag="NAME")])
        s = t.status()
        assert s["exists"] is True
        assert s["translated"] == 1 and s["dropped"] == 0
        assert s["model"] == "router/L2"  # 请求别名; 实际上游模型在逐句条目里
        assert s["provider"] == "cloud-pretranslate"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
