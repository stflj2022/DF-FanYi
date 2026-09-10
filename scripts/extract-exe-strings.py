#!/usr/bin/env python3
"""extract-exe-strings.py — 从 Dwarf Fortress.exe 抽取句串并过滤噪音, 对照词典找缺失

输出 JSONL: {"key": <exe原串>}  → 供 translate-exe-strings.py 批量翻译。

安全策略(与 a081666 一致):
- 只保留"完整句串"(去掉方括号标记后 ≥20 字符且含空格) — 短词/专名留给
  dfint lookups 词表, 避免污染精确匹配回退表
- 结构性噪音剔除: XML/URL/邮箱/路径/扩展名/C++符号/模板ID/二进制残留
用法:
  python3 scripts/extract-exe-strings.py [--exe PATH] [--dict PATH] [-o OUT.jsonl]
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

MARKUP_RE = re.compile(r"\[[^\[\]]{1,48}\]")  # [C:7:0:1] [B] [VAR:NATIVENAME:MEETING:ACTOR] 等

NOISE_RESIDUAL = [  # 在"去掉标记后的正文"上匹配
    re.compile(p) for p in (
        r"<[a-zA-Z!/?][^>]*>",                 # XML/HTML 标签
        r"https?://|www\.|\.(com|org|net)\b",  # URL
        r"[\w.\-]+@[\w.\-]+\.[a-z]{2,}",       # 邮箱
        r"(?i)\.(exe|dll|so|png|csv|json|xml|lua|txt|zip|ttf|otf|mp3|ogg|wav|pdb|lib|obj)\b",
        r"/(usr|home|opt|lib|bin|etc|tmp|dev|proc)/",  # unix 路径
        r"[A-Za-z]:[\\/]",                     # win 盘符路径
        r"::",                                 # C++ 作用域
        r"\\[ntrfv]",                          # 字面转义
        r"\{[A-Z0-9_]{3,}\}",                  # {TEMPLATE_ID}
        r"\b0x[0-9a-fA-F]{4,}\b",              # 十六进制地址
        r"\b(nullptr|std|cdecl|stdcall|WINAPI|GL_[A-Z]{2,})\b",
        r"[\x00-\x08\x0b\x0c\x0e-\x1f]",       # 控制字符
        r"^\s*[%~^|]+\s*$",                    # 纯符号
    )
]


def residual_of(s: str) -> str:
    return MARKUP_RE.sub(" ", s)


def is_noise(s: str) -> str | None:
    """返回剔除原因, None=保留"""
    resid = residual_of(s)
    if len(resid) < 20:
        return "too-short(<20)"
    if " " not in resid.strip():
        return "no-space"
    if len(s) > 2000:
        return "too-long(>2000)"
    words = re.findall(r"[A-Za-z]{2,}", resid)
    if len(words) < 2:
        return "too-few-words"
    letters = sum(c.isalpha() for c in resid)
    nonspace = len(resid.replace(" ", ""))
    if nonspace and letters / nonspace < 0.55:
        return "low-letter-ratio"
    for pat in NOISE_RESIDUAL:
        if pat.search(resid):
            return f"noise:{pat.pattern[:28]}"
    return None


def load_dict_keys(path: Path) -> set[str]:
    keys = set()
    with path.open(encoding="utf-8") as f:
        for row in csv.reader(f):
            if row and row[0] and row[0] != "text":
                keys.add(row[0])
    return keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="/home/wu/Games/DF-v5306/Dwarf Fortress/Dwarf Fortress.exe")
    ap.add_argument("--dict", default="/home/wu/Games/DF-v5306/Dwarf Fortress/dfint-data/legacy-dictionary.csv")
    ap.add_argument("-o", "--out", default="dfint-data/exe-missing-20260910.jsonl")
    args = ap.parse_args()

    exe, dic = Path(args.exe), Path(args.dict)
    if not exe.is_file():
        print(f"exe 不存在: {exe}", file=sys.stderr)
        return 1
    keys = load_dict_keys(dic)
    print(f"词典键数: {len(keys)}")

    raw = subprocess.run(
        ["strings", "-n", "8", str(exe)], capture_output=True, text=True, errors="replace"
    ).stdout
    candidates = {ln.strip() for ln in raw.splitlines() if ln.strip()}
    print(f"strings 去重候选: {len(candidates)}")

    reasons: dict[str, int] = {}
    kept: list[str] = []
    for s in sorted(candidates):
        if s in keys:
            continue
        why = is_noise(s)
        if why:
            key = why.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        else:
            kept.append(s)
    print(f"词典缺失: {len(candidates) - len(kept) - sum(reasons.values()) + len(kept)} 已在词典/剔除汇总: {reasons}")
    print(f"待翻译句串: {len(kept)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for s in kept:
            f.write(json.dumps({"key": s}, ensure_ascii=False) + "\n")
    print(f"已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
