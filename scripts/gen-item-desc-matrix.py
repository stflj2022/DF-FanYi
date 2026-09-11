#!/usr/bin/env python3
"""物品说明句全矩阵词典生成器(v2, 2026-09-11"一次全修")。

背景: v53 物品 tooltip 说明句是运行时拼接( exe 内只有分段 "This is a " /
"The material is " ), 词典精确匹配追不上组合。本脚本从游戏 vanilla 数据
系统性提取 物品名全集 × 材质名全集, 按实测句式 "This is a {mat} {item}.  "
(双尾空格, dfint 日志铁证)做笛卡尔积, 交 translate-exe-strings.py 翻译后
merge 入典。

材质源(全部从游戏数据提取, 不靠猜):
- 木:   vanilla_plants 树块 WOOD 材质 STATE_NAME(如 'carambola wood')
- 布丝毛: vanilla_plants THREAD/SILK/YARN 材质 STATE_NAME_ADJ(如 'pig tail')
- 金属/石/土/矿: vanilla_materials INORGANIC(显示名=ID 小写+下划线转空格)
- 皮革: 常见动物硬编码(creature+' leather'; 全量靠日志闭环兜底)

物理不合理的组合(如木剑)只是永不命中的白条, 无害; 宁多勿漏。
颜色句(The material is X)色词表未证实, 本轮不做 —— 等日志积累样本。

用法:
    python3 scripts/gen-item-desc-matrix.py \
        --game "/home/wu/Games/DF-v5306/Dwarf Fortress" \
        --out dfint-data/item-desc-matrix.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

NAME_RE = re.compile(r"\[NAME:([^:\]]+):")
PLANT_RE = re.compile(r"^\[PLANT:([^:\]]+)\]")
STATE_NAME_RE = re.compile(r"\[STATE_NAME(?:_ADJ)?:ALL_SOLID:([^:\]]+)\]")
STATE_NAME_SOLID_RE = re.compile(r"\[STATE_NAME(?:_ADJ)?:SOLID:([^:\]]+)\]")
MAT_TEMPLATE_RE = re.compile(r"\[USE_MATERIAL_TEMPLATE:[A-Z_]+:([A-Z_]+)_TEMPLATE\]")
INORGANIC_RE = re.compile(r"^\[INORGANIC:([A-Z_0-9]+)\]")

# exe 内置老式物品类型(不在 item_*.txt; 家具/备品/工场件; 宁多勿漏, 白条无害)
EXE_FURNITURE = [
    "chair", "bed", "table", "coffin", "armor stand", "weapon rack", "cabinet",
    "door", "floodgate", "hatch cover", "grate", "throne", "statue", "window",
    "cage", "barrel", "bucket", "bin", "box", "bag", "anvil", "slab",
    "quern", "millstone", "display case", "altar", "pedestal", "bookcase",
]
# 常见皮革来源(creature 名; 全量物种靠日志闭环兜底)
LEATHER_SOURCES = [
    "cow", "horse", "pig", "sheep", "goat", "dog", "donkey", "mule",
    "camel", "alpaca", "llama", "yak", "water buffalo", "cave crocodile",
    "black bear", "grizzly bear", "wolf", "troll", "giant rat", "retriever",
]


def _txt_names(txt: Path) -> list[str]:
    out: list[str] = []
    for line in txt.read_text(encoding="cp437", errors="replace").splitlines():
        m = NAME_RE.search(line)
        if m:
            out.append(m.group(1).strip())
    return out


def parse_plants(objects: Path) -> dict[str, list[str]]:
    """vanilla_plants → {wood, thread, silk, yarn} 材质显示名(去重有序)。"""
    cats = {"wood": [], "thread": [], "silk": [], "yarn": []}
    seen = {k: set() for k in cats}
    for txt in sorted(objects.glob("plant_*.txt")):
        cur_mat_tpl = None
        for line in txt.read_text(encoding="cp437", errors="replace").splitlines():
            m = MAT_TEMPLATE_RE.search(line)
            if m:
                cur_mat_tpl = m.group(1)
                continue
            if cur_mat_tpl is None:
                continue
            cat = {"WOOD": "wood", "THREAD_PLANT": "thread",
                   "SILK": "silk", "YARN": "yarn"}.get(cur_mat_tpl)
            if cat is None:
                continue
            for rx in (STATE_NAME_RE, STATE_NAME_SOLID_RE):
                sm = rx.search(line)
                if sm:
                    name = sm.group(1).strip()
                    if name and name.lower() not in seen[cat]:
                        seen[cat].add(name.lower())
                        cats[cat].append(name)
                    break
            if "[" in line and "STATE_NAME" not in line:
                cur_mat_tpl = None  # 材质块结束(下一条非 STATE_NAME 指令)
    return cats


CREATURE_RE = re.compile(r"^\[CREATURE:([^:\]]+)\]")
CREATURE_NAME_RE = re.compile(r"^\t\[NAME:([^:\]]+):")


def parse_creatures(objects: Path) -> dict[str, list[str]]:
    """vanilla_creatures → {silk, yarn}: 产丝/产毛动物显示名 + ' silk'/' wool'。

    creature 材质无独立 STATE_NAME(显示名 = 物种名+材质词, 如 'cave spider silk'),
    所以按 [CREATURE] 块提取 [NAME:], 块内有 SILK_TEMPLATE → 丝, 有 [YARN] → 毛。
    只扫常见来源文件(domestic/地下/other/equipment), 稀有物种靠日志闭环兕底。
    """
    cats = {"silk": [], "yarn": []}
    seen = {k: set() for k in cats}
    for fname in ("creature_domestic.txt", "creature_subterranean.txt",
                  "creature_other.txt", "creature_equipment.txt"):
        txt = objects / fname
        if not txt.exists():
            continue
        cname = None
        has_silk = has_yarn = False
        for line in txt.read_text(encoding="cp437", errors="replace").splitlines():
            m = CREATURE_RE.match(line.strip())
            if m:  # 新物种块: 收割上一块
                if cname and has_silk:
                    k = f"{cname} silk".lower()
                    if k not in seen["silk"]:
                        seen["silk"].add(k)
                        cats["silk"].append(f"{cname} silk")
                if cname and has_yarn:
                    k = f"{cname} wool".lower()
                    if k not in seen["yarn"]:
                        seen["yarn"].add(k)
                        cats["yarn"].append(f"{cname} wool")
                cname, has_silk, has_yarn = None, False, False
                continue
            nm = CREATURE_NAME_RE.match(line)
            if nm and cname is None:
                cname = nm.group(1).strip()
                continue
            if "SILK_TEMPLATE" in line:
                has_silk = True
            if re.search(r"\[YARN\]", line):
                has_yarn = True
        # 收割最后一块
        if cname and has_silk:
            cats["silk"].append(f"{cname} silk")
        if cname and has_yarn:
            cats["yarn"].append(f"{cname} wool")
    return cats


def parse_inorganic(objects: Path) -> dict[str, list[str]]:
    """vanilla_materials → {metal, stone_layer, mineral, soil}(ID 小写+下划线转空格)。"""
    files = {"metal": "inorganic_metal.txt", "stone_layer": "inorganic_stone_layer.txt",
             "mineral": "inorganic_stone_mineral.txt", "soil": "inorganic_stone_soil.txt"}
    out = {}
    for cat, fname in files.items():
        p = objects / fname
        names: list[str] = []
        if p.exists():
            for line in p.read_text(encoding="cp437", errors="replace").splitlines():
                m = INORGANIC_RE.match(line.strip())
                if m:
                    names.append(m.group(1).replace("_", " ").lower())
        out[cat] = names
    return out


def parse_items(objects: Path) -> dict[str, list[str]]:
    """vanilla_items → 物品名分类。"""
    files = {"armor": "item_armor.txt", "helm": "item_helm.txt",
             "pants": "item_pants.txt", "gloves": "item_gloves.txt",
             "shoes": "item_shoes.txt", "shield": "item_shield.txt",
             "weapon": "item_weapon.txt", "ammo": "item_ammo.txt",
             "tool": "item_tool.txt", "toy": "item_toy.txt",
             "trapcomp": "item_trapcomp.txt", "siegeammo": "item_siegeammo.txt"}
    out: dict[str, list[str]] = {}
    for cat, fname in files.items():
        p = objects / fname
        out[cat] = _txt_names(p) if p.exists() else []
    return out


def dedup(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for x in seq:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sentence", default="This is a {mat} {item}.  ")
    args = ap.parse_args()

    vanilla = Path(args.game) / "data" / "vanilla"
    plants = parse_plants(vanilla / "vanilla_plants" / "objects")
    creas = parse_creatures(vanilla / "vanilla_creatures" / "objects")
    inorg = parse_inorganic(vanilla / "vanilla_materials" / "objects")
    items = parse_items(vanilla / "vanilla_items" / "objects")

    cloth_items = dedup(items["armor"] + items["helm"] + items["pants"] +
                        items["gloves"] + items["shoes"] + ["cloth", "bag"])
    hard_items = dedup(items["tool"] + items["toy"] + items["shield"] +
                       items["trapcomp"] + items["siegeammo"] + EXE_FURNITURE)
    leather_items = dedup(cloth_items + ["backpack", "waterskin"])
    furniture = EXE_FURNITURE + ["quern", "millstone"]

    metals = dedup(inorg["metal"])
    stones = dedup(inorg["stone_layer"] + inorg["mineral"] + inorg["soil"])
    fibers = dedup(plants["thread"] + plants["silk"] + plants["yarn"] +
                   creas["silk"] + creas["yarn"])
    woods = dedup(plants["wood"])
    leathers = [f"{c} leather" for c in LEATHER_SOURCES]

    print(f"材质: 木 {len(woods)} / 布丝毛 {len(fibers)}(植物 {len(plants['thread'])}+丝 {len(creas['silk'])}+毛 {len(creas['yarn'])}) / "
          f"金属 {len(metals)} / 石土矿 {len(stones)} / 皮革 {len(leathers)}")
    print(f"物品: 布类 {len(cloth_items)} / 硬类 {len(hard_items)} / 家具 {len(furniture)}")

    combos: dict[str, None] = {}  # (mat,item) 去重, 保序
    def add(mats: list[str], items_: list[str]) -> None:
        for m in mats:
            for it in items_:
                combos.setdefault(args.sentence.format(mat=m, item=it), None)

    add(fibers, cloth_items)          # 布丝毛 × 衣物/布匹
    add(woods, hard_items)            # 木 × 工具/玩具/盾/备品家具
    add(metals, hard_items)           # 金属 × 硬类(武器弹药此前已做, merge 去重)
    add(stones, furniture)            # 石土矿 × 石器家具
    add(leathers, leather_items)      # 皮革 × 衣物/包袋

    keys = list(combos)
    with open(args.out, "w", encoding="utf-8") as fh:
        for k in keys:
            fh.write(json.dumps({"key": k}, ensure_ascii=False) + "\n")
    print(f"生成 {len(keys)} 条候选键 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
