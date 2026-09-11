#!/usr/bin/env python3
"""从 dfint legacy 词典中删除封闭类虚词条目(词沙拉根治)。

背景: v53 新 UI 对长段落按单词逐次渲染(addst 逐词调用), 词典中的虚词条目
(the/of/to/your/and...)被逐词命中 → 「Prepare 至指引 你的肥硕」词沙拉。
虚词不可能作为 UI 标签单独渲染, 删除零风险; 名词/动词/形容词条目保留
(它们可能是 UI 项/描述词整串渲染场景)。

用法: python3 scripts/prune-dict-function-words.py [词典路径]
默认词典: 游戏目录 dfint-data/legacy-dictionary.csv (自动备份 .bak-prune-<date>)
"""
import csv, re, shutil, sys, datetime, os

GAME_DICT = "/home/wu/Games/DF-v5306/Dwarf Fortress/dfint-data/legacy-dictionary.csv"

# 封闭类虚词(冠词/介词/代词/连词/助动词/系动词/限定词/常用副词) — 可穷举
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

# UI 标签白名单: 键面为虚词形态但实为 DF 界面按钮/过滤选项, 永不删除
# (2026-09-11 审查 181 条被删条目后回补 15 条, 见工作总结第九节)
UI_KEEP = {"all", "done", "off", "no", "yes", "any", "other", "some", "might"}

def norm(k): return k.strip().lower()

def main(path):
    if not os.path.exists(path):
        sys.exit(f"词典不存在: {path}")
    bak = f"{path}.bak-prune-{datetime.date.today():%Y%m%d}"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print(f"已备份 → {bak}")
    rows = list(csv.reader(open(path, newline="", encoding="utf-8")))
    head, body = rows[0], rows[1:]
    keep, removed = [], []
    for r in body:
        (keep if norm(r[0]) not in CLOSED or norm(r[0]) in UI_KEEP else removed).append(r)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(keep)
    print(f"原 {len(body)} 条 → 删 {len(removed)} 条虚词 → 剩 {len(keep)} 条")
    for r in removed[:10]:
        print("  删:", repr(r[0]), "→", r[1])
    if len(removed) > 10:
        print(f"  ... 共 {len(removed)} 条")
    print("⚠️ 词典在游戏启动时加载, 需重启游戏生效")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else GAME_DICT)
