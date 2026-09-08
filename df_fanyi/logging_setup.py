"""日志(工程书 §45): logs/engine.log, 默认 INFO。

- 日志禁止记录 API Key / Authorization Header / 完整 Secret(§46)。
- 骨架期只启用 engine logger; router/provider/validator 日志随各自模块落地。
- setup_logging 幂等: 重复调用不重复加 handler。
"""
from __future__ import annotations

import logging
from pathlib import Path

from df_fanyi.config import REPO_ROOT, Config

_logger = logging.getLogger("df_fanyi")


def resolve_log_dir(cfg: Config) -> Path:
    """日志目录: 配置可覆盖, 相对路径解析到仓库根(§48 logs/)。"""
    dir_name = str(cfg.logging.get("dir", "logs"))
    path = Path(dir_name)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def engine_log_path(cfg: Config) -> Path:
    """logs/engine.log 的完整路径(selftest 与测试可见)。"""
    name = str(cfg.logging.get("engine_log", "engine.log"))
    return resolve_log_dir(cfg) / name


def setup_logging(cfg: Config) -> logging.Logger:
    """初始化 df_fanyi logger → logs/engine.log(INFO) + stderr(WARNING)。"""
    level_name = str(cfg.logging.get("level", "INFO"))
    level = getattr(logging, level_name, logging.INFO)

    if any(isinstance(h, logging.FileHandler) and getattr(h, "_df_fanyi", False) for h in _logger.handlers):
        return _logger

    log_path = engine_log_path(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    file_handler._df_fanyi = True  # type: ignore[attr-defined]  # 幂等标记

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)

    _logger.setLevel(level)
    _logger.addHandler(file_handler)
    _logger.addHandler(console)
    _logger.propagate = False
    return _logger
