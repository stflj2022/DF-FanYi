"""固化调用方式的测试: 用真实录制的 ollama 响应回放, 不依赖本机服务。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from providers.ollama_client import (  # noqa: E402
    OllamaChatClient,
    OllamaEmptyResponse,
    OllamaUnavailable,
    TRANSLATION_SYSTEM_PROMPT,
)

FIXTURES = REPO / "tests" / "fixtures" / "ollama"


def replay_transport(fixture_name: str) -> Callable:
    """录制回放 transport: 从 tests/fixtures/ollama/ 读取真实录制响应。"""

    def _call(endpoint: str, payload: dict) -> dict:
        return json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))

    return _call


def _fixture(name: str) -> dict:
    return json.loads((REPO / "tests" / "fixtures" / "ollama" / name).read_text())


def _recording_transport(fixture: dict, calls: list):
    def _call(endpoint: str, payload: dict) -> dict:
        calls.append((endpoint, payload))
        return fixture

    return _call


class TestBuildPayload:
    def test_messages_format_with_finalized_system_prompt(self) -> None:
        client = OllamaChatClient()
        payload = client.build_payload("Urist cancels Make Wooden Barrel.")
        assert payload["model"] == "gemma-4b-trans"
        assert payload["stream"] is False
        roles = [m["role"] for m in payload["messages"]]
        assert roles == ["system", "user"]
        assert payload["messages"][0]["content"] == TRANSLATION_SYSTEM_PROMPT
        assert payload["messages"][1]["content"] == "Urist cancels Make Wooden Barrel."
        assert payload["options"] == {"temperature": 0.3, "num_predict": 256}

    def test_system_prompt_forbids_extra_commentary(self) -> None:
        """实测 /api/generate 无系统提示会附带英文注释, 系统提示必须禁止。"""
        assert "只输出译文" in TRANSLATION_SYSTEM_PROMPT


class TestChatReplay:
    def test_success_short_sentence(self) -> None:
        fixture = _fixture("chat_success.json")
        calls: list = []
        client = OllamaChatClient(transport=_recording_transport(fixture, calls))

        result = client.chat("Urist cancels Make Wooden Barrel: needs barrel.")

        assert result.content == fixture["message"]["content"]
        assert "木桶" in result.content
        # 调用形状: 走 /api/chat, messages 格式
        assert calls[0][0] == "chat"
        assert calls[0][1]["messages"][0]["role"] == "system"
        # 统计数据透传
        assert result.eval_count == fixture["eval_count"]
        assert result.tokens_per_second == pytest.approx(
            fixture["eval_count"] / fixture["eval_duration"] * 1e9
        )
        assert result.is_loaded is True  # 热载

    def test_cold_load_detected(self) -> None:
        fixture = _fixture("chat_cold_load.json")
        client = OllamaChatClient(transport=_recording_transport(fixture, []))
        result = client.chat("Stray dog has given birth to puppies.")
        assert result.is_loaded is False
        assert result.load_duration_ns == fixture["load_duration"]

    def test_empty_content_raises(self) -> None:
        """曾出现的空响应必须显式报错, 由上层回退原文(绝不静默)."""
        client = OllamaChatClient(
            transport=_recording_transport(
                {"model": "gemma-4b-trans", "message": {"role": "assistant", "content": ""}},
                [],
            )
        )
        with pytest.raises(OllamaEmptyResponse):
            client.chat("anything")

    def test_transport_failure_raises_unavailable(self) -> None:
        def _boom(endpoint: str, payload: dict) -> dict:
            raise ConnectionError("connection refused")

        client = OllamaChatClient(transport=_boom)
        with pytest.raises(OllamaUnavailable):
            client.chat("anything")


class TestReplayTransport:
    def test_replays_recorded_fixture(self) -> None:
        client = OllamaChatClient(transport=replay_transport("chat_success.json"))
        result = client.chat("Urist cancels Make Wooden Barrel.")
        assert "木桶" in result.content
