"""ticket-008: JSON-RPC 桥协议 —— 帧/解析/错误码 + TextEvent(§6) 归一化 + 优先级映射。

被测单元: df_fanyi.bridge.protocol(传输无关)。
覆盖:
1. JSON 行帧编码/解码往返;
2. 解析错误(-32700)/非法请求(-32600)/方法不存在(-32601)/参数非法(-32602);
3. TextEvent(§6) 归一化: 默认字段补齐、类型白名单(§7)、source_text 必填;
4. §6 priority(0-100 整数) → 队列 Priority(P0-P4) 分档映射;
5. 事件优先级缺失时按 §6 默认(80=IMPORTANT/公告 P2)。
"""
from __future__ import annotations

import json

import uuid

import pytest

from df_fanyi.bridge.protocol import (
    RPCError,
    TEXT_TYPES,
    decode_line,
    encode_line,
    make_error,
    make_request,
    make_result,
    normalize_event,
    priority_from_event,
)
from df_fanyi.core.queue.priority import Priority


class TestJsonLineFraming:
    """JSON Lines 帧: 每请求/响应一行(单行序列化, 供 Lua 侧 *l receive)。"""

    def test_encode_line_is_single_line_json(self) -> None:
        obj = {"jsonrpc": "2.0", "id": 1, "method": "translate", "params": {"source_text": "x"}}
        line = encode_line(obj)
        assert line.endswith("\n")
        assert line.count("\n") == 1  # 内嵌换行必须转义(默认 ensure_ascii=False 时 \n 会原样出)
        assert json.loads(line) == obj

    def test_encode_line_escapes_embedded_newlines(self) -> None:
        obj = {"result": {"translated_text": "第一行\n第二行"}}
        line = encode_line(obj)
        assert json.loads(line)["result"]["translated_text"] == "第一行\n第二行"

    def test_decode_line_roundtrip(self) -> None:
        obj = {"jsonrpc": "2.0", "id": 7, "result": {"engine": "ok"}}
        decoded, err = decode_line(encode_line(obj))
        assert err is None
        assert decoded == obj

    def test_decode_invalid_json_returns_parse_error(self) -> None:
        _, err = decode_line("{not json\n")
        assert err is not None
        assert err["error"]["code"] == -32700

    def test_request_result_error_shapes(self) -> None:
        req = make_request("translate", {"source_text": "Urist cancels job."}, rid=3)
        assert req == {"jsonrpc": "2.0", "id": 3, "method": "translate", "params": {"source_text": "Urist cancels job."}}
        res = make_result(3, {"status": "queued"})
        assert res == {"jsonrpc": "2.0", "id": 3, "result": {"status": "queued"}}
        perr = make_error(-32700, "parse error", rid=None)
        assert perr["error"]["code"] == -32700
        assert perr["id"] is None


class TestNormalizeEvent:
    """TextEvent(§6) 归一化: 字段齐全、类型白名单(§7)、必填校验。"""

    def test_full_event_passthrough(self) -> None:
        ev = {
            "event_id": "evt-1",
            "timestamp": 1234,
            "screen": "announcement",
            "source_text": "Urist cancels Make Wooden Barrel.",
            "text_type": "ANNOUNCEMENT",
            "priority": 80,
            "context_id": "report-42",
            "markup": [],
            "variables": [],
            "source_hash": "abc123",
            "game_version": "53.16",
        }
        out = normalize_event(ev)
        assert out == ev

    def test_missing_field_defaults_filled(self) -> None:
        out = normalize_event({"source_text": "hello"})
        assert uuid.UUID(out["event_id"])  # §6: event_id 为 uuid, 服务端兜底生成
        assert isinstance(out["timestamp"], int)
        assert out["screen"] == "unknown"
        assert out["text_type"] == "UNKNOWN"
        assert out["priority"] == 80  # §6 示例默认
        assert out["markup"] == []
        assert out["variables"] == []
        assert out["source_hash"] == ""

    def test_upper_text_type_normalized(self) -> None:
        out = normalize_event({"source_text": "x", "text_type": "announcement"})
        assert out["text_type"] == "ANNOUNCEMENT"

    def test_text_type_whitelist_any_of_section7(self) -> None:
        for tt in TEXT_TYPES:
            assert normalize_event({"source_text": "x", "text_type": tt})["text_type"] == tt

    def test_invalid_text_type_falls_back_unknown(self) -> None:
        out = normalize_event({"source_text": "x", "text_type": "bogus-type"})
        assert out["text_type"] == "UNKNOWN"

    def test_missing_source_text_rejected(self) -> None:
        with pytest.raises(RPCError) as ei:
            normalize_event({})
        assert ei.value.code == -32602

    def test_empty_source_text_rejected(self) -> None:
        with pytest.raises(RPCError):
            normalize_event({"source_text": "   "})

    def test_source_text_trimmed(self) -> None:
        out = normalize_event({"source_text": "  hello world  "})
        assert out["source_text"] == "hello world"

    def test_bad_priority_coerced_to_int(self) -> None:
        assert normalize_event({"source_text": "x", "priority": "70"})["priority"] == 70
        with pytest.raises(RPCError):
            normalize_event({"source_text": "x", "priority": "abc"})


class TestPriorityMapping:
    """§6 priority(0-100) → 队列 Priority 分档(公告=80 → P2 IMPORTANT)。"""

    def test_bands(self) -> None:
        cases = [
            (100, Priority.REALTIME),
            (95, Priority.REALTIME),
            (94, Priority.IMPORTANT),
            (80, Priority.IMPORTANT),  # §6 公告默认档
            (70, Priority.INTERACTIVE),
            (69, Priority.BACKGROUND),
            (50, Priority.BACKGROUND),
            (49, Priority.PREFETCH),
            (0, Priority.PREFETCH),
        ]
        for score, expect in cases:
            assert priority_from_event(score) is expect, f"{score} -> {expect}"

    def test_none_defaults_to_important(self) -> None:
        assert priority_from_event(None) is Priority.IMPORTANT

    def test_out_of_range_clamped(self) -> None:
        assert priority_from_event(250) is Priority.REALTIME
        assert priority_from_event(-5) is Priority.PREFETCH