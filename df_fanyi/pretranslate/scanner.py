"""raws 扫描器(ticket-011): 从 vanilla raws 提取可翻译文本。

工程书依据:
- §8.2(TM 表): 预翻译句装入 L2 translation_memory, 运行时零延迟命中;
- §24(验证): 分句/去重/hash 与运行时编排器同款(normalize → protect → source_hash),
  保证预翻译条目能被运行时缓存 key 精确命中;
- §46(privacy): 扫描器只读游戏文件, 不产生网络流量。

提取白名单(名称/描述优先, 工单范围): DESCRIPTION / NAME / CASTE_NAME /
GENERAL_CHILD_NAME / NAME_PLURAL / NAME_MALE / NAME_FEMALE / PREFSTRING /
STATE_NAME / STATE_NAME_ADJ / STATE_ADJ。
STATE_* 的第 0 字段是状态键(SOLID/LIQUID 等), 不提取。
ADJ/CREATURE_TILE/COLOR/BIOME 等非叙事字段刻意排除。

说明: vanilla raws 大量模板复用, 跨文件去重必不可少(工单预估 17 万词 →
去重后 3-6 万句)。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from df_fanyi.core.parser import normalize_text, source_hash
from df_fanyi.core.protector import Protector

# ---------------------------------------------------------------------------
# 可翻译 tag 白名单: tag → (kind, value_field_indexes)
# kind: "name"(名称/短语, 整值入 TM+terminology) | "description"(描述, 先分句)
# fields: 取冒号分隔后的字段下标; "all"=全部非空字段; STATE_* 第 0 字段是状态键
# ---------------------------------------------------------------------------
TRANSLATABLE_TAGS: dict[str, tuple[str, tuple[int, ...] | None]] = {
    "DESCRIPTION": ("description", (0,)),
    "NAME": ("name", None),
    "CASTE_NAME": ("name", None),
    "GENERAL_CHILD_NAME": ("name", None),
    "NAME_PLURAL": ("name", None),
    "NAME_MALE": ("name", None),
    "NAME_FEMALE": ("name", None),
    "PREFSTRING": ("name", None),
    "STATE_NAME": ("name", (1,)),
    "STATE_NAME_ADJ": ("name", (1,)),
    "STATE_ADJ": ("name", (1,)),
}

# [TAG:value1:value2] —— 值内不跨方括号(raws 叙事值不含嵌套方括号)
_TAG_RE = re.compile(r"\[([A-Z_][A-Z0-9_]*):([^\[\]]*)\]")

# 保守英文分句: .!?\ 后跟空白再跟大写/数字/引号才断(小数 "2.5"、缩写不被误切)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])[\"']?\s+(?=[\"'A-Z0-9])")

# ticket-011 默认扫描范围: 叙事内容目录(vanilla_languages 字典词与
# vanilla_text 的 TEXT_SET 模板文本不含玩家可见直译句, 刻意排除)
DEFAULT_SUBDIRS: tuple[str, ...] = (
    "vanilla_creatures",
    "vanilla_creatures_extinct",
    "vanilla_items",
    "vanilla_plants",
    "vanilla_materials",
    "vanilla_descriptors",
    "vanilla_entities",
    "vanilla_buildings",
    "vanilla_interactions",
    "vanilla_environment",
)


def split_sentences(text: str) -> list[str]:
    """保守英文分句: 保留终止标点; 尾句无标点原样保留。"""
    text = text.strip()
    if not text:
        return []
    parts = _SENTENCE_BOUNDARY.split(text)
    return [p.strip() for p in parts if p and p.strip()]


@dataclass(frozen=True)
class Segment:
    """一条可翻译片段(分句后的单句, 或单个名称值)。"""

    hash: str  # 运行时缓存 key: source_hash(protect(normalize(text)))
    text: str  # 规范化原文
    kind: str  # "name" | "description"
    tag: str  # 来源 raws tag(如 CASTE_NAME)
    rel_path: str  # 来源文件(相对扫描根)
    line: int  # 1-based 行号


def _tag_values(tag: str, raw_value: str) -> list[str]:
    """按白名单语义取出一个 tag 的可翻译字段值(规范化, 去空)。"""
    kind, fields = TRANSLATABLE_TAGS[tag]
    del kind  # kind 由调用方经 TRANSLATABLE_TAGS[tag][0] 取
    parts = raw_value.split(":")
    if fields is None:
        idx = range(len(parts))
    else:
        idx = [i for i in fields if i < len(parts)]
    values: list[str] = []
    for i in idx:
        value = normalize_text(parts[i])
        if value:
            values.append(value)
    return values


def scan_file(path: str | Path, *, rel_path: str | None = None) -> list[Segment]:
    """扫描单个 raws 文件, 返回去重后的 Segment 列表(文件内去重)。"""
    path = Path(path)
    rel = rel_path or path.name
    protector = Protector()
    seen: dict[str, Segment] = {}
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        for match in _TAG_RE.finditer(line):
            tag, raw_value = match.group(1), match.group(2)
            spec = TRANSLATABLE_TAGS.get(tag)
            if spec is None:
                continue
            kind = spec[0]
            for value in _tag_values(tag, raw_value):
                if kind == "description":
                    pieces = split_sentences(value)
                else:
                    pieces = [value]
                for piece in pieces:
                    protected, _ = protector.protect(piece)
                    digest = source_hash(protected)
                    if digest not in seen:
                        seen[digest] = Segment(
                            hash=digest, text=piece, kind=kind, tag=tag,
                            rel_path=rel, line=lineno,
                        )
    return list(seen.values())


@dataclass
class ScanResult:
    """扫描汇总(工单验收: 总句数/去重句数/字符分布/抽样)。"""

    segments: list[Segment]  # 去重后, 稳定排序(文件, 行号)
    total_values: int  # 可翻译字段值总数(分句前)
    total_segments: int  # 分句后片段总数(去重前)
    duplicates: int  # 重复片段数
    files_scanned: int
    char_distribution: dict[str, int]  # 字符长度分桶(去重后)
    per_dir: dict[str, int]  # 每个一级子目录的去重片段数

    def sample(self, n: int) -> list[Segment]:
        """前 n 条(扫描顺序稳定, 供报告人工抽查)。"""
        return self.segments[:n]

    @property
    def unique_segments(self) -> int:
        return len(self.segments)

    @property
    def name_count(self) -> int:
        return sum(1 for s in self.segments if s.kind == "name")

    @property
    def description_count(self) -> int:
        return sum(1 for s in self.segments if s.kind == "description")


_CHAR_BUCKETS: tuple[tuple[str, int], ...] = (
    ("1-10", 10), ("11-20", 20), ("21-40", 40), ("41-80", 80), ("81-160", 160), ("160+", 1 << 30),
)


def _char_bucket(text: str) -> str:
    n = len(text)
    for label, upper in _CHAR_BUCKETS:
        if n <= upper:
            return label
    return "160+"


def _iter_raws_files(root: Path, subdirs: Iterable[str] | None) -> list[Path]:
    if subdirs is None:
        return sorted(root.rglob("*.txt"))
    files: list[Path] = []
    for sub in subdirs:
        base = root / sub
        if base.is_dir():
            files.extend(sorted(base.rglob("*.txt")))
    return sorted(set(files))


def scan(root: str | Path, subdirs: Iterable[str] | None = None) -> ScanResult:
    """扫描 raws 根目录(或指定子目录集合), 跨文件去重。

    subdirs=None 表示递归全量; 给定列表则只扫这些一级子目录(默认范围见
    DEFAULT_SUBDIRS, 由 config pretranslate.subdirs 传入)。
    """
    root_path = Path(root)
    protector = Protector()  # 单实例复用(protect 是纯函数, 无状态污染)
    seen: dict[str, Segment] = {}
    total_values = 0
    total_segments = 0
    files = _iter_raws_files(root_path, subdirs)
    per_dir: dict[str, int] = {}

    for path in files:
        rel = path.relative_to(root_path).as_posix()
        dir_key = rel.split("/")[0] if "/" in rel else "(root)"
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            for match in _TAG_RE.finditer(line):
                tag, raw_value = match.group(1), match.group(2)
                spec = TRANSLATABLE_TAGS.get(tag)
                if spec is None:
                    continue
                kind = spec[0]
                for value in _tag_values(tag, raw_value):
                    total_values += 1
                    pieces = split_sentences(value) if kind == "description" else [value]
                    for piece in pieces:
                        total_segments += 1
                        protected, _ = protector.protect(piece)
                        digest = source_hash(protected)
                        if digest in seen:
                            continue
                        seen[digest] = Segment(
                            hash=digest, text=piece, kind=kind, tag=tag,
                            rel_path=rel, line=lineno,
                        )
                        per_dir[dir_key] = per_dir.get(dir_key, 0) + 1

    segments = sorted(seen.values(), key=lambda s: (s.rel_path, s.line, s.text))
    char_distribution: dict[str, int] = {label: 0 for label, _ in _CHAR_BUCKETS}
    for seg in segments:
        char_distribution[_char_bucket(seg.text)] += 1
    duplicates = total_segments - len(segments)
    return ScanResult(
        segments=segments,
        total_values=total_values,
        total_segments=total_segments,
        duplicates=duplicates,
        files_scanned=len(files),
        char_distribution=char_distribution,
        per_dir=per_dir,
    )


def write_report(result: ScanResult, path: str | Path, *, scan_root: str = "") -> Path:
    """scan 报告 → Markdown(工单产出 docs/audits/PRETRANSLATE_SCAN.md)。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# PRETRANSLATE_SCAN — vanilla raws 扫描报告(ticket-011)",
        "",
        f"- 扫描根: `{scan_root or '(未记录)'}`",
        f"- 扫描文件数: **{result.files_scanned}**",
        f"- 可翻译字段值(分句前): **{result.total_values}**",
        f"- 分句后片段总数: **{result.total_segments}**",
        f"- 去重后片段数: **{len(result.segments)}**(重复 {result.duplicates} 条)",
        f"- 名称类: **{result.name_count}** / 描述类: **{result.description_count}**",
        "",
        "## 字符长度分布(去重后)",
        "",
        "| 长度区间 | 句数 |",
        "| --- | --- |",
    ]
    for label, _ in _CHAR_BUCKETS:
        lines.append(f"| {label} | {result.char_distribution.get(label, 0)} |")
    lines += ["", "## 按目录分布(去重后)", "", "| 目录 | 句数 |", "| --- | --- |"]
    for key in sorted(result.per_dir, key=lambda k: -result.per_dir[k]):
        lines.append(f"| {key} | {result.per_dir[key]} |")
    lines += ["", "## 抽样(前 10 句人工抽查)", "", "| # | 原文 | tag | 来源 |", "| --- | --- | --- | --- |"]
    for i, seg in enumerate(result.sample(10), start=1):
        text = seg.text.replace("|", "\\|")
        lines.append(f"| {i} | {text} | {seg.tag} | {seg.rel_path}:{seg.line} |")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
