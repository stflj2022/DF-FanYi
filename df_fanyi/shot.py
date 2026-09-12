"""截图翻译工作流(2026-09-12 用户方案, Ctrl+Print)。

流程: slurp 选区 → grim 截图 → tesseract OCR(TSV) → 段落重组 → 翻译管线 → zenity 大浮窗。

弃游戏内 F11 拖框的原因: Steam 版 DF 53.x 的 enabler.mouse_lbit 不随物理按键更新,
帧驱动拖框无法锚定(实测 2026-09-12 14:44, de29f9f); 系统级截图由 Hyprland 完成,
中文渲染由桌面完成, 绕开 DFHack 鼠标事件与 CP437 图集两大坑。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("df_fanyi.shot")

__all__ = [
    "OcrWord",
    "parse_tsv",
    "assemble_paragraphs",
    "ocr_image",
    "translate_paragraphs",
    "process_image",
    "render_markdown",
    "split_runs",
    "merge_dual_ocr",
    "load_zh_corpus",
]


@dataclass(frozen=True)
class OcrWord:
    """tesseract TSV level=5 词条(一个识别出的单词)。"""

    block: int
    par: int
    line: int
    left: int
    conf: float
    text: str
    top: int = 0
    width: int = 0
    height: int = 0


_TSV_COLS = 12  # level page block par line word left top width height conf text
_LETTER = re.compile(r"[A-Za-z]")
_VOWEL = re.compile(r"[aeiouyAEIOUY]")
_ALNUM = re.compile(r"[A-Za-z0-9]")
# 中日韩统一表意文字 + CJK 标点 + 全角符号(OCR 常见形态)
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]")
# 中文相邻字符间的 OCR 伪空格(tesseract 每字一词) 应折叠
_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f])\s+(?=[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef])")

_LOCAL_TESSDATA = Path.home() / ".local" / "share" / "tesseract" / "tessdata"
_MAX_UPSCALE_PIXELS = 2_000_000  # 超过(整屏级)不放大, 防 OCR 变慢


def preprocess_image(image_path: str) -> str:
    """2x 放大+灰度: DF 位图字体在原生尺寸 OCR 噪声大(w→v/u, 汉字误读),
    放大后 'ave used'→'are used', '坤敏壁'→'数量' 级改善。
    PIL 缺失/大图/异常时原样返回。"""
    try:
        from PIL import Image
    except ImportError:
        return image_path
    try:
        img = Image.open(image_path)
        if img.width * img.height > _MAX_UPSCALE_PIXELS:
            return image_path
        up = img.convert("L").resize((img.width * 2, img.height * 2), Image.LANCZOS)
        out = Path(tempfile.gettempdir()) / f"fanyi-ocr-up-{os.getpid()}-{Path(image_path).stem}.png"
        up.save(out)
        return str(out)
    except Exception:
        return image_path


def collapse_cjk_spaces(text: str) -> str:
    """折叠中文字符间 OCR 伪空格: '加 能 区' → '加能区'。"""
    return _CJK_GAP.sub("", text)


def split_runs(text: str) -> list[tuple[bool, str]]:
    """把混排文本切成 [(是否含CJK, 段), ...] 连续片段。

    中文段(已汉化 UI)原样保留, 英文段才送翻译。
    """
    out: list[tuple[bool, str]] = []
    cur, cur_cjk = "", None
    for ch in text:
        is_cjk = bool(_CJK_CHAR.match(ch))
        if cur_cjk is None:
            cur_cjk = is_cjk
        if is_cjk == cur_cjk:
            cur += ch
        else:
            out.append((cur_cjk, cur))
            cur, cur_cjk = ch, is_cjk
    if cur:
        out.append((bool(cur_cjk), cur))
    return out


def parse_tsv(raw: str, *, min_conf: float = 30.0) -> list[OcrWord]:
    """解析 tesseract TSV 输出, 只留 level=5(单词)且置信度达标的词条。"""
    words: list[OcrWord] = []
    for ln in raw.splitlines():
        parts = ln.split("\t")
        if len(parts) != _TSV_COLS:
            continue
        try:
            if int(parts[0]) != 5:
                continue
            block, par, line = int(parts[2]), int(parts[3]), int(parts[4])
            left, top = int(parts[6]), int(parts[7])
            width, height = int(parts[8]), int(parts[9])
            conf = float(parts[10])
        except ValueError:
            continue  # 表头/坏行
        text = parts[11].strip()
        # not (conf >= min) 同时拦住 nan(nan 与任何数比较均为 False)
        if not text or not (conf >= min_conf):
            continue
        words.append(OcrWord(block, par, line, left, conf, text, top, width, height))
    return words


def _paragraph_text(words: list[OcrWord]) -> str:
    """同(block,par)内: 行内按 left 排序, 行间以空格连接(DF 换行=折行)。"""
    lines: dict[int, list[OcrWord]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    out = []
    for ln in sorted(lines):
        ws = sorted(lines[ln], key=lambda w: w.left)
        out.append(" ".join(w.text for w in ws))
    return " ".join(out)


def _is_game_text(text: str) -> bool:
    """过滤地图区图形 OCR 噪声。

    含中文(≥2 字)直接放行 —— 已汉化 UI 混排段落;
    纯英文段仍需: 够多字母 + 含元音 + 字母占比过半。
    """
    cjk = _CJK_CHAR.findall(text)
    if len(cjk) >= 2:
        return True
    letters = len(_LETTER.findall(text))
    if letters < 4:
        return False
    if not _VOWEL.search(text):
        return False
    alnum = len(_ALNUM.findall(text))
    if letters / max(1, alnum) < 0.5:
        return False
    return True


def assemble_paragraphs(words: list[OcrWord], *, max_par: int = 12) -> list[str]:
    """按(block,par)重组为有序段落, 过滤噪声/去重/限量。"""
    groups: dict[tuple[int, int], list[OcrWord]] = {}
    order: list[tuple[int, int]] = []
    for w in words:
        key = (w.block, w.par)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(w)
    pars: list[str] = []
    seen: set[str] = set()
    for key in order:
        text = _paragraph_text(groups[key])
        if not _is_game_text(text):
            continue
        text = text[:500]
        if text in seen:
            continue
        seen.add(text)
        pars.append(text)
        if len(pars) >= max_par:
            break
    return pars


def _run_tesseract(src: str, langs: str, *, timeout: int) -> list[OcrWord]:
    cmd = ["tesseract"]
    if langs != "eng":
        cmd += ["--tessdata-dir", str(_LOCAL_TESSDATA)]
    cmd += [src, "stdout", "-l", langs, "tsv"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract 失败: {proc.stderr.strip()[:200]}")
    return parse_tsv(proc.stdout)


def _has_cjk(text: str) -> bool:
    return bool(_CJK_CHAR.search(text))


def load_zh_corpus(store_path: str | Path) -> set[str]:
    """从翻译记忆/术语表提取 zh bigram 集合。

    游戏屏上的真中文全部来自引擎自己的翻译(overlay 替换词),
    因此“某 bigram 在语料中”就是“这是真中文”的可靠判据;
    chi_sim 对位图英文的误读(坤敏壁/佐务)不会碰巧成词。
    库不存在/异常 → 空集(过滤关闭, 全保留)。"""
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
        texts = [r[0] or "" for r in conn.execute("SELECT translated_text FROM translation_memory")]
        texts += [r[0] or "" for r in conn.execute("SELECT target FROM terminology")]
        conn.close()
    except Exception:
        return set()
    bigrams: set[str] = set()
    for t in texts:
        for i in range(len(t) - 1):
            a, b = t[i], t[i + 1]
            if "\u4e00" <= a <= "\u9fff" and "\u4e00" <= b <= "\u9fff":
                bigrams.add(a + b)
    return bigrams


def _any_bigram_in(run: str, bigrams: set[str]) -> bool:
    """run 中任一相邻汉字对在语料中成词 → 真中文。
    单字不成词(无 bigram) → False(单独一个'目'这类噪声丢掉)。"""
    if not bigrams:
        return True  # 语料不可用 → 过滤关闭
    for i in range(len(run) - 1):
        if run[i : i + 2] in bigrams:
            return True
    return False


def merge_dual_ocr(
    eng_words: list[OcrWord],
    mixed_words: list[OcrWord],
    zh_bigrams: set[str] | None = None,
) -> list[OcrWord]:
    """双通道合并: 英文只信 eng, 中文只信 chi_sim。

    用户规则: 识别到中文就跳过不翻译, 但原中文要与译文拼在一起。
    关键洞见: chi_sim 会把游戏位图英文误读成垃圾中文(坤敏壁/刺作)。
    判定法(以语料成词为唯一判据):
    相邻 CJK 词拼串, 整串在 zh_bigrams(引擎翻译语料)中不成词
    → chi_sim 幻觉垃圾(坤敏壁/榭些), 丢弃; 成词 → 真中文 UI → 保留。
    注: 不用 eng 重叠否决 —— eng 会把真中文读成中等置信垃圾(T3@62)反而误杀。

    纯中文行(无 eng 词)给独立 block 号(1000+), 让段落重组仍可分组;
    混排行里的 CJK 词继承同行 eng 词的 (block,par,line)。
    """
    bigrams = zh_bigrams if zh_bigrams is not None else set()
    cands = [w for w in mixed_words if _has_cjk(w.text)]
    runs = _group_cjk_runs(cands)
    pure_cjk: list[OcrWord] = []
    for run_words in runs:
        # 单字 CJK run 直接丢弃(DF UI 中文几乎都 ≥2 字)
        if len(run_words) < 2:
            continue
        kept_words = _prune_cjk_run(run_words, bigrams)
        if kept_words:
            pure_cjk.extend(kept_words)

    # 3) 行归属: 与 eng 词同行(top 接近)则继承其 (block,par,line)
    def _adopt(w: OcrWord, ids: tuple[int, int, int]) -> OcrWord:
        return OcrWord(ids[0], ids[1], ids[2], w.left, w.conf, w.text, w.top, w.width, w.height)

    kept: list[OcrWord] = []
    orphans: list[OcrWord] = []
    for w in pure_cjk:
        line_ids = None
        for e in eng_words:
            tol = max(e.height, w.height, 8) * 0.6
            if abs(e.top - w.top) <= tol:
                line_ids = (e.block, e.par, e.line)
                break
        if line_ids:
            kept.append(_adopt(w, line_ids))
        else:
            orphans.append(w)

    # 纯中文行自成段: 按 top 聚类, 每簇一个 block(1000+n), 行内按 line=1
    orphans.sort(key=lambda w: w.top)
    cluster_top = None
    cluster_idx = 0
    for w in orphans:
        if cluster_top is None or w.top - cluster_top > 2.5 * max(w.height, 1):
            cluster_idx += 1
            cluster_top = w.top
        kept.append(_adopt(w, (1000 + cluster_idx, 1, 1)))
    # 4) eng 噪声过滤: 保留的 CJK 区域, eng 读出中等置信垃圾(T3@工坊)
    # 会混入输出。要从 eng_words 里丢掉与任何 kept CJK 重叠的词。
    def _overlap(a: OcrWord, b: OcrWord) -> bool:
        if not (a.width and b.width):
            return False
        ox = min(a.left + a.width, b.left + b.width) - max(a.left, b.left)
        oy = min(a.top + a.height, b.top + b.height) - max(a.top, b.top)
        return ox > 0 and oy > 0

    filtered_eng = [e for e in eng_words if not any(_overlap(e, k) for k in kept)]
    return kept + filtered_eng


def _prune_cjk_run(run_words: list[OcrWord], bigrams: set[str]) -> list[OcrWord]:
    """剔除 run 中不参与任何语料 bigram 的孤立字符。

    '数量目' → 数量✓ + 目(无 bigram 邻居) → 只留 数量
    '制作木制偷物箱' → 制作/木制/物箱✓ + 偷(无 bigram 邻居) → 只留真词
    '榭些' → 整串无 bigram → 全部丢掉
    """
    if not bigrams:
        return run_words  # 语料不可用 → 不做孤立剔除
    if len(run_words) < 2:
        return []
    text = "".join(w.text for w in run_words)
    keep_idx: set[int] = set()
    for i in range(len(text) - 1):
        if text[i : i + 2] in bigrams:
            keep_idx.add(i)
            keep_idx.add(i + 1)
    return [run_words[i] for i in sorted(keep_idx)]


def _group_cjk_runs(words: list[OcrWord]) -> list[list[OcrWord]]:
    """同一行(顶部接近)且横向间隙 ≤ 1.2×字宽的 CJK 词拼成一串。
    mixed 通道常把每个汉字输出为单字词, 成词测试必须先拼串:
    '任','务' → '任务'✓  '榭','些' → '榭些'✗。
    拼接顺序必须 left→right(阅读顺序)。
    1) 按 top 聚行(绝对 top 差 ≤ 行高半值); 2) 行内按 left 排序; 3) 按横向间隙分组。
    """
    ws = sorted(words, key=lambda w: w.top)
    lines: list[list[OcrWord]] = []
    cur: list[OcrWord] = []
    line_top = 10**9
    for w in ws:
        line_h = max(w.height, 8)
        if cur and w.top - cur[0].top > max(line_h, 12):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    runs: list[list[OcrWord]] = []
    for line in lines:
        line.sort(key=lambda w: w.left)
        cur_run: list[OcrWord] = []
        right = -1
        for w in line:
            w_char_w = max(w.width, 10)
            near = cur_run and (w.left - right) <= 1.2 * w_char_w
            if not near:
                if cur_run:
                    runs.append(cur_run)
                cur_run = []
            cur_run.append(w)
            right = w.left + w.width
        if cur_run:
            runs.append(cur_run)
    return runs


def ocr_image(
    image_path: str, *, timeout: int = 60, zh_bigrams: set[str] | None = None
) -> list[OcrWord]:
    """对图片跑 OCR, 返回词条(双通道合并后)。

    - eng 通道: 英文准确(不会产生中文垃圾)
    - chi_sim+eng 通道: 只取其中的真中文(供保留拼接), 判定:
      与 eng 高置信词重叠=英文误读丢弃; zh_bigrams 不成词=幻觉丢弃
    本地无 chi_sim 时退回纯 eng 单通道。
    """
    src = preprocess_image(image_path)
    eng_words = _run_tesseract(src, "eng", timeout=timeout)
    # 滤掉纯符号词(¥/•): 不是文本, 只会污染翻译与质量门
    eng_words = [w for w in eng_words if re.search(r"[A-Za-z0-9]", w.text)]
    if not (_LOCAL_TESSDATA / "chi_sim.traineddata").exists():
        return eng_words
    mixed_words = _run_tesseract(src, "chi_sim+eng", timeout=timeout)
    return merge_dual_ocr(eng_words, mixed_words, zh_bigrams)


def _needs_translation(seg: str) -> bool:
    """片段是否含值得送翻译的英文(≥3 字母且含元音)。"""
    s = seg.strip()
    letters = len(_LETTER.findall(s))
    return letters >= 3 and bool(_VOWEL.search(s))


def translate_paragraphs(
    paragraphs: list[str], orch: Any, *, workers: int = 4
) -> list[dict[str, Any]]:
    """逐段翻译(中英混排感知), 线程池并行。

    游戏屏常为已汉化 UI + 残留英文混排: 按中文/英文切片,
    **中文段原样保留(折叠伪空格), 仅英文段送管线**, 结果拼成完整中文内容。
    全中文段直接透传, 不耗 LLM。

    交互式截图可能有 10+ 段新句子, 串行 LLM 往返要数分钟;
    并行(默认 4 线程)把墙钟时间压到 1/4。
    orch 可传实例(单/测试)或工厂函数(每线程独立实例 ——
    sqlite 连接默认不可跨线程, 必须每线程一份 orchestrator)。
    结果顺序与 paragraphs 一致(ThreadPoolExecutor.map 保序)。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    factory = orch if callable(orch) else (lambda: orch)
    local = threading.local()

    def _one(text: str) -> dict[str, Any]:
        if getattr(local, "orch", None) is None:
            local.orch = factory()
        runs = split_runs(text)
        # 无需翻译的英文碎片也归入保留段; 全保留 → 透传不耗 LLM
        if all(is_cjk or not _needs_translation(seg) for is_cjk, seg in runs):
            return {
                "en": collapse_cjk_spaces(text),
                "zh": collapse_cjk_spaces(text),
                "model": "",
                "provider": "passthrough",
                "confidence": 1.0,
                "error": None,
            }
        o = local.orch
        parts: list[str] = []
        last: Any = None
        errors: list[str] = []
        for is_cjk, seg in runs:
            if is_cjk or not _needs_translation(seg):
                parts.append(collapse_cjk_spaces(seg).strip())
                continue
            r = o.translate(seg.strip(), context={"source": "screenshot"})
            last = r
            err = getattr(r, "error", None)
            if err:
                errors.append(str(err))
            parts.append(r.text.strip())
        return {
            "en": collapse_cjk_spaces(text),
            "zh": "".join(parts),
            "model": getattr(last, "model", "") if last else "",
            "provider": getattr(last, "provider", "") if last else "",
            "confidence": getattr(last, "confidence", 0.0) if last else 0.0,
            "error": "; ".join(errors) if errors else None,
        }

    if not paragraphs:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(paragraphs))) as ex:
        return list(ex.map(_one, paragraphs))


def process_image(image_path: str, orch: Any, *, zh_bigrams: set[str] | None = None) -> list[dict[str, Any]]:
    """OCR → 段落 → 翻译, 全流程。"""
    return translate_paragraphs(
        assemble_paragraphs(ocr_image(image_path, zh_bigrams=zh_bigrams)), orch
    )


# 2026-09-12: 合并多段为单次 LLM 调用的硬性约束。
# 用户报"一句一句翻译"不连贯——根因是每段独立上下文, 词汇漂移(Tracks/铁路/轨道混用)。
# 合并调用让 LLM 在同一上下文决定术语, 1 次往返而非 N 次。
# 用**编号标签** <¶¶¶PARA=N¶¶¶> 包裹每段, 模型能逐个对照翻译,
# 拆分时按标签正则抽取, 严格保证 N 段对齐。
_COMBINED_TAG_RE = re.compile(r"<¶¶¶PARA=(\d+)¶¶¶>")
_COMBINED_MAX_CHARS = 3500  # 超过则回退并行(避免单次 LLM 超 max_tokens)
_COMBINED_OVERHEAD_PER_PARA = 60  # 每段分隔符 + 行尾开销估计
_COMBINED_SYSTEM_PROMPT_ADDON = """\n\n【多段合并调用特殊指令】
输入格式: {n} 段**互相独立**的提示文本, 每段前有 `<¶¶¶PARA=N¶¶¶>` 标签 (N=1 到 {n})。
请**逐段**翻译, 要求:
  1. 严格输出 {n} 段(不多不少), 每段用同样的 `<¶¶¶PARA=N¶¶¶>` 标签包裹
  2. 段内中英混排时翻译英文部分, 与原中文拼接, 保留专有名词和变量占位符
  3. 只输出译文本体 + 标签, 不要任何说明/前缀/尾注
  4. 【DF 领域术语·硬性】下面是“活跃术语表”, **遇到表中源词时必须使用对应译文, 不允许随意外译**:
{terms_block}
  5. 【DF 游戏语境·绝对禁止】本输入是 Dwarf Fortress 车辆系统说明文:
     - "put on" = **装上**(装载到轨道/矿车上), 严禁译为“穿上”
     - "remove from" = **从…卸下**(从轨道/矿车/库存区卸下), 严禁译为“脱下”
     - "Tracks" = **轨道**(铁轨轨制), 严禁译为“音轨”“跟踪”
     - "minecart" = **矿车**, "Stops" = **停靠点**, "friction" = **摩擦力**, "stockpile" = **库存区**
     - 若句子不完整(如末尾 "the" 或 "to"), 直接按表达译出即可, 不要再发挥。
  6. 【OCR 残留清理】输入可能混入游戏标记剥壳后的无意义碎片(如 =1t、F&、ITA):
     译文中**不得保留**这类碎片——按语境还原为所指对象或直接省略,
     保证输出是通顺自然的中文(2026-09-12 用户反馈译文里残留 =1t/F&/ITA)。

示例 (2 段):
输入:
<¶¶¶PARA=1¶¶¶>
Tracks are convenient.
<¶¶¶PARA=2¶¶¶>
Stops have friction.

输出:
<¶¶¶PARA=1¶¶¶>
轨道很便捷。
<¶¶¶PARA=2¶¶¶>
停靠点有摩擦力。
"""


def _format_active_terms(text: str, *, limit: int = 30) -> str:
    """从术语库扫出在 text 中出现的活跃术语, 格式化为术语表块。

    2026-09-12 用户报 put on/remove from 被译为穿/脱(望文生意)——
    术语注入可明确指明 “put on = 装上”“stockpile = 库存区”等。
    """
    try:
        from df_fanyi.config import load_config
        from df_fanyi.database.store import store_from_config
        cfg = load_config()
        store = store_from_config(cfg)
    except Exception:
        return "  (无——术语库不可用)"

    rows = store.term_search_in_text(text, limit=limit)
    if not rows:
        return "  (无——本次输入中未检测到术语表中的词)"

    lines = []
    for r in rows:
        src = (r.get("source") or "").strip()
        dst = (r.get("target") or "").strip()
        if src and dst:
            lines.append(f"  - {src} → {dst}")
    return "\n".join(lines) if lines else "  (无)"


def _split_combined_response(text: str, n: int) -> list[str] | None:
    """按 `<¶¶¶PARA=N¶¶¶>` 标签切分, 期望得到 n 个非空部分(标签按 N 升序)。

    模型遗漏某个标签 → 补空串占位(提示拆分错)。
    返回 None = 拆分失败 → 调用方回退并行。
    """
    matches = list(_COMBINED_TAG_RE.finditer(text))
    if not matches:
        return None
    # 按标签抽段: 标签 N 到 下一标签之间是该段译文
    parts: list[str | None] = [None] * (n + 1)  # index 0 不用
    for i, m in enumerate(matches):
        n_tag = int(m.group(1))
        if not (1 <= n_tag <= n):
            continue  # 越界标签忽略
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if parts[n_tag] is None or len(body) > len(parts[n_tag] or ""):
            parts[n_tag] = body
    out = [p for p in parts[1:] if p is not None]
    if len(out) != n:
        return None
    return out


def _translate_combined(
    paragraphs: list[str],
    *,
    client: Any = None,
) -> list[dict[str, Any]] | None:
    """合并多段为单次 LLM 调用。

    返回 None 表示合并失败(超长/网络/拆分错)→ 调用方回退到 translate_paragraphs。
    返回 N 个结果字典时与 paragraphs 一一对应。
    """
    if not paragraphs:
        return []
    n = len(paragraphs)
    # 用编号标签包裹每段: <¶¶¶PARA=1¶¶¶>\n段1\n\n<¶¶¶PARA=2¶¶¶>\n段2...
    parts_combined = []
    for i, p in enumerate(paragraphs, 1):
        parts_combined.append(f"<¶¶¶PARA={i}¶¶¶>\n{p}")
    combined = "\n\n".join(parts_combined)
    if len(combined) + _COMBINED_OVERHEAD_PER_PARA * n > _COMBINED_MAX_CHARS:
        return None  # 超长, 走并行

    # 加载主系统提示词并叠加多段指令
    from df_fanyi.providers.router_client import (
        TRANSLATION_SYSTEM_PROMPT,
        load_system_prompt,
    )
    base_prompt = load_system_prompt("v1") or TRANSLATION_SYSTEM_PROMPT
    # 注入 DF 活跃术语(从数据库扫输入中出现的术语子串, 让 LLM 遵守词表)
    terms_block = _format_active_terms(combined)
    system = base_prompt + _COMBINED_SYSTEM_PROMPT_ADDON.format(n=n, terms_block=terms_block)

    # 直接调云端 router, 跳过编排器缓存/词典/规则(它们只对单段有效, 合并文本不可信)
    from df_fanyi.providers.router_client import RouterChatClient, RouterError

    if client is None:
        # 2026-09-12 19:14 实测: minimax 软降级后 zhipu/glm-5.3 优先, 5 段合并
        # 大 prompt 非流式生成 >60s → 默认 60s 超时误判失败回退并行(丢术语注入)。
        # 合并调用单独抬到 180s。
        client = RouterChatClient(timeout=180.0)
    try:
        # 2026-09-12 fix: 关掉 minimax-M3 的思考机制。
        # 合并调用需要 max_tokens 都给译文用, 思考占满会被路由器判空返空。
        # 2026-09-12 补充: minimax 思考常灬 1500-3200 token; 加术语提示后,
        # prompt 从 ~800 飙升到 ~1200 token, 需给足译文预算。
        # 1500/2000/2500/4096 都会 finish=length 被截; 8192 才稳定。
        result = client.chat(
            combined,
            system=system,
            temperature=0.2,
            max_tokens=8192,
            enable_thinking=False,
        )
    except RouterError as exc:
        logger.warning("合并翻译失败, 回退并行: %s", exc)
        return None
    if not result.content:
        return None

    zh_parts = _split_combined_response(result.content, n)
    if zh_parts is None:
        logger.warning(
            "合并响应拆分错(期望 %d 段): %s",
            n, result.content[:200],
        )
        return None

    out: list[dict[str, Any]] = []
    for en, zh in zip(paragraphs, zh_parts):
        out.append({
            "en": collapse_cjk_spaces(en),
            "zh": collapse_cjk_spaces(zh),
            "model": result.model or "",
            "provider": result.provider or "",
            "confidence": 0.9,
            "error": None,
        })
    return out


def render_markdown(
    results: list[dict[str, Any]], image_path: str, *, timestamp: str
) -> str:
    """渲染归档 md: 与截图同名同目录, 含原文/译文/模型元数据/图片引用。

    2026-09-12 用户反馈: 5 段分段呈现不连贯。改为一次性拼接为连贯段落。
    图片用相对名引用, md 与 png 同目录时可直接预览。
    """
    import os

    name = os.path.basename(image_path)
    lines = [f"# FanYi 截图翻译 · {timestamp}", "", f"![原图]({name})", ""]
    # 连贯呈现: 所有段落拼成一个长段, 原文与译文并列阅读。
    # OCR 段落之间通常本就是同一段文本(被切碎), 拼接后更接近原文语义。
    full_en = " ".join(str(r.get("en") or "").strip() for r in results if r.get("en"))
    full_zh = " ".join(str(r.get("zh") or "").strip() for r in results if r.get("zh"))
    # 某段错错时 append 错误信息避免丢上下文
    errs = [r["error"] for r in results if r.get("error")]
    lines.append("## 原文")
    lines.append("")
    lines.append(full_en or "(空)")
    lines.append("")
    lines.append("**译文**")
    lines.append("")
    lines.append(full_zh or "(空)")
    if errs:
        lines.append("")
        lines.append(f"⚠️ 部分段失败: {'; '.join(errs)}")
    # 模型元数据: 采用最后一次成功的(组合调用只用一次 LLM)
    last = next((r for r in reversed(results) if not r.get("error")), results[0] if results else {})
    meta = f"模型 {last.get('model') or '?'} / {last.get('provider') or '?'} · 置信 {last.get('confidence', 0):.2f} · 合并 {len(results)} 段"
    lines.extend(["", f"<sub>{meta}</sub>", ""])
    lines.append("---")
    lines.append("<sub>DF-FanYi shot 工作流 (Ctrl+Print: slurp 选区→OCR→合并翻译→归档)</sub>")
    lines.append("")
    return "\n".join(lines)
