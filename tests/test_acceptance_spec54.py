"""工程书 §54 第一阶段验收 Test 1-8(ticket-010)。

与散装功能测试(test_pipeline/test_e2e_loop)的区别: 本模块把 §54 八项验收
**逐条**映射为独立测试, 每条 docstring 即验收报告(MVP_ACCEPTANCE.md)的一行
结论, 全部走真 socket 桥 + 真调度器(最高集成层), 不 mock 内部实现。

Test 1 矮人 / Test 2 木桶 / Test 3 动态句 / Test 4 变量 / Test 5 标记:
    词典/规则/保护器快路径 + LLM 占位还原(§22-24)。
Test 6 长句异步: 同步提交 <500ms 占位(§29 主线程不等待), fetch_done 取回。
Test 7 断网: provider 关闭(死端口, 连接拒绝)全链路 20 句压测 —— 词典/规则照常,
    其余 failed 静默, 引擎 health 恒 ok, 游戏照常显示原文(§2.3/§30)。
Test 8 API 超时: 慢速假 provider(真 HTTP 服务延迟应答) + 小超时客户端 →
    fallback 链(词典/规则 → LLM 超时 → 原文), worker 重试耗尽 → failed。

全程不依赖真实 ollama/外网(ADR-cloud-first: provider 不可用即超时/拒绝路径)。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

from df_fanyi.core.orchestrator import Orchestrator  # noqa: E402
from df_fanyi.core.queue import TranslationScheduler  # noqa: E402
from df_fanyi.bridge.server import BridgeServer  # noqa: E402
from df_fanyi.local.gemma import GemmaTranslator  # noqa: E402
from df_fanyi.providers.ollama_client import OllamaChatClient, http_transport  # noqa: E402


class Recorder:
    """假 LLM: 记录调用, 返回预设译文或抛异常(录制回放, §15)。"""

    def __init__(self, reply: str | None = None, exc: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.reply = reply
        self.exc = exc

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return self.reply or ""


class JsonLineClient:
    """最小 JSON Lines 同步客户端(等价 fanyi.lua send+drain 语义)。"""

    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""
        self._rid = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._rid += 1
        req = json.dumps(
            {"jsonrpc": "2.0", "id": self._rid, "method": method, "params": params or {}},
            ensure_ascii=False,
        )
        self.sock.sendall(req.encode("utf-8") + b"\n")
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        return json.loads(raw)

    def close(self) -> None:
        self.sock.close()


def make_server(llm) -> tuple[BridgeServer, TranslationScheduler]:
    orch = Orchestrator(llm) if llm is not None else Orchestrator(None)
    sched = TranslationScheduler(orch, workers=1, autostart=True)
    server = BridgeServer(sched, transport="tcp", port=0, version="0.1.0-acceptance")
    server.start()
    return server, sched


def translate(client: JsonLineClient, event_id: str, text: str, priority: int = 80) -> dict:
    resp = client.call(
        "translate",
        {
            "event_id": event_id,
            "source_text": text,
            "priority": priority,
            "screen": "announcement",
            "text_type": "ANNOUNCEMENT",
        },
    )
    assert resp["result"]["event_id"] == event_id, "event_id 回显"
    return resp["result"]


def wait_failed(client: JsonLineClient, want: int, *, timeout_s: float = 30.0) -> list[dict]:
    """轮询 fetch_done 直到收齐 want 条记录(或超时), 返回收到的记录。"""
    got: list[dict] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and len(got) < want:
        got.extend(client.call("fetch_done", {})["result"]["translations"])
        time.sleep(0.05)
    return got


@pytest.fixture
def bridge():
    servers: list[BridgeServer] = []
    clients: list[JsonLineClient] = []

    def _make(llm=None):
        server, _sched = make_server(llm)
        servers.append(server)
        client = JsonLineClient("127.0.0.1", server.bound_port)
        clients.append(client)
        return server, client

    yield _make
    for c in clients:
        c.close()
    for s in servers:
        s.stop()


# ---------------------------------------------------------------------------
# Test 1 — Dwarf → 矮人
# ---------------------------------------------------------------------------


class Test1Dwarf:
    def test_dwarf_via_bridge(self, bridge) -> None:
        """结论: 通过 —— "Dwarf" 经真桥词典层译出「矮人」(confidence=1.0, 零 LLM)。"""
        llm = Recorder("LLM 不应被调用")
        _server, client = bridge(llm)
        r = translate(client, "acc-1", "Dwarf")
        assert r["status"] == "done"
        assert r["translated_text"] == "矮人"
        assert r["model"] == "dictionary"
        assert r["confidence"] == 1.0
        assert llm.calls == [], "词典快路径不调 LLM(§11)"

    def test_dwarf_case_insensitive(self, bridge) -> None:
        """补充: 大小写不敏感(游戏内 Dwarf/dwarf 混用)。"""
        _server, client = bridge(Recorder("x"))
        assert translate(client, "acc-1b", "dwarf")["translated_text"] == "矮人"


# ---------------------------------------------------------------------------
# Test 2 — wooden barrel → 木桶
# ---------------------------------------------------------------------------


class Test2WoodenBarrel:
    def test_wooden_barrel_via_bridge(self, bridge) -> None:
        """结论: 通过 —— "wooden barrel" 词典整句命中「木桶」, 立即返回。"""
        _server, client = bridge(Recorder("x"))
        r = translate(client, "acc-2", "wooden barrel")
        assert r["status"] == "done"
        assert r["translated_text"] == "木桶"
        assert r["model"] == "dictionary"


# ---------------------------------------------------------------------------
# Test 3 — 动态句 Urist cancels Make Wooden Barrel.
# ---------------------------------------------------------------------------


class Test3DynamicSentence:
    def test_cancels_template_via_bridge(self, bridge) -> None:
        """结论: 通过 —— 程序生成动态句经规则层译出中文(取消+木桶), 未调 LLM。"""
        llm = Recorder("LLM 不应被调用")
        _server, client = bridge(llm)
        r = translate(client, "acc-3", "Urist cancels Make Wooden Barrel.")
        assert r["status"] == "done"
        assert "取消" in r["translated_text"]
        assert "木桶" in r["translated_text"]
        assert r["model"] == "rule"
        assert llm.calls == [], "规则命中不调 LLM"


# ---------------------------------------------------------------------------
# Test 4 — 变量 {COUNT} dwarves
# ---------------------------------------------------------------------------


class Test4VariablePreserved:
    def test_variable_preserved_via_bridge(self, bridge) -> None:
        """结论: 通过 —— {COUNT} 占位保护(§23), LLM 异步译文验证守恒后原位还原。"""
        llm = Recorder("VAR_001 名矮人已抵达。")
        _server, client = bridge(llm)
        # 变量句需 LLM → 桥返回占位(§29), 后台译完经 fetch_done 投递(生产路径)
        r = translate(client, "acc-4", "{COUNT} dwarves.")
        assert r["status"] == "queued"
        assert r["translated_text"] == "{COUNT} dwarves.", "占位=原文"
        got = wait_failed(client, 1, timeout_s=10.0)
        assert got and got[0]["status"] == "done"
        assert "{COUNT}" in got[0]["translated_text"], "变量必须完整保留"
        assert got[0]["translated_text"] == "{COUNT} 名矮人已抵达。"
        assert got[0]["confidence"] > 0
        assert llm.calls == ["VAR_001 dwarves."], "LLM 只见保护后文本(§21/§23)"

    def test_variable_dropped_by_llm_falls_back(self, bridge) -> None:
        """补充: LLM 丢失变量 → 验证拒绝(§24) → 重试耗尽 → failed;
        译文空 → Lua 不上屏 → 玩家看原文(变量信息不失真)。"""
        llm = Recorder("名矮人已抵达。")  # 丢掉 VAR_001
        _server, client = bridge(llm)
        assert translate(client, "acc-4b", "{COUNT} dwarves.")["status"] == "queued"
        got = wait_failed(client, 1, timeout_s=10.0)
        rec = got[0]
        assert rec["status"] == "failed", "验证不过重试耗尽 → failed"
        assert rec["translated_text"] == "", "failed 静默: 空译文(原文占位仍在上屏)"
        assert llm.calls.count("VAR_001 dwarves.") == 3, "max_attempts=3"


# ---------------------------------------------------------------------------
# Test 5 — Markup <color=red>Urist</color>
# ---------------------------------------------------------------------------


class Test5MarkupPreserved:
    def test_markup_preserved_via_bridge(self, bridge) -> None:
        """结论: 通过 —— 标记占位保护(§22), 异步译文内容正常翻译, 标记原样还原。"""
        llm = Recorder("MARKUP_001乌里斯特MARKUP_002")
        _server, client = bridge(llm)
        r = translate(client, "acc-5", "<color=red>Urist</color>")
        assert r["status"] == "queued", "标记句需 LLM → 异步占位"
        got = wait_failed(client, 1, timeout_s=10.0)
        assert got and got[0]["status"] == "done"
        assert got[0]["translated_text"] == "<color=red>乌里斯特</color>"
        assert llm.calls == ["MARKUP_001UristMARKUP_002"]

    def test_markup_dropped_by_llm_falls_back(self, bridge) -> None:
        """补充: LLM 丢失标记 → 验证拒绝 → failed 静默(原文含完整标记, 不丢信息)。"""
        llm = Recorder("乌里斯特")  # 丢掉标记
        _server, client = bridge(llm)
        assert translate(client, "acc-5b", "<color=red>Urist</color>")["status"] == "queued"
        got = wait_failed(client, 1, timeout_s=10.0)
        rec = got[0]
        assert rec["status"] == "failed"
        assert rec["translated_text"] == ""


# ---------------------------------------------------------------------------
# Test 6 — 长句 >500 字符必须异步
# ---------------------------------------------------------------------------

LONG_SENTENCE = (
    "Urist McDwarf, Legendary Engraver has created a masterwork engraving depicting "
    "the raising of the fortress gates, surrounded by images of dwarves and cats. "
    "The artwork shows the founding of Oslanbakust, Windurge, in the year 125, when "
    "seven brave dwarves struck the earth beneath the forbidding wilderness. It is "
    "engraved with fine curves of gold and shows the mountain homes gleaming in the "
    "light of a new dawn, while elephants graze peacefully beyond the palisade walls "
    "and the caravan from the mountain halls arrives with barrels of plump helmets, "
    "dwarven ale, and fresh supplies for the long winter ahead of the settlement."
) * 2
assert len(LONG_SENTENCE) > 500


class Test6LongSentenceAsync:
    def test_long_sentence_async_nonblocking(self, bridge) -> None:
        """结论: 通过 —— >500 字符长句同步提交 <500ms 占位原文(§29), 后台译完取回。"""
        # 假译文必须含源文每个数字同次数(源文×2故 125 出现两次), 否则 §24 数字
        # 守恒会正确拒绝——那是校验器工作
        reply = (
            "乌里斯特·矮人矿工在 125 年雕刻了一幅杰作，描绘了要塞大门的升起，"
            "这也是关于 125 年奠基的画作，周围环绕着矮人与猫的图像。"
            "作品展现了七个勇敢的矮人在荒野之下开凿大地，"
            "山脉宫殿的光辉映照着新黎明，商队带着酒桶与食物抵达，为漫长的冬季做好准备，"
            "而大象在栅栏墙外安静地吃草，金色细纹在石壁上闪闪发光。"
        )
        _server, client = bridge(Recorder(reply))
        t0 = time.perf_counter()
        r = translate(client, "acc-6", LONG_SENTENCE, priority=30)
        submit_ms = (time.perf_counter() - t0) * 1000
        assert submit_ms < 500, f"主线程不得等待 LLM(§29), 实测 {submit_ms:.0f}ms"
        assert r["status"] == "queued", "长句必须异步"
        assert r["translated_text"] == LONG_SENTENCE, "占位=原文"
        assert r["confidence"] == 0.0
        done = wait_failed(client, 1, timeout_s=10.0)
        assert done and done[0]["event_id"] == "acc-6"
        assert done[0]["status"] == "done"
        assert done[0]["confidence"] > 0
        assert "雕刻" in done[0]["translated_text"]

    def test_complexity_scores_long_sentence_high(self) -> None:
        """补充: 复杂度评分(§34)给 >500 字符句打满分长度分量 → 必入异步队列。"""
        from df_fanyi.core.queue.heuristic import complexity_score

        assert complexity_score(LONG_SENTENCE) >= 0.45, ">500 字符应至少到 E2B 档"


# ---------------------------------------------------------------------------
# Test 7 — 断网: provider 关闭, 游戏照常运行
# ---------------------------------------------------------------------------


def dead_port() -> int:
    """取一个当前必然拒绝连接的回环端口(模拟 provider 关闭/断网)。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


MIXED_EVENTS: list[tuple[str, str]] = [
    # (event_id, source_text) —— 词典/规则可解 与 必须依赖 LLM 的句子混合
    ("off-1", "Dwarf"),
    ("off-2", "wooden barrel"),
    ("off-3", "Urist cancels Make Wooden Barrel."),
    ("off-4", "carpenter"),
    ("off-5", "cancel"),
    ("off-6", "fortress"),
    ("off-7", "goblin"),
    ("off-8", "Urist has become a legendary miner."),
    ("off-9", "The granite wall has been mined successfully and the tunnel extends "
              "deeper into the unexplored regions of the cavern layer below."),
    ("off-10", "A vile force of darkness has arrived! Goblin spearmen are riding "
               "cave crocodiles toward the western gate of the fortress."),
]


class Test7NetworkDown:
    def test_full_chain_stress_with_provider_dead(self, bridge) -> None:
        """结论: 通过 —— provider 关闭(死端口) 20 句全链路压测: 词典/规则照常出中文,
        LLM 句 failed 静默原文, health 恒 ok, 无异常冒泡(§2.3/§30, 游戏照常)。"""
        # 真 OllamaChatClient 指向死端口 = provider 关闭(连接拒绝, 非录制回放假异常)
        client_llm = GemmaTranslator(
            OllamaChatClient(host=f"http://127.0.0.1:{dead_port()}", timeout=1.0)
        )
        _server, client = bridge(client_llm)

        # 每句提交两次共 20 条(压测重复事件, 同文 FIFO 映射)
        t0 = time.perf_counter()
        sync_done = 0
        for i in range(2):
            for eid, text in MIXED_EVENTS:
                r = translate(client, f"{eid}-{i}", text, priority=80)
                if r["status"] == "done":
                    sync_done += 1
                    assert r["translated_text"] != text or len(text) < 5, "done 必须有译文"
        submit_ms = (time.perf_counter() - t0) * 1000
        assert sync_done >= 16, "词典/规则句必须全部 done(20 句压测的同步部分)"

        # LLM 依赖句(off-9/10 × 2 轮 = 4 条) → 异步 → 重试耗尽 → failed; health 恒 ok
        health = client.call("health", {})["result"]
        assert health["engine"] == "ok"
        got = wait_failed(client, 4, timeout_s=90.0)
        assert len(got) == 4, "4 条 LLM 依赖句应各产生一条终态记录"
        for rec in got:
            assert rec["status"] in ("done", "failed")
            if rec["status"] == "failed":
                assert rec["translated_text"] == "", "failed 静默: 空译文(Lua 不上屏→原文)"
        assert client.call("health", {})["result"]["engine"] == "ok"

    def test_offline_no_llm_at_all(self, bridge) -> None:
        """补充: 极端断网(无任何 LLM)—— 词典照常, 未知句 failed, 引擎不倒。"""
        _server, client = bridge(None)  # llm=None = 离线
        assert translate(client, "off-x", "wooden barrel")["translated_text"] == "木桶"
        long_text = "word " * 120
        assert translate(client, "off-y", long_text, priority=30)["status"] == "queued"
        got = wait_failed(client, 1, timeout_s=10.0)
        assert got[0]["status"] == "failed"
        assert client.call("health", {})["result"]["engine"] == "ok"


# ---------------------------------------------------------------------------
# Test 8 — API 超时: 自动 fallback
# ---------------------------------------------------------------------------


class _SlowChatHandler(BaseHTTPRequestHandler):
    """慢速假 provider: /api/chat 延迟应答(可配置秒数), 记录命中次数。"""

    hit_count = 0
    delay_s = 1.5

    def do_POST(self):  # noqa: N802 — http.server 命名
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(self.delay_s)
        _SlowChatHandler.hit_count += 1
        body = json.dumps(
            {"message": {"role": "assistant", "content": "迟到的译文"},
             "eval_count": 5, "eval_duration": 5_000_000_000}
        ).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端已按超时断开 —— 这正是本假 provider 要模拟的现象

    def log_message(self, *args) -> None:  # 静默测试输出
        pass


@pytest.fixture
def slow_provider():
    _SlowChatHandler.hit_count = 0
    server = HTTPServer(("127.0.0.1", 0), _SlowChatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class Test8ApiTimeout:
    def test_orchestrator_timeout_falls_back_to_original(self, slow_provider) -> None:
        """结论: 通过 —— LLM 超时被吞掉, 编排器回退原文(confidence=0), 延迟有界。"""
        orch = Orchestrator(
            GemmaTranslator(OllamaChatClient(host=slow_provider, timeout=0.3,
                                             transport=http_transport(slow_provider, 0.3)))
        )
        t0 = time.perf_counter()
        r = orch.translate("The dwarves dug too greedily and too deep, unleashing "
                           "nameless terrors in the darkness of the deep tunnels.")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 2000, f"超时回退应有界(实测 {elapsed_ms:.0f}ms)"
        assert r.confidence == 0.0
        assert r.error is not None and "不可达" in r.error
        assert r.text.startswith("The dwarves dug too greedily"), "回退=原文"

    def test_full_fallback_chain_via_bridge(self, bridge, slow_provider) -> None:
        """结论: 通过 —— 慢 provider + 桥全链: 词典/规则层在链前段照常出中文;
        LLM 句重试耗尽(max_attempts=3)→ failed 静默; 异步提交立即返回(§29/§30)。"""
        orch = Orchestrator(
            GemmaTranslator(OllamaChatClient(host=slow_provider, timeout=0.3,
                                             transport=http_transport(slow_provider, 0.3)))
        )
        # 直接构造桥(自定义编排器): 复用 make_server 的桥装配
        sched = TranslationScheduler(orch, workers=1, autostart=True)
        server = BridgeServer(sched, transport="tcp", port=0, version="0.1.0-t8")
        server.start()
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            # 链中段(词典/规则, LLM 之前): 不受 provider 超时影响
            assert translate(client, "t8-1", "Dwarf")["translated_text"] == "矮人"
            r = translate(client, "t8-2", "Urist cancels Make Wooden Barrel.")
            assert "取消" in r["translated_text"] and "木桶" in r["translated_text"]
            # 链尾段: 未知句 → 超时 → 重试 → failed; 提交即时返回不阻塞游戏
            t0 = time.perf_counter()
            q = translate(client, "t8-3",
                          "word " * 120, priority=30)
            assert (time.perf_counter() - t0) * 1000 < 500, "异步提交不等待 LLM"
            assert q["status"] == "queued"
            got = wait_failed(client, 1, timeout_s=60.0)
            assert got and got[0]["status"] == "failed", "重试耗尽 → failed"
            assert got[0]["translated_text"] == ""
        finally:
            client.close()
            server.stop()
        # 慢服务器是单线程: hit 计数在客户端超时断连之后才累加, 轮询等齐再断言
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and _SlowChatHandler.hit_count < 3:
            time.sleep(0.1)
        assert _SlowChatHandler.hit_count == 3, (
            f"max_attempts=3 应恰试 3 次, 实测 {_SlowChatHandler.hit_count}"
        )

    def test_slow_provider_does_not_block_queue_forever(self, bridge, slow_provider) -> None:
        """补充: 慢 provider 下队列仍推进(§30: 翻译失败绝不阻塞游戏)。"""
        orch = Orchestrator(
            GemmaTranslator(OllamaChatClient(host=slow_provider, timeout=0.3,
                                             transport=http_transport(slow_provider, 0.3)))
        )
        sched = TranslationScheduler(orch, workers=1, autostart=True)
        server = BridgeServer(sched, transport="tcp", port=0, version="0.1.0-t8b")
        server.start()
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            # 词典句与 LLM 句交替: LLM 句超时不拖死词典句(快路径同步返回)
            for i in range(5):
                assert translate(client, f"t8c-{i}", "Dwarf")["translated_text"] == "矮人"
                translate(client, f"t8d-{i}", "word " * 120, priority=30)
            got = wait_failed(client, 5, timeout_s=60.0)
            assert len(got) == 5
        finally:
            client.close()
            server.stop()
