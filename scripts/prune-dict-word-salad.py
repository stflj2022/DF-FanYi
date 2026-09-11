#!/usr/bin/env python3
"""
根治 v53 长段落词沙拉: 删除 legacy 词典中所有"全小写单词级"条目。

根因: DF v53 对长段落(textviewer 弹窗/功能区说明/帮助页)逐词调用 addst,
词典里任何单词级条目都会被逐词命中 → 词沙拉("功能区 are 区块 you 指定...").
上一轮只删了封闭类虚词(prune-dict-function-words.py), 实词单词条目仍造成词沙拉.

本脚本删除规则(仅此一类):
  - 键去掉 [..] 标记后, 无空格、非空、首字母小写 的条目(普通英语句子词)
  - 保留 UI_KEEP 白名单(all/done/off/no/yes/any/other/some/might)
  - 保留首字母大写条目(DF 专名/UI 标签: Zones→功能区, Worship 等)
  - 保留多词短语/物品矩阵整句(零误删, 已有核验)

副作用: 词沙拉场景回退为英文原文, 由 fanyi 字幕条整句翻译接管.
注意: 词典为 static_init 启动时加载, 需重启游戏生效.

用法: python3 prune-dict-word-salad.py [--dict legacy-dictionary.csv] [--write]
不传 --write 只预览删除数量, 不落盘.
"""
import argparse
import csv
import re

UI_KEEP = {"all", "done", "off", "no", "yes", "any", "other", "some", "might"}


def norm(key: str) -> str:
    return re.sub(r"\[[^\[\]]*\]", "", key).strip()


def is_word_salad_culprit(key: str) -> bool:
    core = norm(key)
    if not core:
        return False
    if " " in core or "\t" in core:
        return False
    if core[0].isupper():
        return False
    if core.lower() in UI_KEEP:
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dict", default="legacy-dictionary.csv")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    with open(args.dict, encoding="utf-8") as f:
        rows = [r for r in csv.reader(f) if r]
    head, body = rows[0], rows[1:]

    keep, removed = [], []
    for r in body:
        (removed if is_word_salad_culprit(r[0]) else keep).append(r)

    print(f"词典 {len(body)} → 待删 {len(removed)} → 保留 {len(keep)}")
    if not args.write:
        print("(预览模式, 未写盘; 加 --write 生效)")
        return

    with open(args.dict, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(keep)
    print(f"已写盘: {args.dict}")


if __name__ == "__main__":
    main()
