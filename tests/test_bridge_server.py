"""ticket-008: 桥服务端 —— JSON-RPC over TCP(unix) 与调度器集成。

覆盖(验收「socket 协议回放」):
1. health/version: 引擎存活、版本、队列统计;
2. translate 同步快路径(词典/规则命中) → status=done + 译文;
3. translate 长句异步 → status=queued(§29 不阻塞), 轮询 fetch_done 取回译文
   (事件回放: event_id 一致, 含置信度/模型);
4. 异步失败(LLM 抛异常) → fetch_done 返回 failed 记录(原文静默语义);
5. 错误码: -32601 方法不存在 / -32602 参数非法 / -32700 解析错误(连接不中断);
6. unix socket 传输同协议; stop 后 socket 文件清理, 可再 start;
7. 双连接并发安全(线程模型);
8. 队列已满回退 → status=done 且译文=原文(§27 拒绝不阻塞)。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from df_fanyi.bridge.protocol import PROTOCOL_VERSION, decode_line, encode_line, make_request
from df_fanyi.bridge.server import BridgeServer
from df_fanyi.core.orchestrator import Orchestrator
from df_fanyi.core.parser import normalize_text
from df_fanyi.core.queue import TranslationScheduler


class Recorder:
    """假 LLM: 可配置回复或抛异常(§15 录制回放, 不依赖真实 ollama)。

    reply 为 None 时按源文长度生成比例译文(验证器 §24 长度比 0.3-3x 会拒绝过短译文)。
    """

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
        # §24 按信息长度(汉字数+词元数)比: CN_REPLY 每段 19 个汉字,
        # 源文 ~len/6.7 个词元 → 每 90 字 1 段 → 比例≈1.4, 在 [0.3,3] 内
        return CN_REPLY * max(1, round(len(text) / 90))


CN_REPLY = "古老的矮人厅中回荡着镐子敲击石头的声音。"


def make_scheduler(reply: str | None = None, *, attempts: int = 3, max_queue: int = 32):
    return TranslationScheduler(
        Orchestrator(llm=Recorder(reply)),
        workers=1,
        max_attempts=attempts,
        max_queue=max_queue,
        autostart=True,
    )


class JsonLineClient:
    """最小 JSON Lines 同步客户端(测试回放工具)。"""

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
        return json.loads(raw)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def bridge_tcp(tmp_path: Path):
    server = None

    def _make(**kw) -> BridgeServer:
        nonlocal server
        cfg = {"transport": "tcp", "port": 0, "version": "0.1.0-test"}
        cfg.update(kw)
        server = BridgeServer(make_scheduler(), **cfg)
        server.start()
        assert server.bound_port, "需要已绑定端口"
        return server

    yield _make
    if server is not None:
        server.stop()


class TestHealthAndLifecycle:
    def test_health_ok(self, bridge_tcp) -> None:
        server = bridge_tcp()
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            resp = client.call("health")
            assert resp["id"] == 1
            assert resp["result"]["engine"] == "ok"
            assert resp["result"]["protocol"] == PROTOCOL_VERSION
            assert resp["result"]["version"] == "0.1.0-test"
            assert resp["result"]["queue"]["pending"] >= 0
        finally:
            client.close()

    def test_version_method(self, bridge_tcp) -> None:
        server = bridge_tcp()
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            resp = client.call("version")
            assert resp["result"]["protocol"] == PROTOCOL_VERSION
        finally:
            client.close()

    def test_stop_closes_server(self, bridge_tcp) -> None:
        """stop 后监听消失: 残余内核 backlog 可能仍接受 1 次 connect, 但绝无服务响应。"""
        server = bridge_tcp()
        port = server.bound_port
        server.stop()
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=1.0)
        except OSError:
            return  # 已达 REFUSED(通常情况)
        # 内核 accept 队列残留 → 连接被接受但无逻辑服务: 请求不得有响应
        conn.settimeout(0.5)
        conn.sendall(encode_line(make_request("health", {}, rid=1)).encode())
        data = conn.recv(1024)
        conn.close()
        assert data == b"", f"停止后不应响应, 却收到: {data!r}"

    def test_unknown_method_error(self, bridge_tcp) -> None:
        client = JsonLineClient("127.0.0.1", bridge_tcp().bound_port)
        try:
            resp = client.call("no_such_method")
            assert resp["error"]["code"] == -32601
        finally:
            client.close()

    def test_invalid_params_rejected(self, bridge_tcp) -> None:
        client = JsonLineClient("127.0.0.1", bridge_tcp().bound_port)
        try:
            resp = client.call("translate", {})  # 缺 source_text
            assert resp["error"]["code"] == -32602
        finally:
            client.close()

    def test_parse_error_then_connection_alive(self, bridge_tcp) -> None:
        client = JsonLineClient("127.0.0.1", bridge_tcp().bound_port)
        try:
            client.sock.sendall(b"{this is not json\n")
            resp = json.loads(client.sock.recv(4096).split(b"\n")[0])
            assert resp["error"]["code"] == -32700
            # 连接未中断: 后续健康轮询仍可用(重连不死锁)
            resp2 = client.call("health")
            assert resp2["result"]["engine"] == "ok"
        finally:
            client.close()


class TestTranslate:
    def test_dict_fast_path_sync_done(self, bridge_tcp) -> None:
        client = JsonLineClient("127.0.0.1", bridge_tcp().bound_port)
        try:
            resp = client.call(
                "translate",
                {
                    "event_id": "evt-dict-1",
                    "screen": "announcement",
                    "source_text": "Dwarf",
                    "text_type": "ANNOUNCEMENT",
                    "priority": 80,
                    "context_id": "report-1",
                    "source_hash": "h1",
                    "game_version": "53.16",
                },
            )
            r = resp["result"]
            assert r["status"] == "done"
            assert r["event_id"] == "evt-dict-1"
            assert r["translated_text"] == "矮人"
            assert r["confidence"] == 1.0
        finally:
            client.close()

    def test_long_text_async_then_fetch_done(self, bridge_tcp) -> None:
        """长句 → queued(§29), 轮询 fetch_done 回放事件→译文。"""
        client = JsonLineClient("127.0.0.1", bridge_tcp().bound_port)
        try:
            long_text = ("The ancient dwarven halls echo with the rhythm of pickaxes "
                         "striking against weathered stone while torches flicker in the "
                         "cold mountain air. ") * 6
            resp = client.call(
                "translate",
                {"event_id": "evt-long-42", "source_text": long_text, "priority": 80},
            )
            r = resp["result"]
            assert r["status"] == "queued"  # §29: 主线程不被 LLM 阻塞
            assert r["translated_text"] == normalize_text(long_text)  # 原文(归一化)占位
            assert r["confidence"] == 0.0

            # 轮询直到后台 worker 完成
            done = None
            for _ in range(100):
                got = client.call("fetch_done")
                for rec in got["result"]["translations"]:
                    if rec["event_id"] == "evt-long-42":
                        done = rec
                        break
                if done is not None:
                    break
                time.sleep(0.05)
            assert done is not None, "fetch_done 未回放完成事件"
            assert done["status"] == "done"
            assert done["translated_text"]
            assert done["confidence"] > 0.0
        finally:
            client.close()

    def test_failed_job_reported_as_failed(self, bridge_tcp) -> None:
        """LLM 持续失败 → 重试耗尽 → fetch_done 返回 failed(原文静默的数据侧)。"""
        server = BridgeServer(
            make_scheduler(attempts=1), transport="tcp", port=0, version="0.1.0-test"
        )
        server.start()
        scheduler = server.scheduler
        scheduler._translator._llm.raise_error = RuntimeError("engine down")
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            long_text = ("A long story about a dwarven hero " * 30)
            resp = client.call("translate", {"event_id": "evt-fail-9", "source_text": long_text})
            assert resp["result"]["status"] == "queued"
            done = None
            for _ in range(100):
                got = client.call("fetch_done")
                for rec in got["result"]["translations"]:
                    if rec.get("event_id") == "evt-fail-9":
                        done = rec
                        break
                if done is not None:
                    break
                time.sleep(0.05)
            assert done is not None
            assert done["status"] == "failed"
            assert done["translated_text"] == ""
            assert done["error"]
        finally:
            client.close()
            server.stop()

    def test_queue_full_falls_back_to_original(self, bridge_tcp) -> None:
        """§27 队列满 → 拒收不阻塞: translate 返回 done 且译文=原文(静默)。

        用 BlockingLLM 卡住 worker 让等待队列保持满员(确定性, 排除调度竞态)。
        """
        release = threading.Event()

        class BlockingLLM:
            def __call__(self, text: str) -> str:
                release.wait(15)
                return CN_REPLY

        server = BridgeServer(
            TranslationScheduler(
                Orchestrator(llm=BlockingLLM()),
                workers=1,
                max_queue=1,
                max_attempts=1,
                autostart=True,
            ),
            transport="tcp",
            port=0,
            version="0.1.0-test",
        )
        server.start()
        client = JsonLineClient("127.0.0.1", server.bound_port)
        try:
            mk = lambda n: ("Chatter about the deep mines. co%d " % n) * 20  # noqa: E731
            r_a = client.call("translate", {"event_id": "eA", "source_text": mk(1)})
            assert r_a["result"]["status"] == "queued"  # 被 worker 取走(阻塞中)
            r_b = client.call("translate", {"event_id": "eB", "source_text": mk(2)})
            assert r_b["result"]["status"] == "queued"  # 占住等待队列(满员)
            r_c = client.call("translate", {"event_id": "eC", "source_text": mk(3)})
            assert r_c["result"]["status"] == "done"  # 满员拒绝 → 同步回退
            assert r_c["result"]["translated_text"] == normalize_text(mk(3))
            assert r_c["result"]["error"]  # 拒绝原因传给调用方
        finally:
            release.set()
            client.close()
            server.stop()


class TestUnixTransport:
    def test_unix_socket_health_and_translate(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "engine.sock"
        server = BridgeServer(
            make_scheduler(), transport="unix", socket_path=str(sock_path), version="0.1.0-test"
        )
        server.start()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(sock_path))
            s.sendall(encode_line(make_request("health", {}, rid=1)).encode())
            line = s.recv(65536).split(b"\n")[0]
            resp = json.loads(line)
            assert resp["result"]["engine"] == "ok"
            s.close()
        finally:
            server.stop()
        assert not sock_path.exists(), "stop 后 socket 文件应清理"

    def test_unix_restart_same_path(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "engine.sock"
        server = BridgeServer(
            make_scheduler(), transport="unix", socket_path=str(sock_path), version="v1"
        )
        server.start()
        server.stop()
        # 遗留文件不存在(上一测试已清理); 模拟残留场景
        sock_path.write_text("stale")
        server2 = BridgeServer(
            make_scheduler(), transport="unix", socket_path=str(sock_path), version="v2"
        )
        server2.start()  # 不应因残留文件失败
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(sock_path))
            s.close()
        finally:
            server2.stop()


class TestConcurrency:
    def test_two_clients_parallel_health(self, bridge_tcp) -> None:
        server = bridge_tcp()
        errors: list[Exception] = []
        results: list[dict] = []

        def worker() -> None:
            try:
                c = JsonLineClient("127.0.0.1", server.bound_port)
                for _ in range(10):
                    results.append(c.call("health"))
                c.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, errors
        assert len(results) == 20
        assert all(r["result"]["engine"] == "ok" for r in results)