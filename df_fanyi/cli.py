"""df-fanyi 命令行入口(ticket-003, ticket-004 接入真实管线)。

子命令:
- translate: 读 stdin 单句英文游戏文本 → 译文
  (ticket-004 起为真实管线: 缓存→词典→规则→本地LLM→验证)
- selftest: 配置加载 / 日志可写 / 管线可用 自检
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Sequence

from df_fanyi import __version__
from df_fanyi.config import Config, ConfigError, load_config
from df_fanyi.core.orchestrator import Orchestrator
from df_fanyi.logging_setup import engine_log_path, setup_logging
from df_fanyi.pipeline import build_orchestrator

logger = logging.getLogger("df_fanyi.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="df-fanyi",
        description="DF-FanYi 矮人要塞 AI 动态汉化引擎(翻译内核)",
    )
    parser.add_argument("--version", action="version", version=f"df-fanyi {__version__}")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="显式配置文件(默认 config/default.yaml, 用户 ~/.config/df-fanyi/config.yaml 覆盖)",
    )
    sub = parser.add_subparsers(dest="command", metavar="子命令", required=True)

    t = sub.add_parser("translate", help="翻译 stdin 的单句英文游戏文本")
    t.add_argument(
        "--config",
        metavar="PATH",
        help="显式配置文件(全局位置或此处均可)",
    )
    t.add_argument("--json", action="store_true", help="输出 JSON(含 source/confidence 等字段)")

    s = sub.add_parser("selftest", help="自检: 配置加载 / 日志可写 / 管线可用")
    s.add_argument(
        "--config",
        metavar="PATH",
        help="显式配置文件(全局位置或此处均可)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "translate":
            return _cmd_translate(args)
        if args.command == "selftest":
            return _cmd_selftest(args)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 2


def _load_config(args: argparse.Namespace) -> Config:
    cfg = load_config(getattr(args, "config", None))
    setup_logging(cfg)
    return cfg


def _cmd_translate(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    text = sys.stdin.read().strip()
    if not text and sys.stdin.isatty():
        try:
            text = input("原文: ").strip()
        except EOFError:
            return 2
    if not text:
        print("输入为空", file=sys.stderr)
        return 2

    orch = build_orchestrator(cfg)  # ticket-004: 真实管线(缓存→词典→规则→LLM→验证)
    result = orch.translate(text)
    logger.info(
        "translate: model=%s provider=%s confidence=%.2f latency=%dms cache_hit=%s fallback=%s error=%s",
        result.model,
        result.provider,
        result.confidence,
        result.latency_ms,
        result.cache_hit,
        result.text == result.source_text,
        result.error,
    )

    if args.json:
        payload = {
            "text": result.text,
            "source_text": result.source_text,
            "model": result.model,
            "provider": result.provider,
            "confidence": result.confidence,
            "latency_ms": result.latency_ms,
            "cache_hit": result.cache_hit,
            "is_placeholder": result.is_placeholder,
            "error": result.error,
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(result.text)
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    lines = [
        f"配置: {cfg.config_path} (providers={len(cfg.providers)}, 可用="
        f"{sum(1 for p in cfg.providers if p.available)})",
        f"日志: {engine_log_path(cfg)}",
    ]
    result = build_orchestrator(cfg).translate("Dwarf")
    lines.append(
        f"翻译: model={result.model} 管线可用"
        + (f" (confidence={result.confidence})" if result.confidence else " (回退原文)")
    )
    ok = result.error is None
    for line in lines:
        print(line)
    print("df-fanyi 自检" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1
