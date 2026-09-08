"""固化 ollama gemma-4b-trans 的正确调用方式(ticket-001)。

规格依据:
- /api/generate 不采用(无 messages 结构、曾返回空响应)。
- /api/chat + messages(系统提示+用户文本) 为唯一固化路径。
- 测试用真实录制的响应回放(spec: 假 LLM 录制回放), 不 mock 内部实现,
  不依赖本机 ollama 服务是否在跑。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 错误类型: 引擎上层(编排器, ticket-004+)据此回退显示原文, 绝不让游戏崩溃。
# ---------------------------------------------------------------------------


class OllamaError(Exception):
    """本地模型调用失败基类。"""


class OllamaUnavailable(OllamaError):
    """服务不可达/超时(HTTP 或连接层失败)。"""


class OllamaEmptyResponse(OllamaError):
    """服务可达但返回空译文(不可用, 必须回退)。"""


# ---------------------------------------------------------------------------
# 固化的中文翻译系统提示(与 docs/audits/ENVIRONMENT_AUDIT.md 实测一致)
# ---------------------------------------------------------------------------

TRANSLATION_SYSTEM_PROMPT = (
    "你是矮人要塞(Dwarf Fortress)游戏文本翻译引擎。"
    "把用户给出的英文游戏文本翻译成简体中文。"
    "规则:1)只输出译文,不要任何解释、注释或原文;"
    "2)保留专有名词(人名如Urist保留原文);"
    "3)保留数字、标点结构与占位符;"
    "4)使用简洁的游戏公告语气。"
)

@dataclass(frozen=True)
class ChatResult:
    """一次 /api/chat 调用的外部可观测结果。"""

    content: str
    eval_count: int  # 生成的 token 数
    eval_duration_ns: int  # 生成耗时(纳秒)
    load_duration_ns: int  # 模型加载耗时(纳秒, 热载≈0)
    prompt_eval_count: int  # 提示词 token 数

    @property
    def tokens_per_second(self) -> float | None:
        """生成速度 tok/s; 无数据时返回 None。"""
        if self.eval_count <= 0 or self.eval_duration_ns <= 0:
            return None
        return self.eval_count / self.eval_duration_ns * 1e9

    @property
    def is_loaded(self) -> bool:
        """模型是否为热载(load_duration 可忽略)。"""
        return self.load_duration_ns < 1_000_000_000  # <1s 视为热载


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]
"""transport(endpoint, payload) -> response_json。生产实现走 HTTP; 测试走录制回放。"""


def http_transport(host: str, timeout: float) -> Transport:
    """生产 transport: urllib 直连 ollama HTTP API(无第三方依赖)。"""
    import urllib.error
    import urllib.request

    url = host.rstrip("/") + "/api/chat"

    def _call(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url if endpoint == "chat" else host.rstrip("/") + endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise OllamaUnavailable(f"ollama 不可达({url}): {exc}") from exc

    return _call


class OllamaChatClient:
    """固化的 ollama /api/chat 调用客户端(单并发, 阻塞式; 异步由上层 worker 包裹)。

    铁律对应: 游戏主线程绝不等待 LLM —— 本客户端只被翻译层后台 worker 调用。
    """

    DEFAULT_HOST = "http://127.0.0.1:11434"
    DEFAULT_MODEL = "gemma-4b-trans"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: float = 120.0,
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
        num_predict: int = 256,
        stream: bool = False,
    ) -> dict[str, Any]:
        """构造 /api/chat 请求体(messages 格式)。"""
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "stream": stream,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }

    def chat(self, text: str, **options: Any) -> ChatResult:
        """翻译一句话。失败抛 OllamaError 子类, 调用方回退原文。"""
        payload = self.build_payload(text, **options)
        try:
            data = self._transport("chat", payload)
        except OllamaError:
            raise
        except Exception as exc:  # transport 实现的意外异常也归一为不可用
            raise OllamaUnavailable(f"ollama 调用异常: {exc}") from exc

        content = (data.get("message") or {}).get("content") or ""
        if not content.strip():
            raise OllamaEmptyResponse(
                f"ollama 返回空译文(model={data.get('model')}, done_reason="
                f"{data.get('done_reason')})"
            )
        return ChatResult(
            content=content,
            eval_count=int(data.get("eval_count") or 0),
            eval_duration_ns=int(data.get("eval_duration") or 0),
            load_duration_ns=int(data.get("load_duration") or 0),
            prompt_eval_count=int(data.get("prompt_eval_count") or 0),
        )
