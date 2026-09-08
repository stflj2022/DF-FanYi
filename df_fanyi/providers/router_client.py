"""云端翻译客户端 —— 指向本机 Pi Model Router(OpenAI-compatible)。

决策(ADR-cloud-first-translation, 2026-09-08 用户定):
- 云端为默认主力, 本地 gemma 降为离线/降级兜底。
- 云端经 Pi Model Router(本机 model-router, http://127.0.0.1:8010/v1)
  的 `router/L2` 模型(Coding 档)接入, 路由到 智谱 GLM-4.7 → 国家超算
  DeepSeek V4 Flash → OpenRouter, key 由 model-router 侧持有(env),
  DF-FanYi 侧无需任何 API key。
- 走 OpenAI-compatible /v1/chat/completions, 与工程书 §12/§17-18 约束一致。

与本地 ollama_client 的区别:
- 端点 /v1/chat/completions(OpenAI 格式), 非 ollama /api/chat。
- 响应含 `reasoning`(DeepSeek 思维链)字段, 必须丢弃, 只取 `message.content`。
- max_tokens 需较大(推理模型 reasoning 会占用 token 预算), 默认 2048。

错误语义与 ollama_client 一致: 服务不可达/超时/空译文 → 抛 RouterError,
由编排器回退显示原文, 绝不让游戏崩溃(§2.3/§30)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from df_fanyi.prompts import load_system_prompt

# ---------------------------------------------------------------------------
# 错误类型: 与 ollama_client 对齐, 编排器据此回退显示原文。
# ---------------------------------------------------------------------------


class RouterError(Exception):
    """云端模型调用失败基类。"""


class RouterUnavailable(RouterError):
    """服务不可达/超时(HTTP 或连接层失败)。"""


class RouterEmptyResponse(RouterError):
    """服务可达但返回空译文(不可用, 必须回退)。"""


# 固化的中文翻译系统提示(与本地 ollama 共用同一版本化提示词)
TRANSLATION_SYSTEM_PROMPT = load_system_prompt("v1")


@dataclass
class ChatResult:
    """一次 /v1/chat/completions 调用的外部可观测结果。"""
    content: str | None
    reasoning: str | None = None
    model: str | None = None
    provider: str | None = None


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


def http_transport(host: str, timeout: float) -> Transport:
    """生产 transport: urllib 直连 model-router OpenAI 端点。

    model-router 监听 127.0.0.1:8010: 显式禁代理, 避免环境 HTTP(S)_PROXY
    把本地请求送进全局代理(与 ollama_client 同因)。
    """
    import urllib.error
    import urllib.request

    base = host.rstrip("/") + "/v1"
    _no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = base + endpoint
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with _no_proxy.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise RouterUnavailable(f"云端 router 不可达({url}): {exc}") from exc

    return _call


class RouterChatClient:
    """固化的 OpenAI-compatible 云端翻译客户端(经 Pi Model Router)。

    单并发、阻塞式; 异步由上层 worker 包裹。铁律: 游戏主线程绝不等待 LLM。
    """

    DEFAULT_HOST = "http://127.0.0.1:8010"
    DEFAULT_MODEL = "router/L2"
    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ) -> None:
        self.model = model
        self.host = host
        self.timeout = timeout
        self._transport = transport or http_transport(host, timeout)

    def build_payload(
        self,
        text: str,
        *,
        system: str = TRANSLATION_SYSTEM_PROMPT,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        stream: bool = False,
    ) -> dict[str, Any]:
        """构造 /v1/chat/completions 请求体(OpenAI 格式)。"""
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

    def chat(self, text: str, **options: Any) -> ChatResult:
        """调用云端翻译一句, 返回解析结果(content 去 reasoning)。"""
        payload = self.build_payload(text, **options)
        data = self._transport("/chat/completions", payload)
        try:
            choice = data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content")
            reasoning = message.get("reasoning")
        except (KeyError, IndexError, TypeError) as exc:
            raise RouterEmptyResponse(f"云端响应结构异常: {exc}") from exc
        if not content or not content.strip():
            raise RouterEmptyResponse("云端返回空译文(content 为空)")
        return ChatResult(
            content=content.strip(),
            reasoning=reasoning,
            model=data.get("model"),
            provider=data.get("provider"),
        )
