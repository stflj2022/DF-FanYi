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
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from df_fanyi import __version__
from df_fanyi.config import REPO_ROOT, Config, ConfigError, load_config
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

    sc = sub.add_parser(
        "shot", help="截图翻译: OCR→段落→管线(2026-09-12 Ctrl+Print 工作流)"
    )
    sc.add_argument("image", metavar="PATH", help="截图文件路径")
    sc.add_argument("--json", action="store_true", help="JSON 输出(机器可读)")
    sc.add_argument("--zh-only", action="store_true", help="仅输出中文(用于剪贴板)")
    sc.add_argument("--save-md", action="store_true", help="在截图同目录写同名 .md 归档")
    sc.add_argument("--config", metavar="PATH", help=argparse.SUPPRESS)

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

    pt = sub.add_parser(
        "pretranslate",
        help="云端批量预翻译 vanilla raws(ticket-011): 离线翻译包装入 L2/术语层",
    )
    pt.add_argument(
        "--config",
        metavar="PATH",
        help="显式配置文件(全局位置或此处均可)",
    )
    pt_sub = pt.add_subparsers(dest="pretranslate_cmd", metavar="子命令", required=True)

    scan_p = pt_sub.add_parser("scan", help="扫描 raws → 统计报告(默认 docs/audits/PRETRANSLATE_SCAN.md)")
    scan_p.add_argument("--vanilla-dir", help="raws 根目录(覆盖 config pretranslate.vanilla_dir)")
    scan_p.add_argument("--report", help="报告输出路径(覆盖 config pretranslate.report_path)")
    scan_p.add_argument("--config", metavar="PATH", help=argparse.SUPPRESS)

    run_p = pt_sub.add_parser("run", help="云端批量翻译(断点续跑, 只补缺; 试点先 --limit 50)")
    run_p.add_argument("--limit", type=int, help="本轮最多翻译 N 句(缺省=全部剩余)")
    run_p.add_argument("--vanilla-dir", help="raws 根目录(覆盖 config)")
    run_p.add_argument("--progress-path", help="进度/翻译包路径(覆盖 config)")
    run_p.add_argument("--batch-size", type=int, help="每请求打包句数(覆盖 config)")
    run_p.add_argument("--config", metavar="PATH", help=argparse.SUPPRESS)

    st_p = pt_sub.add_parser("status", help="查看翻译包进度(已译/已弃/模型)")
    st_p.add_argument("--progress-path", help="进度/翻译包路径(覆盖 config)")
    st_p.add_argument("--config", metavar="PATH", help=argparse.SUPPRESS)

    in_p = pt_sub.add_parser("install", help="翻译包装入 L2 TM + 锁定名称术语(幂等)")
    in_p.add_argument("--progress-path", help="进度/翻译包路径(覆盖 config)")
    in_p.add_argument("--config", metavar="PATH", help=argparse.SUPPRESS)
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
        if args.command == "pretranslate":
            return _cmd_pretranslate(args)
        if args.command == "shot":
            return _cmd_shot(args)
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


def _cmd_shot(args: argparse.Namespace) -> int:
    """截图翻译: OCR→段落→管线; 输出人读格式/JSON/仅中文。"""
    from df_fanyi.shot import assemble_paragraphs, ocr_image, translate_paragraphs

    cfg = _load_config(args)
    paragraphs: list[str]
    try:
        paragraphs = assemble_paragraphs(ocr_image(args.image))
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False) if args.json else f"OCR 失败: {exc}",
              file=sys.stderr)
        return 1
    if not paragraphs:
        if args.json:
            print("[]")
        else:
            print("未识别到英文文本", file=sys.stderr)
        return 1

    orch = build_orchestrator(cfg)
    results = translate_paragraphs(paragraphs, orch)
    if args.save_md:
        from datetime import datetime

        from df_fanyi.shot import render_markdown

        md_path = str(Path(args.image).with_suffix(".md"))
        Path(md_path).write_text(
            render_markdown(results, args.image, timestamp=datetime.now().strftime("%Y-%m-%d %H:%M")),
            encoding="utf-8",
        )
        if not args.json and not args.zh_only:
            print(f"[归档] {md_path}", file=sys.stderr)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.zh_only:
        for r in results:
            print(r["zh"])
    else:
        for i, r in enumerate(results, 1):
            print(f"【{i}】{r['en']}")
            print(r["zh"])
            if r.get("error"):
                print(f"(错误: {r['error']})")
            print()
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


def _resolve_repo_path(raw: str | Path) -> Path:
    """相对路径解析到仓库根(与 cache.l2_path 同规则)。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _resolve_vanilla_dir(pt_cfg: dict, override: str | None) -> Path:
    """raws 根目录: CLI 覆盖 > config > 仓库 data/vanilla > DF 安装目录。"""
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    elif pt_cfg.get("vanilla_dir"):
        candidates.append(_resolve_repo_path(str(pt_cfg["vanilla_dir"])))
    candidates.append(REPO_ROOT / "data" / "vanilla")
    candidates.append(Path.home() / "Games" / "DwarfFortress" / "data" / "vanilla")
    for cand in candidates:
        if cand.is_dir():
            return cand
    joined = ", ".join(str(c) for c in candidates)
    raise SystemExit(f"找不到 vanilla raws 目录(试过: {joined}); 用 --vanilla-dir 指定")


def _scan_subdirs(pt_cfg: dict, root: Path) -> list[str] | None:
    """扫描子目录列表: config 声明都不存在时回退全量递归(自定义扁平根/测试)。"""
    from df_fanyi.pretranslate import scanner

    subdirs = pt_cfg.get("subdirs") or scanner.DEFAULT_SUBDIRS
    if not any((root / str(s)).is_dir() for s in subdirs):
        return None
    return [str(s) for s in subdirs]


def _cmd_pretranslate(args: argparse.Namespace) -> int:
    """云端批量预翻译(ticket-011): scan → run(--limit 试点) → install。

    scan: 只读本地 raws, 产出统计报告; run: 批量云端翻译, 进度即离线翻译包,
    断点续跑(重跑只补缺), router 故障指数退避、重试耗尽停本轮; install:
    翻译包装入 L2 TM(provider=cloud-pretranslate)+ 名称锁定进术语层。
    """
    from df_fanyi.pretranslate import scanner
    from df_fanyi.pretranslate.batch import BatchTranslator
    from df_fanyi.pretranslate.install import install_from_progress, load_progress_file
    from df_fanyi.providers.router_client import RouterChatClient

    cfg = _load_config(args)
    pt_cfg = cfg.raw.get("pretranslate", {}) or {}
    cmd = args.pretranslate_cmd

    if cmd == "scan":
        root = _resolve_vanilla_dir(pt_cfg, getattr(args, "vanilla_dir", None))
        result = scanner.scan(root, subdirs=_scan_subdirs(pt_cfg, root))
        report_raw = getattr(args, "report", None) or pt_cfg.get(
            "report_path", "docs/audits/PRETRANSLATE_SCAN.md"
        )
        report = _resolve_repo_path(report_raw)
        scanner.write_report(result, report, scan_root=str(root))
        print(
            f"扫描完成: 文件 {result.files_scanned} | 字段值 {result.total_values} | "
            f"分句后 {result.total_segments} | 去重后 {result.unique_segments}"
            f"(名称 {result.name_count}/描述 {result.description_count})"
        )
        print(f"报告: {report}")
        return 0

    progress_raw = (
        getattr(args, "progress_path", None) or pt_cfg.get(
            "progress_path", "data/pretranslate_progress.json"
        )
    )
    progress_path = _resolve_repo_path(progress_raw)

    if cmd == "status":
        client = RouterChatClient(
            host=str(pt_cfg.get("router_host", RouterChatClient.DEFAULT_HOST)),
            model=str(pt_cfg.get("router_model", RouterChatClient.DEFAULT_MODEL)),
        )
        translator = BatchTranslator(client, progress_path=progress_path)
        s = translator.status()
        if not s["exists"]:
            print(f"未开始(无进度文件: {s['progress_path']}); 先跑 pretranslate run --limit 50")
            return 0
        print(
            f"翻译包: {s['progress_path']}\n"
            f"模型: {s['model']} ({s['provider']}) | 已译 {s['translated']} | "
            f"丢弃 {s['dropped']} | 请求 {s['requests']} | 更新于 {s['updated_at']}"
        )
        return 0

    if cmd == "run":
        root = _resolve_vanilla_dir(pt_cfg, getattr(args, "vanilla_dir", None))
        scan_result = scanner.scan(root, subdirs=_scan_subdirs(pt_cfg, root))
        client = RouterChatClient(
            host=str(pt_cfg.get("router_host", RouterChatClient.DEFAULT_HOST)),
            model=str(pt_cfg.get("router_model", RouterChatClient.DEFAULT_MODEL)),
            timeout=float(pt_cfg.get("router_timeout_s", RouterChatClient.DEFAULT_TIMEOUT)),
        )
        translator = BatchTranslator(
            client,
            progress_path=progress_path,
            batch_size=int(
                getattr(args, "batch_size", None) or pt_cfg.get("batch_size", 12)
            ),
            request_interval_s=float(pt_cfg.get("request_interval_s", 2.0)),
            max_retries=int(pt_cfg.get("max_retries", 5)),
            backoff_initial_s=float(pt_cfg.get("backoff_initial_s", 2.0)),
            backoff_max_s=float(pt_cfg.get("backoff_max_s", 120.0)),
            max_tokens=int(pt_cfg.get("router_max_tokens", 4096)),
            store_source_text=bool(cfg.privacy.get("store_source_text", True)),
        )
        print(
            f"扫描: 去重后 {scan_result.unique_segments} 句; "
            f"进度已有 {len(translator.load_progress()['translations'])} 句"
        )
        stats = translator.run(scan_result.segments, limit=getattr(args, "limit", None))
        print(stats.summary())
        if not stats.ok:
            print("本轮中止(router 持续不可用/输出不齐); 进度已保存, 稍后重跑自动续传", file=sys.stderr)
            return 1
        return 0

    if cmd == "install":
        progress = load_progress_file(progress_path)
        from df_fanyi.database.store import store_from_config

        with store_from_config(cfg) as store:
            stats = install_from_progress(progress, store)
        print(f"已安装 {progress_path}: {stats.summary()}")
        return 0

    print(f"未知 pretranslate 子命令: {cmd}", file=sys.stderr)
    return 2
