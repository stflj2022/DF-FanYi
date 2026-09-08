"""本地 gemma 适配器(ticket-004): 包装 OllamaChatClient 为 LLMTranslator 签名。

工程书 §14/§15(E4B): 本地模型只处理普通动态句; 并发 worker=1(§27, CPU 单实例)。
调用方式以 ticket-001 审计结论为准: 仅 /api/chat + messages + 版本化中文系统提示
(见 prompts/translation_system_v1.txt); 游戏文本在系统提示中标注为不可信内容(§21)。
"""
from __future__ import annotations

from df_fanyi.providers.ollama_client import OllamaChatClient


class GemmaTranslator:
    """str → str 的 LLM 可调用对象: 失败抛 OllamaError, 由编排器回退原文。"""

    def __init__(
        self,
        client: OllamaChatClient | None = None,
        *,
        host: str = OllamaChatClient.DEFAULT_HOST,
        model: str = OllamaChatClient.DEFAULT_MODEL,
        timeout: float = 120.0,
    ) -> None:
        self._client = client or OllamaChatClient(host=host, model=model, timeout=timeout)
        self.model = model

    def __call__(self, text: str) -> str:
        return self._client.chat(text).content
