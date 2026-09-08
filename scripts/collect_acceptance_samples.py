#!/usr/bin/env python3
"""从本机安装的 Dwarf Fortress 收集真实游戏文本验收样本(ticket-010)。

三个来源(全部真实游戏文本, 不人工编造):
- 公告:    gamelog.txt 中实际捕获的游戏内公告(剥除 [C:..]/[B]/[R] 颜色码);
- 描述:    data/vanilla/*/objects/*.txt 的 [DESCRIPTION:...] 物品/生物描述;
- 历史:    data/vanilla/vanilla_text/objects/text_*.txt 事件/对话文本行。

输出: data/samples/acceptance_samples.jsonl
    每行 {"id","category","origin","text"}; 选取确定性(排序后取前 N 条合格句),
    保证同机重跑结果一致。

用法: python3 scripts/collect_acceptance_samples.py [--game-dir PATH] [--out PATH]
      [--per-category N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_GAME_DIR = Path.home() / "Games" / "DwarfFortress"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "samples" / "acceptance_samples.jsonl"

# gamelog 颜色/排版控制码(真实捕获文本内嵌, 游戏渲染层语法, 非文本内容)
_COLOR_CODE = re.compile(r"\[[CRB]:?\d*(?::\d*)*(?::\d*)*\]")
# world-gen 记账行(非游戏内公告, 不作为样本)
_META_LINE = re.compile(r"\*\*\*|Seed:|Command Line|Generating world")
_WORDS = re.compile(r"[A-Za-z]")
_DESC = re.compile(r"\[DESCRIPTION:([^]]*)\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _clean(text: str) -> str:
    """剥除控制码、压缩空白(采集规范化, 详见文件头)。"""
    text = _COLOR_CODE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _qualifies(text: str) -> bool:
    """句子级合格标准: 3-400 字符、至少 3 个英文词、无残留控制码。"""
    if not (8 <= len(text) <= 400):
        return False
    if len(_WORDS.findall(text)) < 3:
        return False
    if "[" in text or "]" in text:
        return False
    if text.count("{") + text.count("<") > 2:
        return False  # 重模板句交给 Test 4/5, 样本集以自然句为主
    return True


def collect_announcements(game_dir: Path) -> list[tuple[str, str]]:
    """gamelog.txt: 真实游戏内公告(已在用户机器上实际产生)。"""
    out: list[tuple[str, str]] = []
    log = game_dir / "gamelog.txt"
    if not log.exists():
        return out
    for line in log.read_text(errors="replace").splitlines():
        if _META_LINE.search(line):
            continue
        text = _clean(line)
        # 先切句再逐句判定(整条公告常超单句长度上限)
        for sent in _SENTENCE_SPLIT.split(text):
            sent = sent.strip()
            if _qualifies(sent):
                out.append((sent, "gamelog.txt"))
    return out


def collect_descriptions(game_dir: Path) -> list[tuple[str, str]]:
    """raw 定义文件的 [DESCRIPTION:...] —— 游戏内物品/生物描述原文。"""
    out: list[tuple[str, str]] = []
    vanilla = game_dir / "data" / "vanilla"
    if not vanilla.exists():
        return out
    for path in sorted(vanilla.rglob("objects/*.txt")):
        try:
            content = path.read_text(errors="replace")
        except OSError:
            continue
        for match in _DESC.finditer(content):
            text = _clean(match.group(1))
            if _qualifies(text):
                out.append((text, str(path.relative_to(game_dir))))
    return out


def collect_history(game_dir: Path) -> list[tuple[str, str]]:
    """vanilla_text: 游戏内事件/对话/传记生成文本(历史与角色文本)。"""
    out: list[tuple[str, str]] = []
    text_dir = game_dir / "data" / "vanilla" / "vanilla_text" / "objects"
    if not text_dir.exists():
        return out
    for path in sorted(text_dir.glob("text_*.txt")):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            text = _clean(line)
            if _qualifies(text):
                out.append((text, str(path.relative_to(game_dir))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--per-category", type=int, default=25)
    args = ap.parse_args()

    categories = [
        ("announcement", collect_announcements(args.game_dir)),
        ("description", collect_descriptions(args.game_dir)),
        ("history", collect_history(args.game_dir)),
    ]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for cat, items in categories:
        taken = 0
        for text, origin in items:
            if taken >= args.per_category:
                break
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "id": f"{cat}-{taken + 1:03d}",
                    "category": cat,
                    "origin": origin,
                    "text": text,
                }
            )
            taken += 1
        print(f"{cat}: {taken} 条(合格池 {len(items)})")

    if not rows:
        print("错误: 未收集到样本, 检查 --game-dir", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"共 {len(rows)} 条 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
