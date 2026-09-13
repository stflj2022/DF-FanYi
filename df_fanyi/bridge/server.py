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

import hashlib
import logging
import os
import select
import socket
import threading
import time
from collections import OrderedDict, deque
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
from df_fanyi.bridge.inline_cache import ParagraphCache
from df_fanyi.core.parser import normalize_text

logger = logging.getLogger("df_fanyi.bridge.server")

_METHODS = ("translate", "fetch_done", "inline_translate", "paragraph_translate", "health", "version")

# inline_translate(ticket-013): textviewer 内嵌覆盖层的段落级缓存上限与文本上限。
# SQLite 持久化段落缓存属 ticket-015, 本层只做进程内有界 LRU。
_INLINE_CACHE_CAPACITY = 256
_INLINE_MAX_TEXT = 20_000

# ticket-015 paragraph_translate: 跨进程持久化段落缓存的文本上限(与 inline_translate 一致)。
_PARAGRAPH_MAX_TEXT = 20_000
# ticket-015: 启动预热最多加载条数(ParagraphCache 默认 1000, 沿用其内部 capacity)。
_PARAGRAPH_WARMUP = 1000


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
        paragraph_cache: ParagraphCache | None = None,
        max_conns: int = 4,
        send_timeout_s: float = 5.0,
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
        # ticket-013 inline_translate: 段落缓存(cache_key → 译记录) + 在途登记
        self._inline_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._inline_wait: dict[str, str] = {}  # source_text → cache_key(异步完成后回填)
        # ticket-015: 跨进程持久化段落缓存(SQLite + 进程内 LRU); 缺省提供内存-only fallback
        # 以保持 ticket-008 的现有调用形态不破坏。
        self.paragraph_cache: ParagraphCache | None = paragraph_cache
        self._paragraph_wait: dict[str, str] = {}  # source_text → key(异步完成后回填)
        self._stats = {"translated": 0, "completed": 0, "failed": 0}
        self._running = False
        self._started_at = 0.0
        self._listen_sock: socket.socket | None = None
        self._listen_thread: threading.Thread | None = None
        self._conn_threads: list[threading.Thread] = []
        self._conns: set[socket.socket] = set()
        # 流控(2026-09-13 卡顿复盘): DF 端 socket 泄漏时 120s 读超时来不及清理,
        # 堆到 97 连接 × 每条 ~2.6MB 内核 sndbuf ≈ 250MB。补连接层防护:
        #   max_conns     — 并发连接上限, 超限踢最旧(DF 正常 1 条, 心跳 48s < 120s
        #                   读超时不会误杀; 4 留调试余量)
        #   send_timeout_s — sendall 专用短超时, 对端不读时 5s 内断开,
        #                   避免内核发送缓冲灌满(~2.6MB/socket)干等 120s
        self._max_conns = max(1, int(max_conns))
        self._send_timeout_s = float(send_timeout_s)
        self._conn_order: list[socket.socket] = []  # accept 顺序(踢最旧用)
        self.bound_port: int | None = None
        # §25 完成/失败事件 → 结果缓冲(worker 线程执行, 本类只入队, 快)
        scheduler.subscribe_done(self._on_done)
        scheduler.subscribe_failed(self._on_failed)
        # ticket-015: 启动预热最近 1000 条到 LRU(仅当 paragraph_cache 提供时)。
        if self.paragraph_cache is not None:
            try:
                self.paragraph_cache.load_top(limit=_PARAGRAPH_WARMUP)
                logger.info("段落缓存预热: %d 条", len(self.paragraph_cache))
            except Exception as exc:  # noqa: BLE001 — 预热失败不影响桥启动
                logger.warning("段落缓存预热失败: %s", exc)

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
                # 流控: 超限踢最旧连接(泄漏 socket 由 120s 超时慢慢清的旧路径太慢)
                evicted = None
                while len(self._conn_order) >= self._max_conns:
                    evicted = self._conn_order.pop(0)
                    self._conns.discard(evicted)
                self._conns.add(conn)
                self._conn_order.append(conn)
            if evicted is not None:
                logger.warning(
                    "连接超限(max_conns=%d), 踢除最旧连接: %s", self._max_conns, evicted
                )
                try:
                    evicted.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    evicted.close()
                except OSError:
                    pass
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
                if conn in self._conn_order:
                    self._conn_order.remove(conn)

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
            if not self._send(conn, make_error(exc.code, exc.message, rid=rid, data=exc.data)):
                raise OSError("send failed") from None
            return
        except Exception as exc:  # noqa: BLE001 — 单请求故障不影响连接/进程(§2.3)
            logger.exception("请求处理异常: %s", exc)
            if not self._send(conn, make_error(-32000, f"internal error: {exc}", rid=rid)):
                raise OSError("send failed") from None
            return
        if not self._send(conn, make_result(rid, result)):
            # 发送失败(对端卡死/缓冲满): 抛出走 finally 关闭连接,
            # 给对端发 FIN/RST 促其自愈重连(2026-09-13 死锁实锤)
            raise OSError("send failed")

    def _send(self, conn: socket.socket, obj: dict[str, Any]) -> bool:
        """发送一行 JSON-RPC。失败返回 False(调用方应断开该连接)。

        流控(2026-09-13 卡顿复盘): 对端不读时 sendall 会把内核 sndbuf 灌满
        (~2.6MB/socket)后死等; 发送专用短超时(5s)快速失败, 且失败必须
        断连 —— Wine 侧 DF 的 socket 被 wineserver 持 dup fd, 客户端
        close 关不干净, 只能靠桥端主动 close 发 FIN/RST 解开死锁。
        """
        try:
            conn.settimeout(self._send_timeout_s)
            try:
                conn.sendall(encode_line(obj).encode("utf-8"))
            finally:
                conn.settimeout(120.0)
            return True
        except OSError:
            return False

    # ---- 方法分发 ---------------------------------------------------------

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "translate":
            return self._on_translate(params)
        if method == "fetch_done":
            return self._on_fetch_done(params)
        if method == "inline_translate":
            return self._on_inline_translate(params)
        if method == "paragraph_translate":
            return self._on_paragraph_translate(params)
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

    # ---- inline_translate(ticket-013): textviewer 内嵌覆盖层段落路由 ----------

    @staticmethod
    def _inline_key(text: str, context: str, title: str) -> str:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        return f"inline:{context}:{title}:{digest}"

    def _inline_cache_get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._inline_cache.get(key)
            if rec is not None:
                self._inline_cache.move_to_end(key)
            return rec

    def _inline_cache_put(self, key: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._inline_cache[key] = record
            self._inline_cache.move_to_end(key)
            while len(self._inline_cache) > _INLINE_CACHE_CAPACITY:
                self._inline_cache.popitem(last=False)

    def _on_inline_translate(self, params: dict[str, Any]) -> dict[str, Any]:
        """段落级缓存路由: 命中即回; 未命中提交调度器(同步快路径即回/异步 queued)。

        游戏侧重开同一 textviewer 页时命中缓存秒回, 不再触发 LLM(预算铁律)。
        异步完成由 _on_done 回填 _inline_wait 登记的 cache_key, 下次命中。

        ticket-015: 进程内 LRU miss 后, fallback 到 paragraph_cache(SQLite 持久层),
        跨重启命中 → cached=true, 不调 LLM。
        """
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RPCError(-32602, "params.text 必须为非空字符串")
        if len(text) > _INLINE_MAX_TEXT:
            raise RPCError(-32602, f"params.text 过长(>{_INLINE_MAX_TEXT} 字符)")
        context = str(params.get("context") or "")
        title = str(params.get("title") or "")
        event_id = str(params.get("event_id") or "")
        key = self._inline_key(text, context, title)

        cached = self._inline_cache_get(key)
        if cached is not None:
            self._stats["translated"] += 1
            return {
                "status": "done",
                "cached": True,
                "event_id": event_id,
                "source_text": text,
                "translated_text": cached["translated_text"],
                "confidence": cached["confidence"],
                "model": cached["model"],
                "provider": cached["provider"],
                "error": "",
            }

        # ticket-015: 进程内 LRU miss → paragraph_cache(SQLite)命中则秒回,
        # 并写回 LRU(给同进程后续调用加速)。
        if self.paragraph_cache is not None:
            p_key = ParagraphCache.hash_text(text)
            p_rec = self.paragraph_cache.get(p_key)
            if p_rec is not None:
                self._inline_cache_put(key, {
                    "translated_text": p_rec["translated_text"],
                    "confidence": p_rec["confidence"],
                    "model": p_rec["model"],
                    "provider": p_rec["provider"],
                })
                self._stats["translated"] += 1
                return {
                    "status": "done",
                    "cached": True,
                    "event_id": event_id,
                    "source_text": text,
                    "translated_text": p_rec["translated_text"],
                    "confidence": p_rec["confidence"],
                    "model": p_rec["model"],
                    "provider": p_rec["provider"],
                    "error": "",
                }

        submission = self.scheduler.submit(
            text,
            priority=priority_from_event(85),  # 用户正在读的弹窗, 高于公告(80)
            context={"screen": context or "textviewer", "context_id": f"inline:{title}",
                     "text_type": "TEXTVIEWER", "game_version": ""},
        )
        if submission.is_async:
            with self._lock:
                # 调度器内部会归一化文本(job.source_text), 按归一化形登记回填键
                self._inline_wait[normalize_text(text)] = key
            self._stats["translated"] += 1
            return {
                "status": "queued",
                "cached": False,
                "event_id": event_id,
                "source_text": text,
                "translated_text": text,
                "confidence": 0.0,
                "model": "",
                "provider": "",
            }
        result = submission.result
        record = {
            "translated_text": result.text,
            "confidence": result.confidence,
            "model": result.model,
            "provider": result.provider,
        }
        self._inline_cache_put(key, record)
        return {
            "status": "done",
            "cached": False,
            "event_id": event_id,
            "source_text": text,
            "translated_text": result.text,
            "confidence": result.confidence,
            "model": result.model,
            "provider": result.provider,
            "error": result.error,
        }

    def _on_health(self, params: dict[str, Any]) -> dict[str, Any]:
        pc_size = len(self.paragraph_cache) if self.paragraph_cache is not None else 0
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
                "paragraph_cache_size": pc_size,
            },
            "methods": list(_METHODS),
        }

    # ---- paragraph_translate(ticket-015): 跨进程持久化段落缓存路由 --------

    def _on_paragraph_translate(self, params: dict[str, Any]) -> dict[str, Any]:
        """跨进程段落级缓存: 命中即回(cached=true); 未命中提交调度器 LLM → 写回。

        与 013 inline_translate 的区别:
        - 缓存键仅由 normalize(text) 派生(与 context 无关), 跨 context 复用;
        - 持久层是 SQLite(ParagraphCache), 重启不丢失;
        - 与 inline_translate 是并行路由: 013 走 inline:{context}:{title} 复合键,
          015 走 paragraph:{hash} 全局键; 二者共享 ParagraphCache 持久层。
        """
        if self.paragraph_cache is None:
            raise RPCError(-32601, "paragraph_translate 未启用(paragraph_cache 未配置)")
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RPCError(-32602, "params.text 必须为非空字符串")
        if len(text) > _PARAGRAPH_MAX_TEXT:
            raise RPCError(-32602, f"params.text 过长(>{_PARAGRAPH_MAX_TEXT} 字符)")
        context = str(params.get("context") or "")
        event_id = str(params.get("event_id") or "")
        key = ParagraphCache.hash_text(text)

        cached = self.paragraph_cache.get(key)
        if cached is not None:
            self._stats["translated"] += 1
            return {
                "status": "done",
                "cached": True,
                "event_id": event_id,
                "source_text": text,
                "translated_text": cached["translated_text"],
                "confidence": cached["confidence"],
                "model": cached["model"],
                "provider": cached["provider"],
                "error": "",
            }

        submission = self.scheduler.submit(
            text,
            priority=priority_from_event(80),
            context={"screen": context or "paragraph", "context_id": f"paragraph:{key}",
                     "text_type": "LONG_SENTENCE", "game_version": ""},
        )
        if submission.is_async:
            with self._lock:
                self._paragraph_wait[normalize_text(text)] = key
            self._stats["translated"] += 1
            return {
                "status": "queued",
                "cached": False,
                "event_id": event_id,
                "source_text": text,
                "translated_text": text,
                "confidence": 0.0,
                "model": "",
                "provider": "",
            }
        result = submission.result
        self.paragraph_cache.put(
            key,
            result.text,
            confidence=result.confidence,
            model=result.model,
            provider=result.provider,
            context=context,
        )
        return {
            "status": "done",
            "cached": False,
            "event_id": event_id,
            "source_text": text,
            "translated_text": result.text,
            "confidence": result.confidence,
            "model": result.model,
            "provider": result.provider,
            "error": result.error,
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
        with self._lock:
            inline_key = self._inline_wait.pop(job.source_text, None)
            paragraph_key = self._paragraph_wait.pop(job.source_text, None)
        if inline_key is not None:  # ticket-013: inline 异步完成 → 回填段落缓存
            self._inline_cache_put(inline_key, {
                "translated_text": result.text,
                "confidence": result.confidence,
                "model": result.model,
                "provider": result.provider,
            })
        if paragraph_key is not None and self.paragraph_cache is not None:
            # ticket-015: paragraph_translate 异步完成 → 写 paragraph_cache(SQLite 持久)
            self.paragraph_cache.put(
                paragraph_key,
                result.text,
                confidence=result.confidence,
                model=result.model,
                provider=result.provider,
                context=str((job.context or {}).get("screen", "") or ""),
            )
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
        with self._lock:
            self._inline_wait.pop(job.source_text, None)  # 失败不缓存, 下次重试
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