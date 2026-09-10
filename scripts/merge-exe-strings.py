#!/usr/bin/env python3
"""merge-exe-strings.py — 把翻译结果合并进 dfint legacy 词典(只补缺, 不覆盖)

- 追加到游戏词典 legacy-dictionary.csv 与仓库 dfint-data/legacy-dictionary.csv
- 生成增量包 dfint-data/legacy-extras-YYYYMMDD.csv(与 a081666 同款)
- 所有写入前校验: key 唯一、CSV 可解析、每行 ≥2 字段
用法:
  python3 scripts/merge-exe-strings.py --in dfint-data/exe-translated-20260910.jsonl --write
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GAME_DICT = Path("/home/wu/Games/DF-v5306/Dwarf Fortress/dfint-data/legacy-dictionary.csv")
REPO_DICT = REPO / "dfint-data" / "legacy-dictionary.csv"


def read_dict(path: Path) -> tuple[dict[str, str], list[str]]:
    """返回 (key->translation, 原始行顺序保留的键列表)"""
    mapping, order = {}, []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if not row:
                continue
            if row[0] == "text":
                continue
            mapping[row[0]] = row[1] if len(row) > 1 else ""
            order.append(row[0])
    return mapping, order


def write_dict(path: Path, mapping: dict[str, str], order: list[str]) -> None:
    seen = set()
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["text", "translation"])
        for k in order:
            if k in seen:
                continue
            seen.add(k)
            w.writerow([k, mapping[k]])
    print(f"  {path}: {len(mapping)} 行")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.inp).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"译文条数: {len(rows)}")

    # key 唯一性
    dup = [k for k, c in __import__("collections").Counter(r["key"] for r in rows).items() if c > 1]
    if dup:
        print(f"!!! 译文含重复 key {len(dup)} 条: {dup[:5]}", file=sys.stderr)
        return 1
    # 值校验
    empty = [r["key"] for r in rows if not r["zh"].strip()]
    if empty:
        print(f"!!! {len(empty)} 条空译文", file=sys.stderr)
        return 1

    newmap = {r["key"]: r["zh"] for r in rows}
    stamp = time.strftime("%Y%m%d")

    for label, path in (("游戏", GAME_DICT), ("仓库", REPO_DICT)):
        if not path.exists():
            print(f"跳过 {label} 词典(不存在): {path}", file=sys.stderr)
            continue
        mapping, order = read_dict(path)
        before = len(mapping)
        added = 0
        for k, v in newmap.items():
            if k not in mapping:
                mapping[k] = v
                order.append(k)
                added += 1
        print(f"[{label}] {path.name}: {before} → {len(mapping)} (+{added})")
        if args.write:
            write_dict(path, mapping, order)
        else:
            print(f"  (试运行, 未写入; 加 --write 生效)")

    # 增量包
    extras = REPO / "dfint-data" / f"legacy-extras-{stamp}.csv"
    if args.write:
        with extras.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["text", "translation"])
            for r in rows:
                w.writerow([r["key"], r["zh"]])
        print(f"增量包: {extras} ({len(rows)} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
