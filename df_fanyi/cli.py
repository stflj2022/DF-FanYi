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
import time
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

    term = sub.add_parser("term", help="术语管理(工程书 §8.1 / ticket-006)")
    term.add_argument(
        "--config",
        metavar="PATH",
        help="显式配置文件(全局位置或此处均可)",
    )
    term_sub = term.add_subparsers(dest="term_cmd", metavar="子命令", required=True)

    add = term_sub.add_parser("add", help="添加/更新术语(同 source 为更新, locked 默认 false)")
    add.add_argument("source", help="原文(整句精确匹配, 大小写不敏感)")
    add.add_argument("target", help="译文")
    add.add_argument("--category", help="分类(如 creature/race/job)")
    add.add_argument("--priority", type=int, default=100, help="优先级(默认 100, 越小越靠前)")
    add.add_argument("--locked", action="store_true", help="锁定词条(locked=true 优先级最高, §11)")
    add.add_argument("--source-type", help="来源类型(seed/manual/...)")
    add.add_argument(
        "--config",
        metavar="PATH",
        help="显式配置文件(全局位置或此处均可)",
    )

    lst = term_sub.add_parser("list", help="列出术语")
    lst.add_argument("--category", help="只列出该分类")
    lst.add_argument("--locked-only", action="store_true", help="只列出锁定词条")
    lst.add_argument(
        "--config",
        metavar="PATH",
        help="显式配置文件(全局位置或此处均可)",
    )

    b = sub.add_parser("bridge", help="启动游戏↔引擎 JSON-RPC 桥(ticket-008, 工程书 §21-22)")
    b.add_argument(
        "--config",
        metavar="PATH",
        help="显式配置文件(全局位置或此处均可)",
    )
    b.add_argument("--transport", choices=["tcp", "unix"], help="传输(默认取配置 bridge.transport)")
    b.add_argument("--port", type=int, help="tcp 端口(默认取配置 bridge.port)")
    b.add_argument("--socket-path", help="unix 套接字路径(默认取配置 bridge.socket_path)")
    b.add_argument(
        "--once", action="store_true", help="诊断模式: 处理完第一个连接后退出"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "translate":
            return _cmd_translate(args)
        if args.command == "selftest":
            return _cmd_selftest(args)
        if args.command == "term":
            return _cmd_term(args)
        if args.command == "bridge":
            return _cmd_bridge(args)
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


def _cmd_term(args: argparse.Namespace) -> int:
    """术语管理(§8.1, ticket-006): `df-fanyi term add/list`。

    term add: 有则更新, 无则插入, locked 默认 false;
    term list: 制表输出 id/source/target/category/priority/locked(locked=0|1)。
    """
    from df_fanyi.database.store import store_from_config

    if not hasattr(args, "term_cmd") or not args.term_cmd:
        print("term 需要子命令: add | list", file=sys.stderr)
        return 2
    cfg = _load_config(args)
    with store_from_config(cfg) as store:
        if args.term_cmd == "add":
            store.term_add(
                args.source,
                args.target,
                category=getattr(args, "category", None),
                priority=int(getattr(args, "priority", 100) or 100),
                locked=bool(getattr(args, "locked", False)),
                source_type=getattr(args, "source_type", None),
            )
            locked = "锁定" if args.locked else "未锁定"
            print(f"术语已保存: {args.source} → {args.target} ({locked})")
            return 0
        if args.term_cmd == "list":
            rows = store.term_list(
                category=getattr(args, "category", None),
                locked_only=bool(getattr(args, "locked_only", False)),
            )
            print("id\tsource\ttarget\tcategory\tpriority\tlocked")
            for r in rows:
                print(
                    f"{r['id']}\t{r['source']}\t{r['target']}\t"
                    f"{r['category'] or ''}\t{r['priority']}\t{int(r['locked'])}"
                )
            return 0
    print(f"未知 term 子命令: {args.term_cmd}", file=sys.stderr)
    return 2


def _cmd_bridge(args: argparse.Namespace) -> int:
    """启动桥服务(§21-22, ticket-008): JSON-RPC over loopback TCP / unix。

    游戏侧 DFHack Lua(fanyi.lua)通过 loopback TCP 接入; --once 供诊断/测试:
    处理完第一个连接即优雅退出(engine 下线时游戏侧静默回退原文 §2.3)。
    """
    from df_fanyi.bridge.server import BridgeServer
    from df_fanyi.pipeline import build_scheduler

    cfg = _load_config(args)
    bridge_cfg = cfg.bridge
    transport = args.transport or str(bridge_cfg.get("transport", "tcp"))
    port = args.port if args.port is not None else int(bridge_cfg.get("port", 17486))
    socket_path = args.socket_path or str(bridge_cfg.get("socket_path", "data/engine.sock"))
    version = str(bridge_cfg.get("version", "0.1.0"))

    scheduler = build_scheduler(cfg)
    server = BridgeServer(
        scheduler,
        transport=transport,
        host=str(bridge_cfg.get("host", "127.0.0.1")),
        port=port,
        socket_path=socket_path,
        version=version,
        results_capacity=int(bridge_cfg.get("results_capacity", 1024)),
        results_ttl_s=float(bridge_cfg.get("results_ttl_s", 300.0)),
    )
    server.start()
    endpoint = (
        f"127.0.0.1:{server.bound_port}" if transport == "tcp" else socket_path
    )
    print(f"桥已启动: {transport} {endpoint} (protocol={server._protocol_version})", flush=True)
    try:
        if args.once:
            # 诊断模式: 等第一个连接被完整处理(其服务线程退出)后优雅停止
            while server._running:
                threads = [t for t in server._conn_threads if t.is_alive()]
                if server._conn_threads and not threads:
                    break
                time.sleep(0.05)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        scheduler.shutdown()
    return 0
