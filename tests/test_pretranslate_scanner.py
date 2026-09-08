"""ticket-011 验收: raws 扫描器 —— [TAG:value] 提取/分句/去重/scan 报告。

工单要求:
- 解析 data/vanilla/*.txt 的 [TAG:value] 结构, 提取可翻译文本
  ([DESCRIPTION:...]/[NAME:...]/[CASTE_NAME:...]/PREFSTRING/STATE_* 等);
- 分句、去重、hash(source_hash 复用 core/parser);
- 产出 scan 报告: 总句数/去重后句数/字符分布/抽样人工可读。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.core.parser import normalize_text, source_hash  # noqa: E402
from df_fanyi.core.protector import Protector  # noqa: E402
from df_fanyi.pretranslate.scanner import (  # noqa: E402
    Segment,
    scan,
    scan_file,
    split_sentences,
    write_report,
)

FIXTURES = REPO / "tests" / "fixtures" / "raws"


def _engine_hash(text: str) -> str:
    """运行时引擎的缓存 key: normalize → protect → source_hash(编排器同款)。"""
    protected, _ = Protector().protect(normalize_text(text))
    return source_hash(protected)


class TestSplitSentences:
    def test_single_sentence_stays_whole(self) -> None:
        s = "A small blue-crested bird living in temperate woodlands."
        assert split_sentences(s) == [s]

    def test_multi_sentence_split_keeps_punctuation(self) -> None:
        assert split_sentences("It is a large predator. It hunts at night! Can it fly?") == [
            "It is a large predator.",
            "It hunts at night!",
            "Can it fly?",
        ]

    def test_no_terminal_period_keeps_tail(self) -> None:
        assert split_sentences("It is large. Very large") == ["It is large.", "Very large"]

    def test_abbrev_decimals_not_split(self) -> None:
        text = "It weighs 2.5 tons."
        assert split_sentences(text) == [text]


class TestScanFile:
    def test_creature_tags_extracted_with_fields(self) -> None:
        segs = scan_file(FIXTURES / "creature_small.txt", rel_path="creature_small.txt")
        texts = {s.text for s in segs}
        # NAME 3 字段: 单数/复数/形容词(重复值由去重处理, 这里只断言值被提取)
        for expect in (
            "blue jay",
            "blue jays",
            "blue jay hatchling",
            "blue jay hatchlings",
            "coloration",
            "A small blue-crested bird living in temperate woodlands, known for its harsh chirps.",
            "It is a large predator.",
            "It hunts at night!",
            "Can it fly?",
        ):
            assert expect in texts, f"缺少: {expect}"

    def test_non_translatable_tags_and_comments_ignored(self) -> None:
        segs = scan_file(FIXTURES / "creature_small.txt", rel_path="creature_small.txt")
        texts = {s.text for s in segs}
        # CREATURE_TILE/COLOR/BIOME/APPLY_* 数值字段不是可翻译文本
        assert "249" not in texts
        assert "GRASSLAND_TEMPERATE" not in texts
        # 行尾注释(] 之后的自由文本)不属于任何 [TAG:value]
        assert not any("56 kph" in t for t in texts)

    def test_state_name_skips_state_key_field(self) -> None:
        segs = scan_file(FIXTURES / "material_small.txt", rel_path="material_small.txt")
        texts = {s.text for s in segs}
        # STATE_NAME:SOLID:gold dust → 提取值字段, 状态键 SOLID 不提取
        assert "gold dust" in texts
        assert "SOLID" not in texts
        assert "iron" in texts  # STATE_NAME_ADJ:ALL_SOLID:iron
        assert "molten iron" in texts
        assert "ALL_SOLID" not in texts

    def test_adj_tag_excluded(self) -> None:
        segs = scan_file(FIXTURES / "plant_small.txt", rel_path="plant_small.txt")
        assert "single-grain wheat" in {s.text for s in segs}
        # ADJ 刻意不在提取白名单(ticket-011 范围: 名称/描述优先)
        assert not any(s.tag == "ADJ" for s in segs)

    def test_kind_classification(self) -> None:
        segs = scan_file(FIXTURES / "creature_small.txt", rel_path="creature_small.txt")
        by_text = {s.text: s for s in segs}
        assert by_text["blue jay"].kind == "name"
        assert by_text["coloration"].kind == "name"
        assert (
            by_text[
                "A small blue-crested bird living in temperate woodlands, known for its harsh chirps."
            ].kind
            == "description"
        )

    def test_segment_carries_provenance_and_engine_hash(self) -> None:
        segs = scan_file(FIXTURES / "item_small.txt", rel_path="item_small.txt")
        name = next(s for s in segs if s.text == "short sword")
        assert name.tag == "NAME"
        assert name.rel_path == "item_small.txt"
        assert name.line > 0
        assert name.hash == _engine_hash("short sword")
        assert isinstance(name, Segment)


class TestScan:
    def test_totals_and_dedup_across_files(self) -> None:
        result = scan(FIXTURES)
        assert result.files_scanned == 4
        # 去重前: creature 16 段 + material 6 + item 4 + plant 3 = 29
        assert result.total_segments == 29
        # 跨文件去重后 23(plants 的 NAME/NAME_PLURAL 同值, creature 内 NAME/CASTE_NAME 同值等)
        assert result.unique_segments == 23
        assert result.duplicates == 6
        # 抽取的原始可翻译字段值(分句前): 14+5+3+3 = 25
        assert result.total_values == 25
        assert len(result.segments) == 23

    def test_hashes_unique_and_engine_compatible(self) -> None:
        result = scan(FIXTURES)
        hashes = [s.hash for s in result.segments]
        assert len(hashes) == len(set(hashes))
        for seg in result.segments:
            assert seg.hash == _engine_hash(seg.text)

    def test_kind_counts(self) -> None:
        result = scan(FIXTURES)
        kinds = {s.kind for s in result.segments}
        assert kinds == {"name", "description"}
        assert sum(1 for s in result.segments if s.kind == "description") == 8
        assert sum(1 for s in result.segments if s.kind == "name") == 15

    def test_char_distribution_covers_all_segments(self) -> None:
        result = scan(FIXTURES)
        assert sum(result.char_distribution.values()) == 23
        assert all(v >= 0 for v in result.char_distribution.values())

    def test_deterministic_order(self) -> None:
        a = scan(FIXTURES)
        b = scan(FIXTURES)
        assert [s.hash for s in a.segments] == [s.hash for s in b.segments]

    def test_subdirs_filter(self) -> None:
        result = scan(FIXTURES, subdirs=["creature_dir"])
        # fixture 目录本身没有子目录结构: 过滤后扫不到文件
        assert result.files_scanned == 0


class TestWriteReport:
    def test_report_contains_required_stats_and_samples(self, tmp_path: Path) -> None:
        result = scan(FIXTURES)
        out = tmp_path / "PRETRANSLATE_SCAN.md"
        write_report(result, out, scan_root=str(FIXTURES))
        text = out.read_text(encoding="utf-8")
        assert "23" in text  # 去重后句数
        assert "29" in text  # 分句后总段数
        assert "25" in text  # 字段值总数
        # 抽样 10 句人工可读: 报告含样本句原文
        assert "blue jay" in text
        assert result.sample(10)[0].text in text

    def test_sample_returns_at_most_n(self) -> None:
        result = scan(FIXTURES)
        assert len(result.sample(10)) == 10
        assert len(result.sample(1000)) == 23


class TestWhitespaceNormalization:
    def test_values_are_normalized(self, tmp_path: Path) -> None:
        f = tmp_path / "x.txt"
        f.write_text("[OBJECT:X]\n[CREATURE:X]\n\t[NAME:spaced   name]\n", encoding="utf-8")
        segs = scan_file(f, rel_path="x.txt")
        assert [s.text for s in segs] == ["spaced name"]
        assert segs[0].hash == _engine_hash("spaced name")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
