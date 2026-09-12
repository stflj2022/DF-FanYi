"""截图翻译工作流(shot) 单元测试: TSV 解析/段落重组/噪声过滤/CLI 解析。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from df_fanyi.cli import build_parser
from df_fanyi.shot import (
    OcrWord,
    assemble_paragraphs,
    collapse_cjk_spaces,
    parse_tsv,
    render_markdown,
    split_runs,
    translate_paragraphs,
)


def _tsv(level, page, block, par, line, word, left, top, w, h, conf, text):
    return f"{level}\t{page}\t{block}\t{par}\t{line}\t{word}\t{left}\t{top}\t{w}\t{h}\t{conf}\t{text}"


HEADER = "\t".join(
    ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
     "left", "top", "width", "height", "conf", "text"]
)


class TestParseTsv:
    def test_header_skipped_and_words_parsed(self):
        raw = "\n".join([
            HEADER,
            _tsv(5, 1, 1, 1, 1, 1, 10, 20, 30, 40, 95.0, "The"),
            _tsv(5, 1, 1, 1, 1, 2, 44, 20, 30, 40, 91.5, "dog"),
        ])
        words = parse_tsv(raw)
        assert len(words) == 2
        assert words[0].text == "The" and words[0].conf == 95.0
        assert words[1].block == 1 and words[1].par == 1

    def test_low_confidence_dropped(self):
        raw = "\n".join([
            HEADER,
            _tsv(5, 1, 1, 1, 1, 1, 0, 0, 1, 1, 10.0, "noise"),
            _tsv(5, 1, 1, 1, 1, 2, 5, 0, 1, 1, 80.0, "keep"),
        ])
        words = parse_tsv(raw)
        assert [w.text for w in words] == ["keep"]

    def test_non_word_levels_dropped(self):
        raw = "\n".join([
            _tsv(4, 1, 1, 1, 1, 1, 0, 0, 1, 1, 99.0, "line-level"),
        ])
        assert parse_tsv(raw) == []

    def test_malformed_rows_ignored(self):
        raw = "\n".join([
            "garbage row without tabs",
            _tsv(5, 1, 1, 1, 1, 1, 0, 0, 1, 1, "nan", "x"),
            _tsv(5, 1, 1, 1, 1, 2, 0, 0, 1, 1, 90.0, "ok"),
        ])
        words = parse_tsv(raw)
        assert [w.text for w in words] == ["ok"]


class TestAssembleParagraphs:
    def test_wrapped_lines_joined_into_one_paragraph(self):
        # DF 折行: 同 block/par 的两行应合成一句
        words = [
            OcrWord(1, 1, 1, 10, 90.0, "The"),
            OcrWord(1, 1, 1, 40, 90.0, "dog"),
            OcrWord(1, 1, 2, 10, 90.0, "bites"),
            OcrWord(1, 1, 2, 60, 90.0, "hard"),
        ]
        assert assemble_paragraphs(words) == ["The dog bites hard"]

    def test_words_ordered_by_left(self):
        words = [
            OcrWord(1, 1, 1, 60, 90.0, "hard"),
            OcrWord(1, 1, 1, 10, 90.0, "bites"),
        ]
        assert assemble_paragraphs(words) == ["bites hard"]

    def test_separate_paragraphs_kept_ordered(self):
        words = [
            OcrWord(1, 1, 1, 0, 90.0, "First"),
            OcrWord(1, 1, 1, 50, 90.0, "para"),
            OcrWord(2, 1, 1, 0, 90.0, "Second"),
            OcrWord(2, 1, 1, 60, 90.0, "para"),
        ]
        assert assemble_paragraphs(words) == ["First para", "Second para"]

    def test_map_noise_filtered(self):
        # 数字为主 + 无元音噪声
        noise = [
            OcrWord(1, 1, 1, 0, 90.0, "1234"),
            OcrWord(1, 2, 1, 0, 90.0, "grrk"),
        ]
        assert assemble_paragraphs(noise) == []

    def test_chinese_paragraph_accepted(self):
        # 已汉化 UI 混排段不应被英文噪声过滤器丢掉
        words = [
            OcrWord(1, 1, 1, 0, 90.0, "加"),
            OcrWord(1, 1, 1, 20, 90.0, "能"),
            OcrWord(1, 1, 1, 40, 90.0, "区"),
            OcrWord(1, 1, 1, 60, 88.0, "are"),
            OcrWord(1, 1, 1, 80, 88.0, "placed"),
        ]
        assert assemble_paragraphs(words) == ["加 能 区 are placed"]

    def test_dedupe_and_cap(self):
        words = []
        for i in range(20):
            words.extend([
                OcrWord(i + 1, 1, 1, 0, 90.0, f"para{i}"),
                OcrWord(i + 1, 1, 1, 50, 90.0, "text"),
            ])
            # 再来一份完全相同的段落 → 去重
            words.extend([
                OcrWord(i + 1, 1, 1, 0, 90.0, f"para{i}"),
                OcrWord(i + 1, 1, 1, 50, 90.0, "text"),
            ])
        pars = assemble_paragraphs(words, max_par=12)
        assert len(pars) == 12
        assert len(set(pars)) == 12


class TestSplitRuns:
    def test_mixed_split(self):
        runs = split_runs("加能区 are placed 加能区")
        assert runs == [(True, "加能区"), (False, " are placed "), (True, "加能区")]

    def test_pure_english_one_run(self):
        assert split_runs("The dog bites.") == [(False, "The dog bites.")]

    def test_pure_chinese_one_run(self):
        assert split_runs("地点已汉化") == [(True, "地点已汉化")]

    def test_collapse_cjk_spaces(self):
        assert collapse_cjk_spaces("加 能 区 是") == "加能区是"
        assert collapse_cjk_spaces("The dog") == "The dog"  # 英文空格保留


class TestTranslateParallel:
    """translate_paragraphs: 实例与工厂函数两种入参, 保序。"""

    class _Result:
        def __init__(self, text):
            self.text = text
            self.model = "m"
            self.provider = "p"
            self.confidence = 0.5
            self.error = None

    class _FakeOrch:
        def __init__(self):
            self.calls = []

        def translate(self, text, context=None):
            self.calls.append(text)
            return TestTranslateParallel._Result(f"[{text}]")

    def test_mixed_chinese_kept_english_replaced(self):
        """中英混排: 中文段原样保留, 仅英文段送翻译, 拼成完整内容。"""
        orch = self._FakeOrch()  # translate(text) -> f"[{text}]"
        out = translate_paragraphs(["加能区 are placed 加能区"], orch)
        assert out[0]["zh"] == "加能区[are placed]加能区"
        assert out[0]["en"] == "加能区 are placed 加能区"
        assert "are placed" in orch.calls

    def test_all_chinese_passthrough_no_llm(self):
        orch = self._FakeOrch()
        out = translate_paragraphs(["地 点 已 汉 化"], orch)
        assert out[0]["zh"] == "地点已汉化"
        assert out[0]["provider"] == "passthrough"
        assert orch.calls == []  # 未耗 LLM

    def test_short_english_fragments_kept(self):
        """过短英文碎片(如 OCR 残片 x, ab)不值得送翻译, 原样保留。"""
        orch = self._FakeOrch()
        out = translate_paragraphs(["中文x 中文y keep"], orch)
        # y 和 keep 之间无中文 → 同一连续英文段一起送翻; x 单独过短保留
        assert orch.calls == ["y keep"]
        assert out[0]["zh"].startswith("中文")

    def test_shared_instance(self):
        orch = self._FakeOrch()
        out = translate_paragraphs(["aaa text", "bbb text"], orch)
        assert [r["zh"] for r in out] == ["[aaa text]", "[bbb text]"]
        assert [r["en"] for r in out] == ["aaa text", "bbb text"]

    def test_factory_per_thread_and_order(self):
        made = []

        def factory():
            o = TestTranslateParallel._FakeOrch()
            made.append(o)
            return o

        pars = [f"paragraph number {i} text" for i in range(8)]
        out = translate_paragraphs(pars, factory, workers=4)
        assert [r["en"] for r in out] == pars
        assert all(r["zh"] == f"[{p}]" for r, p in zip(out, pars))
        # 至少建了 1 个(线程池可能复用同一线程, 只验不炸+正确)
        assert made

    def test_empty(self):
        assert translate_paragraphs([], self._FakeOrch()) == []


class TestCliParser:
    def test_shot_subcommand(self):
        args = build_parser().parse_args(["shot", "/tmp/x.png", "--json"])
        assert args.command == "shot"
        assert args.image == "/tmp/x.png"
        assert args.json is True and args.zh_only is False

    def test_shot_zh_only(self):
        args = build_parser().parse_args(["shot", "/tmp/x.png", "--zh-only"])
        assert args.zh_only is True and args.json is False

    def test_shot_save_md_flag(self):
        args = build_parser().parse_args(["shot", "/tmp/x.png", "--save-md"])
        assert args.save_md is True


class TestRenderMarkdown:
    def test_archive_layout(self):
        results = [
            {
                "en": "The dog bites the goblin.",
                "zh": "狗咬了哥布林。",
                "model": "glm-5.3",
                "provider": "zhipu",
                "confidence": 0.91,
                "error": None,
            }
        ]
        md = render_markdown(results, "/home/wu/Pictures/fanyi-x.png", timestamp="2026-09-12 14:58")
        assert "# FanYi 截图翻译 · 2026-09-12 14:58" in md
        assert "![原图](fanyi-x.png)" in md  # 同目录相对引用
        assert "The dog bites the goblin." in md
        assert "狗咬了哥布林。" in md
        assert "模型 glm-5.3 / zhipu" in md

    def test_archive_marks_error(self):
        results = [
            {
                "en": "hello world text",
                "zh": "hello world text",
                "model": "",
                "provider": "",
                "confidence": 0.0,
                "error": "provider 不可用",
            }
        ]
        md = render_markdown(results, "/tmp/y.png", timestamp="t")
        assert "错误: provider 不可用" in md
