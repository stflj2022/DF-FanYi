#!/usr/bin/env python3
"""collect-untranslated.py — 从 dfint Debug 日志收集画面漏网英文 → 待翻译清单

背景: dfint(hook 渲染函数)未命中词典的串在 LOG_LEVEL=Debug 下记录为
  "missing translation for {func}:\\n{bt}:\\n{string:?}"
这是"游戏画面上仍是英文"的**真实运行时**清单(静态 exe 抽取覆盖不了的:
运行时拼接串/逗号拆分后的 part/raw 生成文本等)。

处理逻辑完全模拟 dfint(src/translator/mod.rs) 的查询行为:
- 小写化完全匹配(legacy+simple 词典)
- 整串未命中且含 ", " → 拆成 part 逐个查 → 所以入词典应入 **part**(模板),
  dfint 拆分时自然命中
- 无字母串/带 CJK(已翻)/已在词典 → 跳过
- 封闭类虚词(词沙拉源, prune-dict-function-words.py 已删)不再收录

输出: JSONL {"key": "<英文>"} — 对齐 translate-exe-strings.py 的输入格式。
用法:
  python3 scripts/collect-untranslated.py                # 收集+报告
  python3 scripts/collect-untranslated.py --rotate       # 收集后归档日志
  python3 scripts/collect-untranslated.py --out foo.jsonl
"""
import argparse
import csv
import datetime
import json
import re
import sys
from collections import Counter
from pathlib import Path

GAME_LOG = Path("/home/wu/Games/DF-v5306/Dwarf Fortress/dfint-data/dfint-log.log")
GAME_DICT = Path("/home/wu/Games/DF-v5306/Dwarf Fortress/dfint-data/legacy-dictionary.csv")
SIMPLE_DICT = Path("/home/wu/Games/DF-v5306/Dwarf Fortress/dfint-data/simple-dictionary.csv")
OUT_DEFAULT = Path.home() / "DF-FanYi/dfint-data" / (
    "untranslated-" + datetime.date.today().strftime("%Y%m%d") + ".jsonl")

MISSING_RE = re.compile(r"missing translation for (\w+):")
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
LETTER_RE = re.compile(r"[A-Za-z]")

# 与 prune-dict-function-words.py 保持一致: 虚词不再入词典
CLOSED = set("""the a an of to in on at for with by from as and or but nor so yet
i you he she it we they me him her us them my your his its our their mine yours
this that these those there here what which who whom whose when where why how
is are was were be been being am do does did done have has had having will would
shall should can could may might must not no yes if than then because while
about above after again against all any before below between both down during
each few more most other some such only own same too under until up very
into through over off once out further just don s t d ll m o re ve y ain aren
couldn didn doesn hadn hasn haven isn ma mightn mustn needn shan shouldn wasn
weren won wouldn""".split())
# UI 标签豁免(与 prune-dict-function-words.py 同步): 这些"虚词"实为界面按钮
UI_KEEP = {"all", "done", "off", "no", "yes", "any", "other", "some", "might"}


def rust_debug_unescape(s: str) -> str:
    """Rust {:?} 字符串转义还原 (含 \\n \\t \\" \\\\ \\u{...} \\xNN)。"""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            d = s[i + 1]
            if d == "n": out.append("\n"); i += 2; continue
            if d == "t": out.append("\t"); i += 2; continue
            if d == "r": out.append("\r"); i += 2; continue
            if d == "0": out.append("\0"); i += 2; continue
            if d in ("\\", '"', "'"): out.append(d); i += 2; continue
            if d == "x" and i + 3 < n:
                try: out.append(chr(int(s[i+2:i+4], 16))); i += 4; continue
                except ValueError: pass
            if d == "u" and i + 2 < n and s[i + 2] == "{":
                j = s.find("}", i + 3)
                if j != -1:
                    try: out.append(chr(int(s[i+3:j], 16))); i = j + 1; continue
                    except ValueError: pass
        out.append(c); i += 1
    return "".join(out)


def load_dict_keys() -> set[str]:
    keys: set[str] = set()
    for p in (GAME_DICT, SIMPLE_DICT):
        if not p.exists():
            print(f"!! 词典不存在(跳过): {p}", file=sys.stderr)
            continue
        with p.open(encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if row and row[0] != "text":
                    keys.add(row[0].strip().lower())
    return keys


def parse_missing(log_path: Path):
    """yield (func, bt, string)。日志块: 头行/bt行/字符串行 各一行。"""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    i, total = 0, len(lines)
    while i < total:
        m = MISSING_RE.search(lines[i])
        if m and i + 2 < total:
            raw = lines[i + 2].strip()
            # Rust {:?} 输出恒为 "..." 包裹(内部引号是 \" 转义), 去壳再还原
            if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
                raw = raw[1:-1]
            yield m.group(1), lines[i + 1], rust_debug_unescape(raw)
            i += 3
            continue
        i += 1


def candidate_parts(s: str):
    """模拟 dfint: 整串或 ', ' 拆分后的 part, 返回值得入词典的候选。"""
    parts = s.split(", ") if ", " in s else [s]
    return [p for p in parts if p.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, default=GAME_LOG)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--rotate", action="store_true", help="收集后归档日志(dfint-log-<日期>.log)")
    args = ap.parse_args()

    if not args.log.exists():
        sys.exit(f"日志不存在: {args.log} (需 LOG_LEVEL:Debug 且玩过一局)")
    dict_keys = load_dict_keys()

    raw = Counter()          # 原始 missing 串计数(含重复查询)
    bt_counter = Counter()   # bt 前缀(渲染栈)频次
    candidates: dict[str, str] = {}   # 候选 key(原文) -> func 示例
    skipped = Counter()

    for func, bt, s in parse_missing(args.log):
        raw[s] += 1
        bt_counter[bt.split("/")[-1][:16]] += 1
        for part in candidate_parts(s):
            key = part.strip().lower()
            if not LETTER_RE.search(part): skipped["无字母"] += 1; continue
            if CJK_RE.search(part): skipped["已含中文"] += 1; continue
            if key in dict_keys: skipped["已在词典"] += 1; continue
            if key in CLOSED and key not in UI_KEEP: skipped["虚词(词沙拉源)"] += 1; continue
            if len(key) < 2: skipped["过短"] += 1; continue
            candidates.setdefault(part, func)

    phrases = {k: v for k, v in candidates.items() if " " in k.strip()}
    words = {k: v for k, v in candidates.items() if " " not in k.strip()}

    print(f"missing 记录: {sum(raw.values())} 条(去重 {len(raw)})")
    print(f"候选: {len(candidates)} 条 = 短语 {len(phrases)} + 单词 {len(words)}")
    print(f"跳过: {dict(skipped)}")
    print("高频渲染栈 top5:", bt_counter.most_common(5))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for k in candidates:
            f.write(json.dumps({"key": k}, ensure_ascii=False) + "\n")
    print(f"→ {args.out}")

    show = list(phrases.items())[:15]
    if show:
        print("短语样例:")
        for k, func in show:
            print(f"  [{func}] {k!r}")
    show = list(words.items())[:10]
    if show:
        print("单词样例:")
        for k, func in show:
            print(f"  [{func}] {k!r}")

    if args.rotate:
        arch = args.log.with_name("dfint-log-" + datetime.date.today().strftime("%Y%m%d") + ".log")
        args.log.rename(arch)
        print(f"日志已归档 → {arch} (下次游戏启动重建)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
