"""DF-FanYi 桥服务端(ticket-008): JSON-RPC over loopback TCP / unix socket, 与调度器集成。

设计(详见 docs/audits/DFHACK_INTEGRATION.md):
- 传输: tcp(127.0.0.1 回环, 游戏侧 DFHack luasocket 官方 API 仅 TCP)默认;
  unix(主机侧工具/测试)。同一帧协议(JSON Lines, protocol 模块)。
- 每连接一个线程: 逐行读请求 → dispatch(translate/fetch_done/health/version),
  响应按请求 id 回写; 单请求异常只影响该请求(-32000), 连接不断(§2.3 静默降级)。
- translate → 调度器 submit(线程安全): 快路径命中 status=done(译文即返);
  异步路 status=queued(§29 原文占位), 完成事件由 subscribe_done(failed) 回写到
  结果缓冲, 游戏侧轮询 fetch_done 取回(单一逻辑客户端=游戏, 排空语义)。
- 完成映射: 事件以 source_text FIFO 映射 job(worker 完成可能先于 submit 返回,
  用 text→event 队列消解竞态; 仍留空 → event_id="" 由 Lua 按 source_text 兜底)。
- 队列满(§27) → status=done 且译文=原文+error(拒收不阻塞, 调用方静默)。
"""
from __future__ import annotations

import logging
import os
import select
import socket
import threading
import time
from collections import deque
from typing import Any

from df_fanyi.bridge.protocol import (
    PROTOCOL_VERSION,
    RPCError,
    decode_line,
    encode_line,
    make_error,
    make_result,
    normalize_event,
    priority_from_event,
)

logger = logging.getLogger("df_fanyi.bridge.server")

_METHODS = ("translate", "fetch_done", "health", "version")


class _ResultsBuffer:
    """已完译文缓冲: 线程安全 + 有界 + TTL 淘汰(fetch_done 排空)。"""

    def __init__(self, capacity: int = 1024, ttl_s: float = 300.0) -> None:
        if capacity < 1:
            raise ValueError("results_capacity 必须 ≥ 1")
        self._capacity = capacity
        self._ttl_s = max(1.0, ttl_s)
        self._recs: deque[tuple[float, dict[str, Any]]] = deque()
        self._lock = threading.Lock()

    def put(self, record: dict[str, Any]) -> None:
        with self._lock:
            now = time.monotonic()
            self._recs.append((now, record))
            self._evict_locked(now)

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            self._evict_locked(now)
            out = [rec for _, rec in self._recs]
            self._recs.clear()
            return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._recs)

    def _evict_locked(self, now: float) -> None:
        while self._recs and (now - self._recs[0][0]) > self._ttl_s:
            self._recs.popleft()
        while len(self._recs) > self._capacity:
            self._recs.popleft()


class BridgeServer:
    """JSON-RPC 桥监听服务(线程模型: 监听线程 + 每连接服务线程)。"""

    def __init__(
        self,
        scheduler,
        *,
        transport: str = "tcp",
        host: str = "127.0.0.1",
        port: int = 17486,
        socket_path: str | None = None,
        version: str = "0.1.0",
        protocol_version: int = PROTOCOL_VERSION,
        results_capacity: int = 1024,
        results_ttl_s: float = 300.0,
    ) -> None:
        if transport not in ("tcp", "unix"):
            raise ValueError(f"transport 非法: {transport!r}(支持 tcp/unix)")
        if port < 0 or port > 65535:
            raise ValueError(f"port 非法: {port}")
        self.scheduler = scheduler
        self.transport = transport
        self.host = host
        self.port = port
        self.socket_path = socket_path
        self.version = version
        self._protocol_version = protocol_version
        self._results = _ResultsBuffer(results_capacity, results_ttl_s)
        self._lock = threading.Lock()
        self._pending: dict[str, deque[str]] = {}  # source_text → [event_id] FIFO(完成映射)
        self._stats = {"translated": 0, "completed": 0, "failed": 0}
        self._running = False
        self._started_at = 0.0
        self._listen_sock: socket.socket | None = None
        self._listen_thread: threading.Thread | None = None
        self._conn_threads: list[threading.Thread] = []
        self._conns: set[socket.socket] = set()
        self.bound_port: int | None = None
        # §25 完成/失败事件 → 结果缓冲(worker 线程执行, 本类只入队, 快)
        scheduler.subscribe_done(self._on_done)
        scheduler.subscribe_failed(self._on_failed)

    # ---- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = time.monotonic()
        listen = self._open_listener()
        self._listen_sock = listen
        self._listen_thread = threading.Thread(
            target=self._accept_loop, name="df-fanyi-bridge-listen", daemon=True
        )
        self._listen_thread.start()
        logger.info(
            "桥服务启动: %s %s (protocol=%d)",
            self.transport,
            f"127.0.0.1:{self.bound_port}" if self.transport == "tcp" else self.socket_path,
            self._protocol_version,
        )

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        listen = self._listen_sock
        self._listen_sock = None
        if listen is not None:
            try:
                listen.close()  # 解除 accept 阻塞
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        for thread in [self._listen_thread, *self._conn_threads]:
            if thread is not None and thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
        if self.transport == "unix" and self.socket_path:
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass
        self._listen_thread = None
        self._conn_threads.clear()
        logger.info("桥服务停止")

    # ---- 内部: 监听/连接 ------------------------------------------------

    def _open_listener(self) -> socket.socket:
        if self.transport == "tcp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            self.bound_port = sock.getsockname()[1]
        else:
            path = str(self.socket_path)
            if not path:
                raise ValueError("unix 传输需要 socket_path")
            if os.path.exists(path):
                os.unlink(path)  # 清理残留文件(含上次异常退出遗留)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(path)
            self.bound_port = None
        sock.listen(4)
        return sock

    def _accept_loop(self) -> None:
        """select 轮询 + accept: close() 无法唤醒阻塞中的 accept(Linux fd 引用语义),
        故用 select 超时循环让 stop() 在 ≤0.5s 内终止监听线程。
        """
        while self._running:
            listener = self._listen_sock  # 本地引用: stop() 可能置 None
            if listener is None:
                break
            try:
                read_ready, _, _ = select.select([listener], [], [], 0.5)
            except (OSError, ValueError):
                break  # 监听套接字已被 stop() 关闭
            if not read_ready:
                continue
            try:
                conn, _addr = listener.accept()
            except OSError:
                break
            conn.settimeout(120.0)
            with self._lock:
                self._conns.add(conn)
            thread = threading.Thread(
                target=self._serve_conn, args=(conn,), daemon=True, name="df-fanyi-bridge-conn"
            )
            self._conn_threads.append(thread)
            thread.start()

    def _serve_conn(self, conn: socket.socket) -> None:
        try:
            stream = conn.makefile("rb")
            for raw in stream:
                if not self._running:
                    break
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    self._send(conn, make_error(-32700, "parse error: invalid utf-8", rid=None))
                    continue
                if not line.strip():
                    continue
                self._handle_line(conn, line)
        except OSError:
            pass
        finally:
            conn.close()
            with self._lock:
                self._conns.discard(conn)

    def _handle_line(self, conn: socket.socket, line: str) -> None:
        obj, perr = decode_line(line)
        if perr is not None:
            self._send(conn, perr)
            return
        rid = obj.get("id")
        if not isinstance(obj, dict) or not isinstance(obj.get("method"), str):
            self._send(conn, make_error(-32600, "invalid request", rid=rid))
            return
        try:
            result = self._dispatch(str(obj["method"]), obj.get("params") or {})
        except RPCError as exc:
            self._send(conn, make_error(exc.code, exc.message, rid=rid, data=exc.data))
            return
        except Exception as exc:  # noqa: BLE001 — 单请求故障不影响连接/进程(§2.3)
            logger.exception("请求处理异常: %s", exc)
            self._send(conn, make_error(-32000, f"internal error: {exc}", rid=rid))
            return
        self._send(conn, make_result(rid, result))

    def _send(self, conn: socket.socket, obj: dict[str, Any]) -> None:
        try:
            conn.sendall(encode_line(obj).encode("utf-8"))
        except OSError:
            pass

    # ---- 方法分发 ---------------------------------------------------------

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "translate":
            return self._on_translate(params)
        if method == "fetch_done":
            return self._on_fetch_done(params)
        if method == "health":
            return self._on_health(params)
        if method == "version":
            return {
                "engine": self.version,
                "protocol": self._protocol_version,
                "methods": list(_METHODS),
            }
        raise RPCError(-32601, f"method not found: {method!r}")

    def _on_translate(self, params: dict[str, Any]) -> dict[str, Any]:
        event = normalize_event(params)  # RPCError(-32602)
        text = event["source_text"]
        priority = priority_from_event(event["priority"])
        submission = self.scheduler.submit(
            text,
            priority=priority,
            context={
                "screen": event["screen"],
                "context_id": event["context_id"],
                "text_type": event["text_type"],
                "game_version": event["game_version"],
            },
        )
        base = {
            "event_id": event["event_id"],
            "source_text": text,
            "source_hash": event["source_hash"],
        }
        if submission.is_async:
            with self._lock:
                self._pending.setdefault(text, deque()).append(event["event_id"])
            self._stats["translated"] += 1
            return {
                **base,
                "status": "queued",  # §29: 主线程不等待 LLM, 占位原文
                "translated_text": text,
                "confidence": 0.0,
                "model": "",
                "provider": "",
            }
        result = submission.result
        return {
            **base,
            "status": "done",
            "translated_text": result.text,
            "confidence": result.confidence,
            "model": result.model,
            "provider": result.provider,
            "error": result.error,
        }

    def _on_fetch_done(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"translations": self._results.drain()}

    def _on_health(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "engine": "ok",
            "version": self.version,
            "protocol": self._protocol_version,
            "uptime_ms": int((time.monotonic() - self._started_at) * 1000),
            "queue": {
                "pending": self.scheduler.qsize,
                "completed": self._stats["completed"],
                "failed": self._stats["failed"],
            },
            "stats": {
                "translated": self._stats["translated"],
                "results_pending": len(self._results),
            },
        }

    # ---- 调度器完成/失败事件 → 结果缓冲 -----------------------------------

    def _pop_event_id(self, source_text: str) -> str:
        with self._lock:
            dq = self._pending.get(source_text)
            if dq:
                event_id = dq.popleft()
                if not dq:
                    self._pending.pop(source_text, None)
                return event_id
            return ""  # 完成先于 submit 返回(竞态) → Lua 按 source_text 兜底

    def _on_done(self, job, result) -> None:
        self._stats["completed"] += 1
        self._results.put(
            {
                "event_id": self._pop_event_id(job.source_text),
                "status": "done",
                "source_text": job.source_text,
                "translated_text": result.text,
                "confidence": result.confidence,
                "model": result.model,
                "provider": result.provider,
                "error": result.error,
            }
        )

    def _on_failed(self, job) -> None:
        self._stats["failed"] += 1
        self._results.put(
            {
                "event_id": self._pop_event_id(job.source_text),
                "status": "failed",
                "source_text": job.source_text,
                "translated_text": "",
                "confidence": 0.0,
                "model": "",
                "provider": "",
                "error": job.error or "translation failed",
            }
        )