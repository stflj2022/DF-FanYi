"""文本解析与规范化(ticket-004)。

工程书 §11: 管线第一步 normalize;
§37: 上下文敏感文本的 hash 必须包含 relevant context(SHA256(text + context)),
否则相同文本不同上下文的译文会错误共享缓存。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_text(text: str | None) -> str:
    """strip + 折叠连续空白 → 单空格(用于缓存 key 与词典/规则匹配)。"""
    if not text:
        return ""
    return " ".join(text.split())


def source_hash(text: str, context: dict[str, Any] | None = None) -> str:
    """SHA256(text) 或 SHA256(text + relevant_context)(§37)。

    空上下文/None 等价: 都只 hash 文本本身, 避免语义上等价的 key 分叉。
    context 以稳定序列化(sort_keys + 紧凑分隔符)参与 hash。
    """
    payload = text
    if context:
        payload = text + "\x00" + json.dumps(
            context, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
