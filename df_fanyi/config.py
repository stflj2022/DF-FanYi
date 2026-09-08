"""配置加载(工程书 §47, §2.4)。

分层:
1. 默认配置: <repo>/config/default.yaml(仓库内置, 无任何密钥)
2. 用户覆盖: ~/.config/df-fanyi/config.yaml(整树深合并)
3. Secrets: 只从环境变量读取 —— provider 的 api_key_env 指定环境变量名,
   配置文件和代码中永远不出现真实密钥(§2.4 / §56)。

配置节(§47 + 工单 003): cache / context / queue / local_llm / providers /
logging / privacy。
"""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("df_fanyi.config")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = REPO_ROOT / "config" / "default.yaml"


class ConfigError(Exception):
    """配置缺失/非法。"""


def user_config_file() -> Path:
    """~/.config/df-fanyi/config.yaml —— 每次动态求值, 便于测试替换 HOME。"""
    return Path.home() / ".config" / "df-fanyi" / "config.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"配置文件不可读或非法: {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置根必须是映射: {path}")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并: override 中 dict 递归下钻, 其余整值覆盖。"""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


@dataclass(frozen=True)
class ProviderConfig:
    """一个模型 provider(§12/§17): base_url + api_key_env + model。"""

    name: str
    base_url: str
    model: str
    api_key_env: str
    enabled: bool = True
    api_key: str | None = field(default=None, repr=False)  # repr 永不泄露

    @property
    def available(self) -> bool:
        """本地模型(enabled 且无需 key)始终可用; 云端需 enabled 且 env 有 key。"""
        if not self.enabled:
            return False
        if not self.api_key_env:
            return True
        return bool(self.api_key)


@dataclass
class Config:
    """加载完成的引擎配置: typed 访问 + raw 保留(密钥永不写入 raw)。"""

    raw: dict[str, Any]
    config_path: Path
    cache: dict[str, Any]
    context: dict[str, Any]
    queue: dict[str, Any]
    local_llm: dict[str, Any]
    providers: list[ProviderConfig]
    logging: dict[str, Any]
    privacy: dict[str, Any]
    bridge: dict[str, Any]

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any], config_path: Path) -> "Config":
        providers = [_resolve_provider(p, config_path) for p in mapping.get("providers", [])]
        cfg = cls(
            raw=mapping,
            config_path=config_path,
            cache=dict(mapping.get("cache", {})),
            context=dict(mapping.get("context", {})),
            queue=dict(mapping.get("queue", {})),
            local_llm=dict(mapping.get("local_llm", {})),
            providers=providers,
            logging=dict(mapping.get("logging", {})),
            privacy=dict(mapping.get("privacy", {})),
            bridge=dict(mapping.get("bridge", {})),
        )
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        # 只校验配置中实际声明的值; 未声明的节按默认/局部配置处理(运维/测试用独立配置)
        workers = self.local_llm.get("workers")
        if workers is not None and (not isinstance(workers, int) or isinstance(workers, bool) or workers < 1):
            raise ConfigError("local_llm.workers 必须为 ≥1 的整数(工程书 §27 本地并发=1)")
        level = self.logging.get("level")
        if level is not None and level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(f"logging.level 非法: {level}")
        l1 = self.cache.get("l1_size")
        if l1 is not None and (not isinstance(l1, int) or isinstance(l1, bool) or l1 < 1):
            raise ConfigError("cache.l1_size 必须为 ≥1 的整数")
        l2 = self.cache.get("l2_path")
        if l2 is not None and not isinstance(l2, str):
            raise ConfigError("cache.l2_path 必须为字符串路径(§8 L2 SQLite 库文件)")
        for p in self.providers:
            if not p.name or not p.base_url or not p.model:
                raise ConfigError(f"provider 缺少 name/base_url/model: {p}")
        # §21 桥配置校验(ticket-008)
        transport = self.bridge.get("transport")
        if transport is not None and transport not in {"tcp", "unix"}:
            raise ConfigError(f"bridge.transport 非法: {transport}(支持 tcp/unix)")
        port = self.bridge.get("port")
        if port is not None and (
            not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535
        ):
            raise ConfigError(f"bridge.port 非法: {port}(0-65535, 0=随机)")


def _resolve_provider(item: Any, source: Path) -> ProviderConfig:
    if not isinstance(item, dict):
        raise ConfigError(f"{source}: provider 项必须是映射, 实际 {type(item).__name__}")
    name = str(item.get("name", ""))
    api_key_env = str(item.get("api_key_env", ""))
    api_key = None
    if api_key_env:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            logger.warning(
                "provider '%s' 需要环境变量 %s, 未检测到, 标记为不可用", name, api_key_env
            )
    return ProviderConfig(
        name=name,
        base_url=str(item.get("base_url", "")),
        model=str(item.get("model", "")),
        api_key_env=api_key_env,
        enabled=bool(item.get("enabled", True)),
        api_key=api_key,
    )


def load_config(path: str | Path | None = None) -> Config:
    """加载配置: 显式 path > 默认(用户目录覆盖合并)。

    - path 给定: 只加载该文件(运维/测试用), 不再合并用户目录。
    - path 未给: config/default.yaml 为基, 深合并 ~/.config/df-fanyi/config.yaml。
    """
    if path is not None:
        cfg_path = Path(path).expanduser()
        if not cfg_path.exists():
            raise ConfigError(f"配置不存在: {cfg_path}")
        merged = _load_yaml(cfg_path)
    else:
        if not DEFAULT_CONFIG_FILE.exists():
            raise ConfigError(f"缺少仓库默认配置: {DEFAULT_CONFIG_FILE}")
        base = copy.deepcopy(_load_yaml(DEFAULT_CONFIG_FILE))
        user_path = user_config_file()
        if user_path.exists():
            merged = _deep_merge(base, _load_yaml(user_path))
        else:
            merged = base
        cfg_path = DEFAULT_CONFIG_FILE
    return Config.from_mapping(merged, cfg_path)
