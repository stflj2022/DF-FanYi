"""截图翻译工作流(2026-09-12 用户方案, Ctrl+Print)。

流程: slurp 选区 → grim 截图 → tesseract OCR(TSV) → 段落重组 → 翻译管线 → zenity 大浮窗。

弃游戏内 F11 拖框的原因: Steam 版 DF 53.x 的 enabler.mouse_lbit 不随物理按键更新,
帧驱动拖框无法锚定(实测 2026-09-12 14:44, de29f9f); 系统级截图由 Hyprland 完成,
中文渲染由桌面完成, 绕开 DFHack 鼠标事件与 CP437 图集两大坑。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any

__all__ = [
    "OcrWord",
    "parse_tsv",
    "assemble_paragraphs",
    "ocr_image",
    "translate_paragraphs",
    "process_image",
    "render_markdown",
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


_TSV_COLS = 12  # level page block par line word left top width height conf text
_LETTER = re.compile(r"[A-Za-z]")
_VOWEL = re.compile(r"[aeiouyAEIOUY]")
_ALNUM = re.compile(r"[A-Za-z0-9]")


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
            left, conf = int(parts[6]), float(parts[10])
        except ValueError:
            continue  # 表头/坏行
        text = parts[11].strip()
        # not (conf >= min) 同时拦住 nan(nan 与任何数比较均为 False)
        if not text or not (conf >= min_conf):
            continue
        words.append(OcrWord(block, par, line, left, conf, text))
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
    """过滤地图区图形 OCR 噪声: 需够多字母、含元音、字母占比过半。"""
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


def ocr_image(image_path: str, *, timeout: int = 30) -> list[OcrWord]:
    """对图片跑 tesseract(eng, 自动版面), 返回词条。"""
    proc = subprocess.run(
        ["tesseract", image_path, "stdout", "-l", "eng", "tsv"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract 失败: {proc.stderr.strip()[:200]}")
    return parse_tsv(proc.stdout)


def translate_paragraphs(
    paragraphs: list[str], orch: Any, *, workers: int = 4
) -> list[dict[str, Any]]:
    """逐段过翻译管线(缓存→词典→规则→LLM→验证), 线程池并行。

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
        r = local.orch.translate(text, context={"source": "screenshot"})
        return {
            "en": text,
            "zh": r.text,
            "model": getattr(r, "model", ""),
            "provider": getattr(r, "provider", ""),
            "confidence": getattr(r, "confidence", 0.0),
            "error": getattr(r, "error", None),
        }

    if not paragraphs:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(paragraphs))) as ex:
        return list(ex.map(_one, paragraphs))


def process_image(image_path: str, orch: Any) -> list[dict[str, Any]]:
    """OCR → 段落 → 翻译, 全流程。"""
    return translate_paragraphs(assemble_paragraphs(ocr_image(image_path)), orch)


def render_markdown(
    results: list[dict[str, Any]], image_path: str, *, timestamp: str
) -> str:
    """渲染归档 md: 与截图同名同目录, 含原文/译文/模型元数据/图片引用。

    图片用相对名引用, md 与 png 同目录时可直接预览。
    """
    import os

    name = os.path.basename(image_path)
    lines = [f"# FanYi 截图翻译 · {timestamp}", "", f"![原图]({name})", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"## {i}. 原文")
        lines.append("")
        lines.append(r["en"])
        lines.append("")
        lines.append("**译文**")
        lines.append("")
        lines.append(r["zh"])
        meta = f"模型 {r.get('model') or '?'} / {r.get('provider') or '?'} · 置信 {r.get('confidence', 0):.2f}"
        if r.get("error"):
            meta += f" · 错误: {r['error']}"
        lines.extend(["", f"<sub>{meta}</sub>", ""])
    lines.append("---")
    lines.append("<sub>DF-FanYi shot 工作流 (Ctrl+Print: slurp 选区→OCR→翻译→归档)</sub>")
    lines.append("")
    return "\n".join(lines)
