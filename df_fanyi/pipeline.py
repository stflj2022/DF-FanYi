"""管线装配(ticket-004): 从 Config 构建真实 Orchestrator。

CLI 与 selftest 共用; 组件全部来自配置:
- L1 缓存容量: cache.l1_size(§9)
- 本地 LLM: local_llm.{base_url, model, timeout_s}(§14/§15/§27, worker=1)
- 系统提示词: prompts/translation_system_v1.txt(§19, 版本化)

超时说明: 工单标注 8s 源自 §17 OpenAI-compatible 云端默认;
本地 gemma 冷启动实测 12s+(磁盘冷加载 82.7s), 超时一律走配置
local_llm.timeout_s(默认 120), 避免冷启动被 8s 一刀切误杀回退。
"""
from __future__ import annotations

from df_fanyi.config import Config
from df_fanyi.core.orchestrator import Orchestrator
from df_fanyi.local.gemma import GemmaTranslator
from df_fanyi.providers.ollama_client import OllamaChatClient


def build_orchestrator(cfg: Config) -> Orchestrator:
    llm_cfg = cfg.local_llm
    host = str(llm_cfg.get("base_url", OllamaChatClient.DEFAULT_HOST))
    model = str(llm_cfg.get("model", OllamaChatClient.DEFAULT_MODEL))
    timeout = float(llm_cfg.get("timeout_s", 120.0))
    client = OllamaChatClient(host=host, model=model, timeout=timeout)
    return Orchestrator(
        llm=GemmaTranslator(client, model=model),
        model_name=model,
        provider="ollama",
        cache_capacity=int(cfg.cache.get("l1_size", 512)),
    )
