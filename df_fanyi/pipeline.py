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
    """
    llm_cfg = cfg.local_llm
    host = str(llm_cfg.get("base_url", OllamaChatClient.DEFAULT_HOST))
    model = str(llm_cfg.get("model", OllamaChatClient.DEFAULT_MODEL))
    timeout = float(llm_cfg.get("timeout_s", 120.0))
    client = OllamaChatClient(host=host, model=model, timeout=timeout)
    if store is None:
        store = store_from_config(cfg)  # ticket-006: L2 SQLite 持久层
    orch = Orchestrator(
        llm=GemmaTranslator(client, model=model),
        model_name=model,
        provider="ollama",
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
