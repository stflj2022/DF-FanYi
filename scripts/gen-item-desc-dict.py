#!/usr/bin/env python3
"""物品描述句批量词典生成器(2026-09-11 铜矛问题根治)。

背景: v53 物品 tooltip 的说明句是运行时拼接( exe 内只有分段 "This is a " ),
词典精确匹配追不上组合 → collect 闭环只能靠玩家逐件 hover 收集。
本脚本按实测句式 "This is a {材质} {物品名}.  "(双尾空格, dfint-log 铁证
'copper spear'/'copper battle axe' 两条样本)做笛卡尔积批量生成候选键,
交 translate-exe-strings.py 翻译后 merge 入典。

范围: 武器(item_weapon)+弹药(item_ammo) × 武器级金属材质。
护甲/鞋类句式无样本, 未证实前不生成(等 Debug 日志闭环确认后再扩)。

用法:
    python3 scripts/gen-item-desc-dict.py \
        --game "/home/wu/Games/DF-v5306/Dwarf Fortress" \
        --out dfint-data/item-desc-keys.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# 武器级金属材质(游戏内小写显示名; vanilla_matgloss INORGANIC 带 ITEMS_WEAPON;
# 宁多勿漏 —— 多入词典只是多几行, 漏了玩家就看到英文)
WEAPON_METALS = [
    "copper", "bronze", "bismuth bronze", "nickel silver",
    "iron", "pig iron", "steel", "silver", "gold", "platinum",
    "aluminum", "electrum", "adamantine",
]

NAME_RE = re.compile(r"\[NAME:([^:\]]+):")


def item_names(txt: Path) -> list[str]:
    """从 vanilla item_*.txt 提取单数 NAME(显示名, 如 'battle axe')。"""
    out: list[str] = []
    for line in txt.read_text(encoding="cp437", errors="replace").splitlines():
        m = NAME_RE.search(line)
        if m:
            out.append(m.group(1).strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, help="游戏根目录")
    ap.add_argument("--out", required=True, help="输出 JSONL(每行 {key})")
    ap.add_argument("--sentence", default="This is a {mat} {item}.  ",
                    help="句式模板(默认实测双尾空格)")
    args = ap.parse_args()

    vanilla = Path(args.game) / "data" / "vanilla" / "vanilla_items" / "objects"
    names: list[str] = []
    for f in ("item_weapon.txt", "item_ammo.txt"):
        p = vanilla / f
        if p.exists():
            got = item_names(p)
            names.extend(got)
            print(f"  {f}: {len(got)} 物品名")
    if not names:
        raise SystemExit(f"未提取到物品名: {vanilla}")

    seen: dict[str, None] = {}
    for mat in WEAPON_METALS:
        for item in names:
            key = args.sentence.format(mat=mat, item=item)
            seen.setdefault(key, None)
    keys = list(seen)

    with open(args.out, "w", encoding="utf-8") as fh:
        for k in keys:
            fh.write(json.dumps({"key": k}, ensure_ascii=False) + "\n")
    print(f"生成 {len(keys)} 条候选键({len(WEAPON_METALS)} 材质 × {len(names)} 物品) → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
