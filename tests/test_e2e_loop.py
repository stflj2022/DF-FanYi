"""ticket-009: 引擎侧端到端 —— 真 socket BridgeServer + 录制回放假 LLM。

复现 Lua 桥(fanyi.lua)的完整消费语义(真回环, 无 mock):
1. 同步快路径: 词典整句命中("dwarf") → 立即 done, LLM 计数为 0(ticket-005 §11);
2. 异步长句: submit → queued(原文占位 §29) → 轮询 fetch_done 取回译文
   (fanyi.lua drain 语义: confidence>0 且译文非空才上屏, failed 静默原文 §2.3);
3. LLM 挂: 词典仍走快路径(兜底不依赖 LLM); 长句 → failed 记录(translated_text="")
4. 引擎重启恢复: 停 A → 起 B, 协议/翻译恢复(游戏侧 Lua 指数退避重连在
   tests/lua/run_fanyi_tests.lua reconnect 场景覆盖);
5. 离线(无任何 LLM, 断网极端): 词典快路径照常; 长句 failed → 静默原文;
6. Lua 线上格式镜像: 按 fanyi.lua rpc_request 的**逐字段**请求(含 source_hash/
   markup/variables/优先级档)打真 socket, 校验 event_id 回显与双事件同文 FIFO 映射
   (server._pending text→event 消解 worker 完成先于 submit 返回的竞态)。

ADR-cloud-first: 本文件全部用本地 Recorder 假 LLM 模拟 provider; 云端不可用即
LLM 异常路径(3), 与"本地/云端都挂 → 原文"同构。
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from df_fanyi.bridge.protocol import PROTOCOL_VERSION, encode_line, make_request
from df_fanyi.bridge.server import BridgeServer
from df_fanyi.core.orchestrator import Orchestrator
from df_fanyi.core.queue import TranslationScheduler


class Recorder:
    """假 LLM: 可配置回复或抛异常(§15 录制回放, 不依赖真实 ollama/云端)。"""

    def __init__(self, reply: str | None = None) -> None:
        self.calls: list[str] = []
        self.reply = reply
        self.raise_error: Exception | None = None

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        if self.raise_error is not None:
            raise self.raise_error
        if self.reply is not None:
            return self.reply
        return "古老的矮人厅中回荡着镐子敲击石头的声音。" * max(
            1, round(len(text) / 60)
        )


class JsonLineClient:
    """最小 JSON Lines 同步客户端(等价 fanyi.lua 的 send+drain 语义)。"""

    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""
        self._rid = 0

    def send_raw(self, line: str) -> None:
        self.sock.sendall(line.encode("utf-8") + b"\n")

    def call(self, method: str, params: dict | None = None) -> dict:
        self._rid += 1
        self.send_raw(
            json.dumps(make_request(method, params or {}, rid=self._rid), ensure_ascii=False)
        )
        return self.recv_json()

    def recv_json(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        return json.loads(raw)

    def close(self) -> None:
        self.sock.close()


LONG_TEXT = (
    "Stray dwarf (Farmer) cancels Store Item in Stockpile: Item unable to reach path. "
    "The fortress guard has been dispatched to investigate reports of strange noises "
    "echoing through the deep tunnels beneath the dining hall."
)


def make_server(llm: Recorder | None, *, version: str = "0.1.0-e2e") -> BridgeServer:
    orch = Orchestrator(llm) if llm is not None else Orchestrator(None)
    sched = TranslationScheduler(orch, workers=1, autostart=True)
    server = BridgeServer(sched, transport="tcp", port=0, version=version)
    server.start()
    return server


@pytest.fixture
def server_factory():
    servers: list[BridgeServer] = []

    def _make(llm: Recorder | None = None) -> BridgeServer:
        s = make_server(llm)
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.stop()


class TestSyncFastPath:
    def test_dict_hit_no_llm(self, server_factory) -> None:
        llm = Recorder("不应该被调用")
        server = server_factory(llm)
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            resp = client.call(
                "translate",
                {
                    "event_id": "report-1",
                    "source_text": "dwarf",
                    "priority": 80,
                    "screen": "announcement",
                    "text_type": "ANNOUNCEMENT",
                },
            )
            result = resp["result"]
            assert resp["id"] == 1
            assert result["status"] == "done"
            assert result["translated_text"] == "矮人"
            assert result["confidence"] > 0
            assert result["event_id"] == "report-1"
            time.sleep(0.05)
            assert llm.calls == [], "词典快路径绝不调 LLM"
        finally:
            client.close()


class TestAsyncFetchDoneLoop:
    def test_queued_placeholder_then_fetch_done(self, server_factory) -> None:
        server = server_factory(Recorder("矮人警告：通道被落石封死了，请绕行餐厅。"))
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            resp = client.call(
                "translate",
                {
                    "event_id": "log-42",
                    "source_text": LONG_TEXT,
                    "priority": 30,
                    "screen": "history",
                    "text_type": "HISTORY",
                },
            )
            queued = resp["result"]
            assert queued["status"] == "queued", "§29: 长句异步, 主线程不等待"
            assert queued["translated_text"] == LONG_TEXT, "占位=原文"
            assert queued["confidence"] == 0.0
            # fanyi.lua tick: fetch_done 每 60 帧轮询(此处 0.05s 间隔直到取回)
            deadline = time.monotonic() + 10.0
            done_rec = None
            while time.monotonic() < deadline:
                r = client.call("fetch_done", {})["result"]
                if r["translations"]:
                    done_rec = r["translations"][0]
                    break
                time.sleep(0.05)
            assert done_rec is not None, "10s 内应完成"
            assert done_rec["status"] == "done"
            assert done_rec["event_id"] == "log-42", "event_id 回显"
            assert "落石" in done_rec["translated_text"]
            assert done_rec["confidence"] > 0
            # 再 fetch 一次: 缓冲已 drain, 不重复投递(Lua displayed 去重前提)
            assert client.call("fetch_done", {})["result"]["translations"] == []
        finally:
            client.close()


class TestLLMDownDegradation:
    def test_dict_still_works_and_long_sentence_fails_silent(self, server_factory) -> None:
        llm = Recorder(None)
        llm.raise_error = ConnectionError("ollama down / 云端不可用")
        server = server_factory(llm)
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            # 词典整句: 与 LLM 无关, 照常兜底(降级链中段: 词典/规则)
            r = client.call(
                "translate",
                {
                    "event_id": "report-7",
                    "source_text": "wooden barrel",
                    "priority": 80,
                },
            )["result"]
            assert r["status"] == "done" and r["translated_text"] == "木桶"
            # 长句 → 异步 → worker 重试耗尽 → failed: translated_text=""
            # (Lua 侧 confidence==0/译文空 → 不上屏 → 玩家看到原文 §2.3)
            client.call(
                "translate",
                {"event_id": "log-8", "source_text": LONG_TEXT, "priority": 30},
            )
            deadline = time.monotonic() + 15.0
            rec = None
            while time.monotonic() < deadline:
                tr = client.call("fetch_done", {})["result"]["translations"]
                if tr:
                    rec = tr[0]
                    break
                time.sleep(0.05)
            assert rec is not None
            assert rec["status"] == "failed"
            assert rec["translated_text"] == ""
            assert rec["confidence"] == 0.0
            assert llm.calls, "LLM 确实被尝试过(重试耗尽后放弃)"
        finally:
            client.close()


class TestEngineRestartRecovery:
    def test_stop_a_start_b(self, server_factory) -> None:
        server_a = server_factory(Recorder("x"))
        port_a = server_a.bound_port
        client = JsonLineClient("127.0.0.1", port_a)
        assert client.call("health", {})["result"]["engine"] == "ok"
        client.close()
        server_a.stop()

        # 引擎重启(游戏侧 Lua 以指数退避重连, 这里直接验证新实例恢复服务)
        server_b = make_server(Recorder("矮人矿工在深厅中凿开了新的岩石通道。"))
        try:
            client_b = JsonLineClient("127.0.0.1", server_b.bound_port)
            try:
                health = client_b.call("health", {})["result"]
                assert health["engine"] == "ok"
                assert health["protocol"] == PROTOCOL_VERSION
                r = client_b.call(
                    "translate",
                    {"event_id": "report-9", "source_text": "dwarf", "priority": 80},
                )["result"]
                assert r["status"] == "done" and r["translated_text"] == "矮人"
            finally:
                client_b.close()
        finally:
            server_b.stop()



class TestOfflineNoLLM:
    def test_dict_works_long_sentence_fails(self, server_factory) -> None:
        server = server_factory(None)  # 断网极段: 本地/云端都不可用
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            r = client.call(
                "translate",
                {"event_id": "report-11", "source_text": "cancel", "priority": 80},
            )["result"]
            assert r["status"] == "done" and r["translated_text"] == "取消"
            client.call(
                "translate",
                {"event_id": "log-12", "source_text": LONG_TEXT, "priority": 30},
            )
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                tr = client.call("fetch_done", {})["result"]["translations"]
                if tr:
                    assert tr[0]["status"] == "failed"
                    assert tr[0]["translated_text"] == ""
                    return
                time.sleep(0.05)
            pytest.fail("离线长句应产生 failed 记录")
        finally:
            client.close()


class TestLuaWireMirror:
    """按 fanyi.lua rpc_request 的逐字段格式打真 socket(跨层一致性)。"""

    @staticmethod
    def lua_translate_line(event_id: str, text: str, priority: int, rid: int) -> str:
        event = {
            "event_id": event_id,
            "timestamp": 1690000000000,
            "screen": "announcement",
            "source_text": text,
            "text_type": "ANNOUNCEMENT",
            "priority": priority,
            "context_id": event_id,
            "markup": {},
            "variables": {},
            "source_hash": "a1b2c3d4",
            "game_version": "53.16",
        }
        req = {
            "jsonrpc": "2.0",
            "id": f"translate-{rid}",
            "method": "translate",
            "params": event,
        }
        return json.dumps(req, ensure_ascii=False)

    def test_wire_format_and_fifo_mapping(self, server_factory) -> None:
        # 比例化回复(不定长): 免得 §24 长度比校验拒绝短句译文(校验器正确工作)
        server = server_factory(Recorder(None))
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            # 同一源文两条事件 → FIFO 映射(源文取 ~150 字符: 比例化假 LLM 的
            # 译文长度比落在 [0.3,3]; 短句会被 §24 校验器正确拒绝——那是校验器工作)
            wire_text = (
                "Urist McDwarf, Farmer cancels Store Item in Stockpile: Item unable to "
                "reach path. New job queued; the item will be hauled to the trade depot "
                "once the caravan leaves and the gate is clear of merchants."
            )
            self.send_and_expect_queued(client, "report-100", wire_text)
            self.send_and_expect_queued(client, "report-101", wire_text)
            deadline = time.monotonic() + 10.0
            seen: dict[str, dict] = {}
            while time.monotonic() < deadline and len(seen) < 2:
                for rec in client.call("fetch_done", {})["result"]["translations"]:
                    assert rec["status"] == "done"
                    seen[rec["event_id"]] = rec
                time.sleep(0.05)
            assert set(seen) == {"report-100", "report-101"}, "event_id 精确映射(不串线)"
            for rec in seen.values():
                assert rec["confidence"] > 0
                assert rec["translated_text"], "译文非空"
        finally:
            client.close()

    @staticmethod
    def send_and_expect_queued(
        client: JsonLineClient, event_id: str, text: str
    ) -> dict:
        client.send_raw(TestLuaWireMirror.lua_translate_line(event_id, text, 80, 1))
        resp = client.recv_json()
        assert resp["id"] == "translate-1", "字符串 id 原样回显(Lua id=method..ms)"
        assert resp["result"]["event_id"] == event_id
        assert resp["result"]["status"] in ("queued", "done")
        return resp
