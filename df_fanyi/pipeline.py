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
from df_fanyi.database.store import store_from_config
from df_fanyi.local.gemma import GemmaTranslator
from df_fanyi.providers.ollama_client import OllamaChatClient


def build_orchestrator(cfg: Config, *, store: "SQLiteStore | None" = None) -> Orchestrator:
    """从 Config 构建真实 Orchestrator(CLI/调度器共用)。

    store 可注入(与调度器共享同一 SQLite 连接, 避免多连接写争用);
    未给时按 config 打开 L2 持久层。

    LLM 选择(2026-09-10 更新): 全部走云端 Router(router_host/router_model),
    本地 ollama 兕底已按用户指令移除(local_llm.enabled=false)。此前硬编码
    ollama → 实时翻译永远走本地 gemma, 超时即回退原文(玩家看到中英混杂,
    见 15:59 欢迎公告 FAILED job)。现在与 pretranslate 同源。
    """
    pt_cfg = cfg.raw.get("pretranslate", {})  # pretranslate 仅在 raw 字典(Config 未 typed)
    if pt_cfg.get("router_host"):
        from df_fanyi.providers.router_client import RouterChatClient
        from df_fanyi.providers.router_translator import RouterTranslator

        host = str(pt_cfg.get("router_host"))
        model = str(pt_cfg.get("router_model", RouterChatClient.DEFAULT_MODEL))
        timeout = float(pt_cfg.get("router_timeout_s", RouterChatClient.DEFAULT_TIMEOUT))
        primary: "GemmaTranslator | RouterTranslator" = RouterTranslator(
            host=host, model=model, timeout=timeout
        )
        primary_provider = "router"
    else:
        llm_cfg = cfg.local_llm
        host = str(llm_cfg.get("base_url", OllamaChatClient.DEFAULT_HOST))
        model = str(llm_cfg.get("model", OllamaChatClient.DEFAULT_MODEL))
        timeout = float(llm_cfg.get("timeout_s", 120.0))
        primary = GemmaTranslator(host=host, model=model, timeout=timeout)
        primary_provider = "ollama"

    if store is None:
        store = store_from_config(cfg)  # ticket-006: L2 SQLite 持久层
    orch = Orchestrator(
        llm=_FallingBackLLM(primary, primary_provider, cfg=cfg),
        model_name=model,
        provider=primary_provider,
        cache_capacity=int(cfg.cache.get("l1_size", 512)),
        store=store,
    )
    orch.warm_l1()  # §38 高频文本常驻
    return orch


def build_scheduler(cfg: Config) -> "TranslationScheduler":
    """ticket-007: 从配置装配调度器(优先级队列 + 后台翻译 worker, 工程书 §25-30)。

    - workers: local_llm.workers(§27 默认 1);
    - max_queue: local_llm.max_queue / queue.max_queue(§27 max_queue=32);
    - max_attempts / realtime_timeout_ms / async_threshold: queue 节(§29/§34);
    - 与编排器共享同一 SQLite 连接(任务表状态落库, §42 崩溃恢复);
    - privacy.store_source_text=false 时不落原文(§46)。
    云端 worker 为第三阶段(接口在 queue.worker 模块预留)。
    """
    from df_fanyi.core.queue import TranslationScheduler  # 惰性导入(避免包循环)

    store = store_from_config(cfg)
    orch = build_orchestrator(cfg, store=store)
    queue_cfg = cfg.queue
    llm_cfg = cfg.local_llm
    return TranslationScheduler(
        orch,
        workers=int(llm_cfg.get("workers", 1)),
        max_queue=int(llm_cfg.get("max_queue") or queue_cfg.get("max_queue", 32)),
        async_threshold=float(queue_cfg.get("async_threshold", 0.25)),
        max_attempts=int(queue_cfg.get("max_attempts", 3)),
        realtime_timeout_ms=int(queue_cfg.get("realtime_timeout_ms", 10_000)),
        store=store,
        store_source_text=bool(cfg.privacy.get("store_source_text", True)),
    )


def _FallingBackLLM(primary, primary_provider, cfg=None):
    """复合 LLM: 优先 primary(router/云端)。

    2026-09-10 起(用户指令): 本地 ollama 兜底已移除, 全部走云端。
    local_llm.enabled=false(默认配置即 false)时云端失败直接回退原文,
    不再降级本地 gemma——本地模型 p50≈58s 且不可达曾导致欢迎公告翻译
    FAILED(15:59 job), 玩家看到中英混杂。
    """
    from df_fanyi.local.gemma import GemmaTranslator
    from df_fanyi.providers.ollama_client import OllamaChatClient
    from df_fanyi.providers.router_client import RouterError

    # 懒加载配置(循环导入防护): 只用一次, 构建兜底客户端
    if cfg is None:
        from df_fanyi.config import load_config

        cfg = load_config()
    llm_cfg = cfg.local_llm
    if not bool(llm_cfg.get("enabled", False)):
        # 纯云端模式: 不构建本地兜底, 云端失败由 Orchestrator 回退原文
        return primary
    backup_host = str(llm_cfg.get("base_url", OllamaChatClient.DEFAULT_HOST))
    backup_model = str(llm_cfg.get("model", OllamaChatClient.DEFAULT_MODEL))
    backup_timeout = float(llm_cfg.get("timeout_s", 120.0))

    class _Fallback:
        model = primary.model

        def __init__(self):
            self._primary = primary
            self._backup = None
            self._provider = primary_provider

        def __call__(self, text: str) -> str:
            try:
                return self._primary(text)
            except (RouterError, OSError, TimeoutError):
                # 云端不可用 → 本地 ollama 兜底(仅在 local_llm.enabled=true 时存在此路径)
                if self._backup is None:
                    self._backup = GemmaTranslator(
                        host=backup_host, model=backup_model, timeout=backup_timeout
                    )
                return self._backup(text)

    return _Fallback()
