#!/usr/bin/env python3
"""DF-FanYi CJK 字形图集生成器(ticket-009)。

把 TTF/TTC 字体的字形渲染成 DF tile 网格 PNG 图集 + index.json, 供
fanyi.lua 经 dfhack.textures.loadTileset 注册后逐字贴图 —— classic 53.16
位图字库(CP437, 无 TRUETYPE)不改一字, 全程 DFHack 官方 texture API
(证据与定论: docs/audits/DFHACK_INTEGRATION.md §9)。

用法(需要 Pillow, 生成是离线一次性动作; 产物已随仓库提交, 玩家无需重跑):
    python3 scripts/generate_font_atlas.py \
        --font /usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc --face SC \
        --charset ascii+cjk-punct+gb2312-1 \
        --tile 8x12 --page 64x64 --out dfhack/data/fanyi-font

产物:
- page-NNN.png   RGBA 图集, 每格 tile_w x tile_h 像素, 白字+黑影(任意底色可读);
- index.json     {tile_w, tile_h, pages:[{png, cols, rows, count, cps:[...]}]},
                  数组下标即 tile 在页内格位, cps[i] = 该格的 Unicode codepoint;
- 生成器不写许可文件 —— LICENSE-OFL.txt 手工维护(字体 SIL OFL 1.1 再分发要求)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---- 字符集 ----------------------------------------------------------------


def _gb2312_level1() -> str:
    """GB2312 一级常用汉字(3755 字): 区码 0xB0-0xD7, 位码 0xA1-0xFE。"""
    out: list[str] = []
    for row in range(0xB0, 0xD8):
        for cell in range(0xA1, 0xFF):
            try:
                out.append(bytes([row, cell]).decode("gb2312"))
            except UnicodeDecodeError:
                pass  # 区尾无效组合
    return "".join(out)


CJK_PUNCT = "，。、；：？！…—·「」『』（）《》【】“”‘’％×÷"

CHARSET_PARTS = {
    "ascii": "".join(chr(c) for c in range(0x20, 0x7F)),
    "cjk-punct": CJK_PUNCT,
    "gb2312-1": _gb2312_level1(),
}


def build_charset(spec: str) -> list[int]:
    """'ascii+cjk-punct+gb2312-1' → 去重有序 codepoint 表(先到先得)。"""
    seen: dict[int, None] = {}
    for part in spec.split("+"):
        part = part.strip()
        if not part:
            continue
        text = CHARSET_PARTS.get(part)
        if text is None:
            raise SystemExit(f"未知字符集段: {part!r}(可用: {', '.join(CHARSET_PARTS)})")
        for ch in text:
            seen.setdefault(ord(ch), None)
    return list(seen)


def plan_pages(cps: list[int], *, cols: int, rows: int) -> list[dict]:
    """codepoint 表 → 连续分页(每页 cols*rows 格, 命名 page-NNN.png)。"""
    per = cols * rows
    pages: list[dict] = []
    for i in range(0, len(cps), per):
        chunk = cps[i : i + per]
        pages.append(
            {
                "png": f"page-{len(pages):03d}.png",
                "cols": cols,
                "rows": rows,
                "count": len(chunk),
                "cps": chunk,
            }
        )
    return pages


# ---- 渲染(需 Pillow) --------------------------------------------------------


def _load_font(font_path: str, face_index: int | str, size_px: int):
    """face_index: 整数下标, 或 'SC'/'JP' 等子字体名(自动扫 TTC 找族名后缀)。"""
    from PIL import ImageFont

    if isinstance(face_index, int) or face_index.isdigit():
        return ImageFont.truetype(font_path, size_px, index=int(face_index))
    want = str(face_index).upper()
    for i in range(64):
        try:
            font = ImageFont.truetype(font_path, size_px, index=i)
        except OSError:
            break
        family = font.getname()[0].upper()
        if family.endswith(want) or f" {want} " in f" {family} ":
            return font
    raise SystemExit(f"TTC 中未找到 face {face_index!r}(font={font_path})")


def render_pages(font_path: str, face_index: int | str, pages: list[dict], *,
                 tile_w: int, tile_h: int, out: Path) -> None:
    """逐页渲染 RGBA PNG: 白色字形 + 1px 黑影, 居中于格子。

    小网格适配: 以 2x 尺寸取字形蒙版, 裁紧 bbox 后等比缩到 (tile_w-1, tile_h-1)
    内再居中 —— 8x12 下笔画清晰度优先于几何保真。
    """
    from PIL import Image, ImageFont

    size_px = tile_h * 2  # 采样尺寸(再缩放, 抗锯齿更足)
    font = _load_font(font_path, face_index, size_px)
    fit_w, fit_h = tile_w - 1, tile_h - 1
    for page in pages:
        img = Image.new("RGBA", (page["cols"] * tile_w, page["rows"] * tile_h), (0, 0, 0, 0))
        for i, cp in enumerate(page["cps"]):
            ch = chr(cp)
            try:
                mask = font.getmask(ch, mode="L")
                gw, gh = mask.size
            except Exception:
                continue  # 字体渲染失败的极个别字符: 留空格(游戏侧跳格)
            if gw == 0 or gh == 0:
                continue
            scale = min(fit_w / gw, fit_h / gh, 1.0)
            nw, nh = max(1, round(gw * scale)), max(1, round(gh * scale))
            glyph = Image.frombytes("L", (gw, gh), bytes(mask)).resize((nw, nh))
            # 缩放抗锯齿会把笔画摊成半透明(核心 α~200): 提对比让笔画实心,
            # 边缘保留渐变(游戏黑底下仍呈清晰白字而非灰字)
            glyph = glyph.point(lambda v: min(255, int(v * 1.7) + 20))
            col, row = i % page["cols"], i // page["cols"]
            x0 = col * tile_w + (tile_w - nw) // 2
            y0 = row * tile_h + (tile_h - nh) // 2
            # 黑影(右下 1px)提对比, 白色本体
            shadow = Image.new("RGBA", (nw, nh), (0, 0, 0, 255))
            img.paste(shadow, (x0 + 1, y0 + 1), glyph)
            solid = Image.new("RGBA", (nw, nh), (255, 255, 255, 255))
            img.paste(solid, (x0, y0), glyph)
        img.save(out / page["png"])
        print(f"  ✓ {page['png']}: {page['count']} 字形")


def emit(font_path: str, face_index: int | str, pages: list[dict], *,
         out: Path, tile_w: int, tile_h: int) -> None:
    """渲染所有页 + 写 index.json(渲染是可跳过的: 已提交产物时测试走 stdlib 校验)。"""
    out.mkdir(parents=True, exist_ok=True)
    render_pages(font_path, face_index, pages, tile_w=tile_w, tile_h=tile_h, out=out)
    index = {
        "version": 1,
        "tile_w": tile_w,
        "tile_h": tile_h,
        "pages": pages,
    }
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    total = sum(p["count"] for p in pages)
    print(f"图集完成: {len(pages)} 页 / {total} 字形 → {out}")


# ---- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DF-FanYi CJK 字形图集生成器")
    parser.add_argument("--font", required=True, help="TTF/TTC 字体路径")
    parser.add_argument("--face", default="0", help="TTC 子字体下标或名(SC/JP/...), 默认 0")
    parser.add_argument("--charset", default="ascii+cjk-punct+gb2312-1",
                        help="ascii/cjk-punct/gb2312-1 以 + 组合")
    parser.add_argument("--tile", default="8x12", help="tile 尺寸(默认 8x12, classic 网格)")
    parser.add_argument("--page", default="64x64", help="每页格数(默认 64x64=4096 格)")
    parser.add_argument("--out", default="dfhack/data/fanyi-font", help="输出目录")
    args = parser.parse_args(argv)

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("需要 Pillow: pip install Pillow(或用 uv venv)", file=sys.stderr)
        return 2

    tw_s, th_s = args.tile.lower().split("x")
    pg_s, pr_s = args.page.lower().split("x")
    tile_w, tile_h = int(tw_s), int(th_s)
    cols, rows = int(pg_s), int(pr_s)
    cps = build_charset(args.charset)
    pages = plan_pages(cps, cols=cols, rows=rows)
    emit(args.font, args.face, pages, out=Path(args.out), tile_w=tile_w, tile_h=tile_h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
