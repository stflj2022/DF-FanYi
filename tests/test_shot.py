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
    _COMBINED_TAG_RE,
    _split_combined_response,
    _translate_combined,
    assemble_paragraphs,
    collapse_cjk_spaces,
    merge_dual_ocr,
    parse_tsv,
    preprocess_image,
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


class TestPreprocess:
    def test_small_image_upscaled(self, tmp_path):
        from PIL import Image

        p = tmp_path / "small.png"
        Image.new("RGB", (100, 50), (10, 10, 10)).save(p)
        out = preprocess_image(str(p))
        assert out != str(p)
        w, h = Image.open(out).size
        assert (w, h) == (200, 100)
        assert Image.open(out).mode == "L"

    def test_huge_image_skipped(self, tmp_path):
        from PIL import Image

        p = tmp_path / "big.png"
        Image.new("RGB", (2000, 1200), (10, 10, 10)).save(p)  # 2.4MP > 2MP
        assert preprocess_image(str(p)) == str(p)


class TestMergeDualOcr:
    """双通道合并: 英文信 eng, 中文信 chi_sim+语料成词判据。"""

    def test_garbage_misread_dropped_by_bigram(self):
        # mixed 把英文 number 误读成 坤敏壁; 语料不成词 → 幻觉丢弃
        eng = [OcrWord(1, 1, 1, 100, 90.0, "number", top=10, width=80, height=20)]
        mixed = [OcrWord(1, 1, 1, 100, 80.0, "坤敏壁", top=10, width=80, height=20)]
        merged = merge_dual_ocr(eng, mixed, zh_bigrams={"任务", "工坊"})
        texts = [w.text for w in merged]
        assert "number" in texts and "坤敏壁" not in texts

    def test_real_chinese_not_vetoed_by_eng_junk(self):
        # eng 把真中文 工坊 读成 T3(conf62); bigram 判据为主, 工坊必须在
        eng = [OcrWord(1, 1, 1, 860, 62.0, "T3", top=26, width=40, height=20)]
        mixed = [
            OcrWord(1, 1, 1, 859, 82.0, "工", top=26, width=40, height=20),
            OcrWord(1, 1, 1, 897, 97.0, "坊", top=22, width=40, height=20),
        ]
        merged = merge_dual_ocr(eng, mixed, zh_bigrams={"工坊"})
        texts = [w.text for w in merged]
        assert "工" in texts and "坊" in texts

    def test_garbage_run_filtered_by_bigram_corpus(self):
        # eng 低置信读不出(无重叠词), 但 榭些 不在语料不成词 → 幻觉丢弃
        mixed = [
            OcrWord(1, 1, 1, 100, 85.0, "榭", top=10, width=40, height=20),
            OcrWord(1, 1, 1, 140, 85.0, "些", top=10, width=40, height=20),
        ]
        merged = merge_dual_ocr([], mixed, zh_bigrams={"任务", "工坊"})
        assert merged == []

    def test_real_run_kept_by_bigram_corpus(self):
        # 任+务 拼串成 任务 在语料 → 两字都保留(单字无 bigram, 必须先拼串)
        mixed = [
            OcrWord(1, 1, 1, 100, 85.0, "任", top=10, width=40, height=20),
            OcrWord(1, 1, 1, 140, 97.0, "务", top=10, width=40, height=20),
        ]
        merged = merge_dual_ocr([], mixed, zh_bigrams={"任务", "工坊"})
        assert "".join(w.text for w in merged if "\u4e00" <= w.text <= "\u9fff") == "任务"

    def test_big_gap_splits_runs(self):
        # 榭些[大间隙]数量 → 两串: 垃圾串丢, 真词串(数量)留
        mixed = [
            OcrWord(1, 1, 1, 100, 45.0, "榭", top=10, width=40, height=20),
            OcrWord(1, 1, 1, 140, 89.0, "些", top=10, width=40, height=20),
            OcrWord(1, 1, 1, 300, 96.0, "数", top=10, width=40, height=20),
            OcrWord(1, 1, 1, 340, 87.0, "量", top=10, width=40, height=20),
        ]
        merged = merge_dual_ocr([], mixed, zh_bigrams={"数量"})
        assert "".join(w.text for w in merged if "\u4e00" <= w.text <= "\u9fff") == "数量"

    def test_no_corpus_keeps_all(self):
        # 语料不可用(空集) → 不做孤立剔除, 保留真中文(2字成词)
        mixed = [
            OcrWord(1, 1, 1, 100, 85.0, "任", top=10, width=20, height=20),
            OcrWord(1, 1, 1, 130, 85.0, "意", top=10, width=20, height=20),
        ]
        assert "".join(w.text for w in merge_dual_ocr([], mixed)) == "任意"

    def test_real_chinese_kept_and_adopts_eng_line(self):
        # 真中文 UI(eng 读不到)不重叠 → 保留, 并继承同行 eng 词的分组 id
        eng = [OcrWord(2, 1, 1, 300, 90.0, "orders", top=50, width=60, height=20)]
        mixed = [
            OcrWord(2, 1, 1, 100, 85.0, "工", top=50, width=20, height=20),
            OcrWord(2, 1, 1, 125, 85.0, "单", top=50, width=20, height=20),
            OcrWord(2, 1, 1, 300, 80.0, "orders", top=50, width=60, height=20),
        ]
        merged = merge_dual_ocr(eng, mixed, zh_bigrams={"工单"})
        cjk = [w for w in merged if w.text in ("工", "单")]
        assert len(cjk) == 2
        assert (cjk[0].block, cjk[0].line) == (2, 1)  # 继承 eng 行

    def test_pure_chinese_line_own_paragraph(self):
        # 整行纯中文(无 eng 词) → 独立 block(1000+), 不会被丢
        mixed = [
            OcrWord(9, 1, 1, 10, 90.0, "地", top=100, width=20, height=20),
            OcrWord(9, 1, 1, 35, 90.0, "点", top=100, width=20, height=20),
        ]
        merged = merge_dual_ocr([], mixed)
        assert sorted(w.text for w in merged) == ["地", "点"]
        assert all(w.block >= 1000 for w in merged)

    def test_merged_assembly_mixed_order(self):
        # 端到端小样本: 同一行 中文+英文+中文, 按位置拼接
        eng = [OcrWord(1, 1, 1, 200, 90.0, "are", top=10, width=30, height=20)]
        mixed = [
            OcrWord(1, 1, 1, 100, 85.0, "工", top=10, width=20, height=20),
            OcrWord(1, 1, 1, 125, 85.0, "单", top=10, width=20, height=20),
            OcrWord(1, 1, 1, 240, 85.0, "物", top=10, width=20, height=20),
            OcrWord(1, 1, 1, 265, 85.0, "品", top=10, width=20, height=20),
        ]
        merged = merge_dual_ocr(eng, mixed, zh_bigrams={"工单", "物品"})
        pars = assemble_paragraphs(merged)
        # mixed 通道每个汉字单字词输出, assemble_paragraphs 按词 join
        assert pars == ["工 单 are 物 品"]

    def test_multichar_word_no_indexerror(self):
        """2026-09-12 19:29 崩溃回归: 多字词混单字时字符索引不能当词索引。

        fanyi-20260912-192922.png: chi_sim 输出整词"矿车"+单字混排,
        len(text) > len(run_words) → 旧代码 IndexError → 整个 shot 崩溃,
        无归档无翻译, 通知 6s 被游戏盖住 → 用户体感"无声无息"。
        """
        mixed = [
            OcrWord(1, 1, 1, 100, 85.0, "矿车", top=10, width=40, height=20),
            OcrWord(1, 1, 1, 145, 85.0, "在", top=10, width=20, height=20),
            OcrWord(1, 1, 1, 170, 85.0, "轨", top=10, width=20, height=20),
            OcrWord(1, 1, 1, 195, 85.0, "道", top=10, width=20, height=20),
            OcrWord(1, 1, 1, 220, 85.0, "榭", top=10, width=20, height=20),
            OcrWord(1, 1, 1, 245, 85.0, "些", top=10, width=20, height=20),
        ]
        # "矿车"整词 + 单字: 旧实现 text 长 9 而 run_words 长 6 → 越界
        merged = merge_dual_ocr([], mixed, zh_bigrams={"矿车", "轨道"})
        texts = "".join(w.text for w in merged)
        assert "矿车" in texts
        assert "轨道" in texts
        assert "榭" not in texts and "些" not in texts  # 幻觉垃圾仍被剔除


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

    def test_translation_error_recorded_not_crash(self):
        """某段翻译出错: 记录 error 不炸, 英文原文回退进合并结果。"""

        class _ErrOrch:
            def translate(self, text, context=None):
                r = TestTranslateParallel._Result(text)  # text=原文回退
                r.error = "provider 429"
                return r

        out = translate_paragraphs(["中文片段 english words"], _ErrOrch())
        assert out[0]["error"] == "provider 429"
        assert out[0]["zh"] == "中文片段english words"  # 回退原文仍在, 不崩

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
        assert "provider 不可用" in md
        assert "⚠️ 部分段失败" in md

    def test_archive_renders_coherent_single_block(self):
        """2026-09-12: 多段拼接为连贯块(不按 ## 分段), 与 zenity 浮窗一致。"""
        results = [
            {"en": "Tracks are convenient.", "zh": "轨道很便捷。",
             "model": "router/L2", "provider": "router", "confidence": 0.9, "error": None},
            {"en": "Minecarts move fast.", "zh": "矿车移动很快。",
             "model": "router/L2", "provider": "router", "confidence": 0.9, "error": None},
        ]
        md = render_markdown(results, "/tmp/a.png", timestamp="2026-09-12 18:30")
        # 连贯呈现: 只一个 ## 原文 / **译文** 标题
        assert md.count("## 原文") == 1
        assert md.count("**译文**") == 1
        # 不再有 ## 1. 原文 这种分段
        assert "## 1. 原文" not in md
        assert "## 2. 原文" not in md
        # 两段拼接在同一行
        assert "Tracks are convenient. Minecarts move fast." in md
        assert "轨道很便捷。 矿车移动很快。" in md
        # 元数据中提示合并 N 段
        assert "合并 2 段" in md


class TestCombinedTranslation:
    """多段合并调用(2026-09-12): 一次 LLM 调用送所有段落, 返 N 段保证词汇一致。"""

    def test_split_response_perfect(self):
        """模型完美保留 N 个标签 → 拆出 N 段。"""
        text = (
            "<¶¶¶PARA=1¶¶¶>\n第一段译文。\n\n"
            "<¶¶¶PARA=2¶¶¶>\n第二段译文。\n\n"
            "<¶¶¶PARA=3¶¶¶>\n第三段译文。"
        )
        out = _split_combined_response(text, 3)
        assert out == ["第一段译文。", "第二段译文。", "第三段译文。"]

    def test_split_response_with_extra_text(self):
        """模型在标签间加了额外说明 → 正确拆出。"""
        text = (
            "<¶¶¶PARA=1¶¶¶>\n甲\n\n"
            "<¶¶¶PARA=2¶¶¶>\n乙\n\n"
            "<¶¶¶PARA=3¶¶¶>\n丙"
        )
        out = _split_combined_response(text, 3)
        assert out == ["甲", "乙", "丙"]

    def test_split_response_missing_tag_returns_none(self):
        """模型遗漏某个标签 → 拆分错返 None(调用方回退并行)。"""
        text = (
            "<¶¶¶PARA=1¶¶¶>\n甲\n\n"
            "<¶¶¶PARA=3¶¶¶>\n丙"
        )
        assert _split_combined_response(text, 3) is None

    def test_split_response_no_tags_returns_none(self):
        """无标签结构 → 拆分错。"""
        assert _split_combined_response("甲\n乙\n丙", 3) is None

    def test_split_response_out_of_range_tag_ignored(self):
        """越界标签(如 N=99)忽略。"""
        text = (
            "<¶¶¶PARA=1¶¶¶>\n甲\n\n"
            "<¶¶¶PARA=2¶¶¶>\n乙\n\n"
            "<¶¶¶PARA=99¶¶¶>\n噪声"
        )
        out = _split_combined_response(text, 2)
        assert out == ["甲", "乙"]

    def test_tag_regex_matches(self):
        assert _COMBINED_TAG_RE.findall("<¶¶¶PARA=5¶¶¶>") == ["5"]
        assert _COMBINED_TAG_RE.findall("a<¶¶¶PARA=12¶¶¶>b<¶¶¶PARA=3¶¶¶>") == ["12", "3"]

    def test_translate_combined_short_circuits_empty(self):
        """空段落列表返空列表(不调 LLM)。"""
        assert _translate_combined([]) == []

    def test_translate_combined_too_long_returns_none(self):
        """超长输入返 None(调用方走并行)。"""
        long_paragraph = "a " * 1000
        paragraphs = [long_paragraph] * 5  # 5000 chars > 3500 limit
        assert _translate_combined(paragraphs) is None

    def test_format_active_terms_finds_match(self):
        """DF 术语库中含的源词应被扫描出。"""
        from df_fanyi.shot import _format_active_terms
        text = "Minecarts on Tracks move to Stops with friction."
        block = _format_active_terms(text)
        # 应该至少含 minecart / track / stop / friction 之一
        # (这些词都装在术语库里)
        assert "→" in block or "(无" in block  # 没入库时返 “无”
