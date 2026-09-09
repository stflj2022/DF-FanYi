#!/usr/bin/env python3
"""export-dfint-dict.py — 将引擎 TM(云端预翻)合并进游戏 dfint 词典(只填空缺, 不覆盖)

dfint(dictionaries/creatures.csv, plants.csv) 是游戏的"查表式"命名汉化;
引擎 translation_memory(cloud-pretranslate) 含 4691+ 条 raws 译文。
本脚本解析 raws → (id, table, text) 三要素 → 查 TM → 仅当该 (id, table)
在 dfint 词典中完全缺失时补写新行(社区已有译文绝不动)。

用法:
  python3 scripts/export-dfint-dict.py                # 试运行: 只报告将新增行
  python3 scripts/export-dfint-dict.py --write        # 正式: 先备份原 csv 再写
  python3 scripts/export-dfint-dict.py --legacy       # 另: TM 完整句子→legacy-dictionary.csv 补缺
  python3 scripts/export-dfint-dict.py --game PATH    # 指定游戏根(默认本机整合包)
  python3 scripts/export-dfint-dict.py --db PATH      # 指定引擎 L2 库路径

legacy 补写规则: 仅补长度 ≥20 字且含空格的完整句子(逐字等效匹配较安全,
短词/专名会污染回退表); 现有 text 键绝不动。
"""
import argparse
import csv
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TAG_RE = re.compile(r"\[([A-Z_0-9]+)(?::([^\]]*))?\]")


def read_lines(p: Path):
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


def parse_creatures(dirs):
    """yield (cid, kind, args_list); kind ∈ name/baby/child/prefstring/description"""
    for d in dirs:
        for f in sorted(Path(d).glob("objects/*.txt")):
            cid = None
            for line in read_lines(f):
                for m in TAG_RE.finditer(line):
                    tag, args = m.group(1), (m.group(2) or "").strip()
                    if tag == "CREATURE" and args:
                        cid = args
                        continue
                    if cid is None or not args:
                        continue
                    parts = args.split(":")
                    if tag == "NAME" and len(parts) >= 3:
                        yield cid, "name", parts[:3]
                    elif tag == "BABY" and len(parts) >= 2:
                        yield cid, "baby", parts[:2]
                    elif tag == "CHILD" and len(parts) >= 2:
                        yield cid, "child", parts[:2]
                    elif tag == "PREFSTRING":
                        yield cid, "prefstring", [args]
                    elif tag == "DESCRIPTION":
                        yield cid, "description", [args]


def parse_plants(dirs):
    """yield (pid, kind, args_list); pid 含 :GROWTH 后缀; kind ∈ name/growth/prefstring/seed"""
    for d in dirs:
        for f in sorted(Path(d).glob("objects/*.txt")):
            cur = None
            growth = None
            for line in read_lines(f):
                for m in TAG_RE.finditer(line):
                    tag, args = m.group(1), (m.group(2) or "").strip()
                    if tag == "PLANT" and args:
                        cur = args
                        growth = None
                        continue
                    if cur is None or not args:
                        continue
                    parts = args.split(":")
                    if tag == "GROWTH" and args:
                        growth = args
                    elif tag == "NAME" and len(parts) >= 3:
                        yield cur, "name", parts[:3]
                    elif tag == "GROWTH_NAME" and growth and len(parts) >= 2:
                        yield f"{cur}:{growth}", "growth", parts[:2]
                    elif tag == "PREFSTRING":
                        yield cur, "prefstring", [args]
                    elif tag == "SEED" and len(parts) >= 2:
                        yield cur, "seed", parts[:2]


def creature_rows(cid, kind, args):
    if kind == "name":
        return [(f"CREATURE:{cid}", f"CREATURE:NAME:{k}", args[i])
                for i, k in ((0, "SINGULAR"), (1, "PLURAL"), (2, "ADJECTIVE"))]
    if kind in ("baby", "child"):
        t = "BABY" if kind == "baby" else "CHILD"
        return [(f"CREATURE:{cid}", f"CREATURE:{t}:SINGULAR", args[0]),
                (f"CREATURE:{cid}", f"CREATURE:{t}:PLURAL", args[1])]
    if kind == "prefstring":
        return [(f"CREATURE:{cid}", "CREATURE:PREFSTRING", args[0])]
    if kind == "description":
        return [(f"CREATURE:{cid}:ALL", "CREATURE:CASTE:DESCRIPTION", args[0])]
    return []


def plant_rows(pid, kind, args):
    if kind == "name":
        return [(f"PLANT:{pid}", f"PLANT:NAME:{k}", args[i])
                for i, k in ((0, "SINGULAR"), (1, "PLURAL"), (2, "ADJECTIVE"))]
    if kind == "growth":
        return [(f"PLANT:{pid}", f"PLANT:GROWTH:NAME:SINGULAR", args[0]),
                (f"PLANT:{pid}", f"PLANT:GROWTH:NAME:PLURAL", args[1])]
    if kind == "prefstring":
        return [(f"PLANT:{pid}", "PLANT:PREFSTRING", args[0])]
    if kind == "seed":
        return [(f"PLANT:{pid}", "PLANT:SEED:SINGULAR", args[0]),
                (f"PLANT:{pid}", "PLANT:SEED:PLURAL", args[1])]
    return []


def load_tm(db_path: Path):
    """source_text → translated_text(首选 cloud-pretranslate, 其次 local)"""
    tm = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for provider in ("cloud-pretranslate", "local"):
            rows = con.execute(
                "SELECT source_text, translated_text FROM translation_memory "
                "WHERE provider=? AND source_text IS NOT NULL "
                "AND source_text <> '' AND translated_text <> ''",
                (provider,),
            ).fetchall()
            for src, tgt in rows:
                if src == tgt:
                    continue
                if src not in tm:
                    tm[src] = tgt
    finally:
        con.close()
    return tm


def load_existing(csv_path: Path):
    """返回 {(id, table)} 集合(社区已有行不动)"""
    keys = set()
    if not csv_path.exists():
        return keys
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[0] and row[0] != "id":
                keys.add((row[0].strip(), row[1].strip()))
    return keys


def load_text_keys(csv_path: Path):
    """legacy/simple 词典: 返回已有 source text 集合"""
    keys = set()
    if not csv_path.exists():
        return keys
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[0] and row[0] != "text":
                keys.add(row[0])
    return keys


def legacy_merge_plan(tm, legacy_path: Path):
    """TM → legacy 补写计划: 完整句子(≥20 字且含空格) 且 text 键不存在"""
    existing = load_text_keys(legacy_path)
    merged = []
    for src, tgt in sorted(tm.items()):
        if src in existing:
            continue
        if len(src) < 20 or " " not in src:
            continue
        merged.append((src, tgt))
    return merged


def build_game_rows(game_dir: Path):
    vanilla = game_dir / "data" / "vanilla"
    if not vanilla.exists():
        sys.exit(f"找不到 {vanilla}, 用 --game 指定游戏根目录")
    rows = {}
    rows["creatures"] = []
    for cid, kind, args in parse_creatures(
        [vanilla / "vanilla_creatures", vanilla / "vanilla_creatures_extinct"]
    ):
        for r in creature_rows(cid, kind, args):
            rows["creatures"].append(r)
    rows["plants"] = []
    for pid, kind, args in parse_plants([vanilla / "vanilla_plants"]):
        for r in plant_rows(pid, kind, args):
            rows["plants"].append(r)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="正式写入(默认只试运行预览)")
    ap.add_argument("--legacy", action="store_true", help="试运行: TM→legacy-dictionary.csv 补缺预览")
    ap.add_argument("--game", default="/home/wu/Games/DF-v5306/Dwarf Fortress")
    ap.add_argument("--db", default=REPO / "data" / "engine.db")
    args = ap.parse_args()

    game = Path(args.game)
    db = Path(args.db)
    tm = load_tm(db)
    if not tm:
        sys.exit("TM 为空, 先跑 pretranslate run/install?")
    print(f"TM 加载: {len(tm)} 条")

    if args.legacy:
        legacy_path = game / "dfint-data" / "legacy-dictionary.csv"
        merged = legacy_merge_plan(tm, legacy_path)
        if not args.write:
            print(f"[legacy] 现有 {len(load_text_keys(legacy_path))} 条 | 本次可补 {len(merged)}")
            for src, tgt in merged[:10]:
                print(f"    + {src[:60]} → {tgt[:30]}")
            sys.exit(0 if True else 1)
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = legacy_path.with_name(f"legacy-dictionary.csv.bak-{ts}")
        bak.write_bytes(legacy_path.read_bytes())
        with legacy_path.open("a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerows(merged)
        print(f"[legacy] 备份 → {bak.name}; 新增 {len(merged)} 条 → 重启游戏生效")
        sys.exit(0)

    game_rows = build_game_rows(game)
    total_existing = 0
    plan = []  # (csv_name, Path, [(id,table,text,translation)...])
    for fname in ("creatures", "plants"):
        csv_path = game / "dfint-data" / "dictionaries" / f"{fname}.csv"
        existing = load_existing(csv_path)
        total_existing += len(existing)
        added = []
        for cid, table, text in game_rows[fname]:
            if (cid, table) in existing:
                continue
            tgt = tm.get(text)
            if tgt:
                added.append((cid, table, text, tgt))
                existing.add((cid, table))  # 同批内去重
        plan.append((fname, csv_path, added))
        print(f"[{fname}] 解析候选 {len(game_rows[fname])} | 社区已有 {len(existing)} | 本次可补 {len(added)}")

    total_new = sum(len(n) for _, _, n in plan)
    print(f"\n合计新增 {total_new} 行")
    if not args.write:
        for fname, _, added in plan:
            for row in added[:10]:
                print(f"    + {fname}: {row[0]} | {row[1]} | {row[2][:40]} → {row[3][:30]}")
        print("(试运行, 加 --write 正式写入)")
        return

    ts = time.strftime("%Y%m%d-%H%M%S")
    for fname, csv_path, added in plan:
        if not added:
            continue
        bak = csv_path.with_name(f"{csv_path.stem}.csv.bak-{ts}")
        bak.write_bytes(csv_path.read_bytes())
        with csv_path.open(encoding="utf-8", newline="") as fh:
            original = list(csv.reader(fh))
        if not (original and original[0][:1] == ["id"]):
            original = [["id", "table", "text", "translation"]] + original
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerows(original)
            w.writerows(added)
        print(f"[{fname}] 备份 → {bak.name}; 原 {len(original)-1} 行 + 新 {len(added)} 行")
    print("完成: 重启游戏(或热载入)后 dfint 将加载新词条")


if __name__ == "__main__":
    main()