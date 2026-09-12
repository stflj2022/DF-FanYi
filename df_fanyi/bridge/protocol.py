"""DF-FanYi 桥协议(ticket-008): JSON-RPC 2.0 over JSON Lines + TextEvent(§6) 归一化。

传输无关: 无论 loopback TCP(游戏侧 DFHack luasocket 唯一可用模式, 见
docs/audits/DFHACK_INTEGRATION.md)还是 unix socket, 帧格式一致:
每请求/响应恰好一行 JSON(内嵌换行转义), Lua 侧 `client:receive('*l')` 逐行读。

方法(method):
- translate(params=TextEvent)  → 调度器 submit; 同步快路径立即 status=done,
  异步路径 status=queued(§29: 游戏主线程绝不等待 LLM), 后续由 fetch_done 取回;
- fetch_done()                → 排空自上次轮询以来完成的译文(全同缓存队列);
- inline_translate(text,      → ticket-013: 段落级缓存路由(键 inline:{context}:{title}:
  context, title, event_id)     {text_hash}); 命中即回(cached=true), 未命中提交调度器
                                (同步 done / 异步 queued, 完成后回填缓存);
- health()                    → 引擎存活/版本/队列统计(重连探测用);
- version()                   → 协议版本信息。

错误码(JSON-RPC 2.0): -32700 解析 / -32600 非法请求 / -32601 方法不存在 /
-32602 参数非法 / -32000 服务器内部。

§6 TextEvent 字段: event_id/timestamp/screen/source_text/text_type/priority/
context_id/markup/variables/source_hash/game_version —— 服务端补齐默认值并校
验必填(source_text)。priority 为 0-100 整数(§6 示例 80), 映射到队列 Priority
分档(P0-P4, §25)。
"""
from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from df_fanyi.core.queue.priority import Priority

PROTOCOL_VERSION = 1

# §7 Text Type 至少支持
TEXT_TYPES = frozenset(
    {
        "STATIC",
        "SHORT",
        "SENTENCE",
        "MULTILINE",
        "LONG_SENTENCE",
        "ANNOUNCEMENT",
        "DIALOG",
        "DESCRIPTION",
        "TOOLTIP",
        "HISTORY",
        "UNKNOWN",
    }
)

# §6 事件字段全集(服务端仅保留这些字段)
EVENT_FIELDS = (
    "event_id",
    "timestamp",
    "screen",
    "source_text",
    "text_type",
    "priority",
    "context_id",
    "markup",
    "variables",
    "source_hash",
    "game_version",
)

_EVENT_DEFAULTS: dict[str, Any] = {
    "event_id": "",  # 必填; 缺失时服务端生成
    "timestamp": 0,
    "screen": "unknown",
    "text_type": "UNKNOWN",
    "priority": 80,  # §6 示例默认(公告档)
    "context_id": "",
    "markup": [],
    "variables": [],
    "source_hash": "",
    "game_version": "",
}


class RPCError(Exception):
    """JSON-RPC 应用错误: 由 dispatch 层捕获并序列化为 error 响应。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ---- JSON Lines 帧 ---------------------------------------------------------


def encode_line(obj: dict[str, Any]) -> str:
    """对象 → 单行 JSON + '\\n'(内嵌换行由 JSON 转义, 保证行帧完整)。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_line(line: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """解码一行; 失败返回 (None, error响应对象)(-32700)。"""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, make_error(-32700, f"parse error: {exc.msg}", rid=None)
    if not isinstance(obj, dict):
        return None, make_error(-32600, "invalid request: not an object", rid=None)
    return obj, None


def make_request(method: str, params: dict[str, Any], rid: int | str | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def make_result(rid: int | str | None, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def make_error(code: int, message: str, rid: int | str | None, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


# ---- TextEvent(§6) ---------------------------------------------------------


def _synthesize_event_id() -> str:
    """服务端兜底 event_id(uuid4, §6 event_id: uuid)。"""
    return str(uuid.uuid4())


def normalize_event(params: Any) -> dict[str, Any]:
    """TextEvent 归一化(§6): 校验必填、补齐默认、白名单化 text_type。

    返回只含 EVENT_FIELDS 的干净字典; 非法输入抛 RPCError(-32602)。
    """
    if not isinstance(params, dict):
        raise RPCError(-32602, "invalid params: TextEvent must be an object")
    raw = {k: params.get(k) for k in EVENT_FIELDS}

    source = raw["source_text"]
    if not isinstance(source, str) or not source.strip():
        raise RPCError(-32602, "invalid params: source_text is required")
    raw["source_text"] = source.strip()

    text_type = raw["text_type"]
    if text_type is not None:
        text_type = str(text_type).strip().upper()
    raw["text_type"] = text_type if text_type in TEXT_TYPES else "UNKNOWN"

    priority = raw["priority"]
    if priority is None:
        priority = _EVENT_DEFAULTS["priority"]
    try:
        raw["priority"] = int(priority)
    except (TypeError, ValueError):
        raise RPCError(-32602, f"invalid params: priority must be an integer, got {priority!r}")

    timestamp = raw["timestamp"]
    try:
        raw["timestamp"] = int(timestamp) if timestamp is not None else int(time.time() * 1000)
    except (TypeError, ValueError):
        raise RPCError(-32602, "invalid params: timestamp must be an integer ms")

    event_id = raw["event_id"]
    if not isinstance(event_id, str) or not event_id.strip():
        raw["event_id"] = _synthesize_event_id()

    for field, default in (
        ("screen", "unknown"),
        ("context_id", ""),
        ("source_hash", ""),
        ("game_version", ""),
    ):
        value = raw[field]
        raw[field] = value if isinstance(value, str) and value.strip() else default

    for field, default in (("markup", []), ("variables", [])):
        value = raw[field]
        raw[field] = value if isinstance(value, list) else list(default)

    return raw


# ---- §6 priority(0-100) → 队列 Priority(P0-P4, §25) 分档 ---------------------


def priority_from_event(priority: int | None) -> Priority:
    """0-100 事件优先级分档映射(公告=80 → P2 IMPORTANT, 与 §6 对齐)。"""
    if priority is None:
        return Priority.IMPORTANT
    p = int(priority)
    p = max(0, min(100, p))  # 越界夹紧
    if p >= 95:
        return Priority.REALTIME
    if p >= 80:
        return Priority.IMPORTANT
    if p >= 70:
        return Priority.INTERACTIVE
    if p >= 50:
        return Priority.BACKGROUND
    return Priority.PREFETCH