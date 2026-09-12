"""ticket-015: JSON-RPC paragraph_translate + inline_translate 跨进程持久化。

覆盖:
1. RPC `paragraph_translate`: 缓存命中 → 直接返回, 不调 LLM;
2. RPC `paragraph_translate`: 缓存未命中 → 提交调度器 → 写回缓存;
3. inline_translate 走 paragraph_cache fallback(进程内 LRU miss → SQLite hit);
4. 异步完成后回填 paragraph_cache(跨重启命中);
5. BridgeServer 启动时 load_top 预热;
6. 参数校验: text 非空 / 不过长 / context 字符串;
7. protocol version 列出新方法。
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from df_fanyi.bridge.inline_cache import ParagraphCache
from df_fanyi.bridge.protocol import PROTOCOL_VERSION, encode_line, make_request
from df_fanyi.bridge.server import BridgeServer
from df_fanyi.core.orchestrator import Orchestrator
from df_fanyi.core.queue import TranslationScheduler


class Recorder:
    """假 LLM: 记录每次调用, 返回按源文长度等比的中文(避免 §24 验证器拒绝)。"""

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
        cn = "古老的矮人厅中回荡着镐子敲击石头的声音。"
        return cn * max(1, round(len(text) / 90))


CN_REPLY = "古老的矮人厅中回荡着镐子敲击石头的声音。"


def make_scheduler(recorder: Recorder) -> TranslationScheduler:
    return TranslationScheduler(
        Orchestrator(llm=recorder),
        workers=1,
        max_attempts=1,
        max_queue=32,
        autostart=True,
    )


class JsonLineClient:
    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""
        self._rid = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._rid += 1
        self.sock.sendall(
            encode_line(make_request(method, params or {}, rid=self._rid)).encode("utf-8")
        )
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        import json
        return json.loads(raw)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def bridge_factory(tmp_path: Path):
    """构造带 paragraph_cache 的 BridgeServer, 端口随机, 测试结束清理。"""
    servers: list[BridgeServer] = []

    def _make(recorder: Recorder | None = None) -> BridgeServer:
        rec = recorder or Recorder()
        sched = make_scheduler(rec)
        pc = ParagraphCache(tmp_path / f"pc-{len(servers)}.db")
        server = BridgeServer(
            sched,
            transport="tcp",
            port=0,
            version="0.1.0-test",
            paragraph_cache=pc,
        )
        server.start()
        servers.append(server)
        return server

    yield _make
    for s in servers:
        s.stop()
        # paragraph_cache 由 server 关闭时手动 close
        if s.paragraph_cache is not None:
            s.paragraph_cache.close()


def test_version_lists_paragraph_translate(bridge_factory) -> None:
    server = bridge_factory()
    client = JsonLineClient("127.0.0.1", server.bound_port)
    try:
        resp = client.call("version")
        methods = resp["result"].get("methods", [])
        assert "paragraph_translate" in methods
        assert "inline_translate" in methods  # 013 仍存在
    finally:
        client.close()


def test_paragraph_translate_cache_hit_skips_llm(tmp_path: Path, bridge_factory) -> None:
    """先预热缓存,再 RPC 调用 → 命中, 假 LLM 一次都不被调。"""
    rec = Recorder()
    server = bridge_factory(rec)
    # 预热: 直接写入 paragraph_cache
    pc = server.paragraph_cache
    text = "Welcome to the dwarven fortress!"
    key = ParagraphCache.hash_text(text)
    pc.put(key, "欢迎来到矮人要塞!", confidence=0.95, model="test", provider="test",
           context="textviewer")
    # RPC 调用
    client = JsonLineClient("127.0.0.1", server.bound_port)
    try:
        resp = client.call("paragraph_translate", {"text": text, "context": "textviewer"})
        result = resp["result"]
        assert result["status"] == "done"
        assert result["cached"] is True
        assert result["translated_text"] == "欢迎来到矮人要塞!"
        assert result["confidence"] == 0.95
    finally:
        client.close()
    assert rec.calls == [], f"命中缓存不应调 LLM, 但收到: {rec.calls}"


def test_paragraph_translate_cache_mrites_back(tmp_path: Path, bridge_factory) -> None:
    """未命中 → LLM → 写回 paragraph_cache → 二次调用 cached=true。

    设计: 文本 ≥ 300 字符让 complexity_score > 0.25(§34), 走异步 LLM 路径;
    信息长度比 ∈ [0.3, 3.0] 使 §24 验证器通过。
    """
    # 源文: 300+ 字符, 仅含不触发词典/规则的英文。
    text = (
        "zorch qwerty alpha bravo charlie delta echo foxtrot golf hotel india "
        "juliet kilo lima mike november oscar papa quebec romeo sierra tango "
        "uniform victor whiskey xray yankee zulu zorch qwerty alpha bravo "
        "charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
        "november oscar papa quebec romeo sierra tango uniform victor"
    )
    # 译文: 30 汉字(信息长度 = 30); 比 30/35 ≈ 0.86 ∈ [0.3, 3.0] ✓。
    cn_reply = "这是一段很长的中文翻译测试段落用于通过验证器"
    rec = Recorder(reply=cn_reply)
    server = bridge_factory(rec)
    pc = server.paragraph_cache
    assert pc.get(ParagraphCache.hash_text(text)) is None

    client = JsonLineClient("127.0.0.1", server.bound_port)
    try:
        # 第一次: 缓存未命中 → 提交调度器 (走 async, 返回 queued)
        r1 = client.call("paragraph_translate", {"text": text, "context": "textviewer"})
        assert r1["result"]["cached"] is False
        # 接受 queued(异步, 主线程不等待 LLM, §29) 或 done(同步快路径)
        assert r1["result"]["status"] in ("queued", "done")
        # 等 worker 完成 → 写回 paragraph_cache
        key = ParagraphCache.hash_text(text)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if pc.get(key) is not None:
                break
            time.sleep(0.05)
        assert pc.get(key) is not None, "异步完成后 paragraph_cache 应有记录"
        # 第二次: 命中
        r2 = client.call("paragraph_translate", {"text": text, "context": "textviewer"})
        assert r2["result"]["cached"] is True
        assert r2["result"]["translated_text"] == cn_reply
        assert r2["result"]["status"] == "done"
    finally:
        client.close()
    # 整个测试 LLM 只调一次(第二次 cached=true 不调 LLM)
    assert rec.calls == [text]


def test_inline_translate_falls_back_to_paragraph_cache(bridge_factory) -> None:
    """013 inline_translate: 进程内 LRU miss 但 paragraph_cache(SQLite)命中 → 秒回。"""
    rec = Recorder()
    server = bridge_factory(rec)
    text = "Cross-process paragraph that was translated yesterday."
    # 直接写 paragraph_cache(模拟"上一局翻译过")
    pc = server.paragraph_cache
    pc.put(ParagraphCache.hash_text(text), "昨天翻译过的段落",
           confidence=0.9, model="prev", provider="prev", context="textviewer")

    client = JsonLineClient("127.0.0.1", server.bound_port)
    try:
        resp = client.call("inline_translate", {
            "text": text, "context": "textviewer", "title": "Intro",
            "event_id": "tv-test-1",
        })
        result = resp["result"]
        # 进程内 LRU 是空的(新进程), 但 paragraph_cache 命中 → cached=True
        assert result["status"] == "done"
        assert result["cached"] is True
        assert result["translated_text"] == "昨天翻译过的段落"
    finally:
        client.close()
    assert rec.calls == [], "paragraph_cache 命中不应再调 LLM"


def test_async_completion_writes_back_to_paragraph_cache(bridge_factory) -> None:
    """异步路径: 排队 → 完成后回填 paragraph_cache(供下次秒回)。

    交付 status 可为 queued(同步未命中走异步)或 done(同步命中),
    二者最终都应让 paragraph_cache 有记录。
    """
    rec = Recorder()
    server = bridge_factory(rec)
    # 用足够长的文本强制走异步路径(§29 主线程不等待 LLM)。仅含不触发规则的英文。
    text = (
        "zorch qwerty alpha bravo charlie delta echo foxtrot golf hotel india "
        "juliet kilo lima mike november oscar papa quebec romeo sierra tango"
    )

    client = JsonLineClient("127.0.0.1", server.bound_port)
    try:
        resp = client.call("paragraph_translate", {"text": text, "context": "textviewer"})
        result = resp["result"]
        # 接受 queued(异步) 或 done(同步快路径)
        assert result["status"] in ("queued", "done")
        assert result["cached"] is False
    finally:
        client.close()
    # 等 worker 完成 → 写回 paragraph_cache
    key = ParagraphCache.hash_text(text)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if server.paragraph_cache.get(key) is not None:
            break
        time.sleep(0.05)
    rec_final = server.paragraph_cache.get(key)
    assert rec_final is not None, "异步完成后 paragraph_cache 应有记录"
    assert rec_final["translated_text"], "译文非空"


def test_paragraph_translate_invalid_text_rejected(bridge_factory) -> None:
    server = bridge_factory()
    client = JsonLineClient("127.0.0.1", server.bound_port)
    try:
        # 空字符串
        resp = client.call("paragraph_translate", {"text": "", "context": "textviewer"})
        assert "error" in resp
        assert resp["error"]["code"] == -32602
        # 非字符串
        resp = client.call("paragraph_translate", {"text": 123, "context": "textviewer"})
        assert "error" in resp
        assert resp["error"]["code"] == -32602
    finally:
        client.close()


def test_paragraph_translate_max_text_length(bridge_factory) -> None:
    server = bridge_factory()
    client = JsonLineClient("127.0.0.1", server.bound_port)
    try:
        long_text = "x" * 20_001  # 超过 _PARAGRAPH_MAX_TEXT
        resp = client.call("paragraph_translate", {"text": long_text, "context": "textviewer"})
        assert "error" in resp
        assert resp["error"]["code"] == -32602
    finally:
        client.close()


def test_bridge_server_loads_paragraph_cache_on_start(tmp_path: Path) -> None:
    """BridgeServer 启动时若 paragraph_cache 已存在, 应自动 load_top 预热。"""
    # 先建一个库, 写一些数据
    db = tmp_path / "warm.db"
    pc1 = ParagraphCache(db)
    text = "Pre-loaded paragraph for warm start."
    key = ParagraphCache.hash_text(text)
    pc1.put(key, "预加载译文", confidence=0.8, model="warm", provider="warm")
    pc1.close()
    # 重启: 用同一个 db 启动 server
    pc2 = ParagraphCache(db)
    assert pc2.get(key) is not None  # 持久层在
    sched = make_scheduler(Recorder())
    server = BridgeServer(
        sched,
        transport="tcp",
        port=0,
        version="0.1.0-test",
        paragraph_cache=pc2,
    )
    try:
        server.start()
        # load_top 应已被构造函数调用 → LRU 中有 key
        assert key in pc2.keys()
    finally:
        server.stop()
        pc2.close()