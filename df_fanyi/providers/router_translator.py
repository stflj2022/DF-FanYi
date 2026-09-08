"""云端 router 适配器(ADR-cloud-first-translation): 包装 RouterChatClient 为
LLMTranslator 签名(str → str), 供编排器/调度器注入。

与本地 GemmaTranslator 完全对称, 编排器无感切换。云端为默认主力,
本地 gemma 降为离线/降级兜底。
"""
from __future__ import annotations

from df_fanyi.providers.router_client import RouterChatClient


class RouterTranslator:
    """str → str 的云端 LLM 可调用对象: 失败抛 RouterError, 由编排器回退原文。"""

    def __init__(
        self,
        client: RouterChatClient | None = None,
        *,
        host: str = RouterChatClient.DEFAULT_HOST,
        model: str = RouterChatClient.DEFAULT_MODEL,
        timeout: float = RouterChatClient.DEFAULT_TIMEOUT,
    ) -> None:
        self._client = client or RouterChatClient(host=host, model=model, timeout=timeout)
        self.model = model

    def __call__(self, text: str) -> str:
        return self._client.chat(text).content
