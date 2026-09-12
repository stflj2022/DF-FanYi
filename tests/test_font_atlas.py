"""ticket-009: CJK 字形图集生成器(scripts/generate_font_atlas.py)与已提交图集校验。

渲染路径定论(docs/audits/DFHACK_INTEGRATION.md §9): classic 53.16 位图字库不可改,
中文走 DFHack 官方 texture API —— 图集 PNG(8x12/格)由本生成器离线产出,
游戏侧 fanyi.lua 经 dfhack.textures.loadTileset 注册后逐字贴图。

- 生成器渲染逻辑需要 Pillow(可选依赖): 缺失时渲染类用例跳过;
- 已提交图集(dfhack/data/fanyi-font/)的校验只需 stdlib(PNG IHDR + JSON),
  永远执行 —— 保证仓库内图集与索引一致、游戏侧可加载。
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys_path = str(REPO / "scripts")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

import generate_font_atlas as atlas  # noqa: E402

ATLAS_DIR = REPO / "dfhack" / "data" / "fanyi-font"


# ---- 字符集规划(纯逻辑, 无 Pillow) -----------------------------------------


def test_charset_ascii_and_gb2312_level1():
    cps = atlas.build_charset("ascii+gb2312-1")
    assert 0x20 in cps and 0x7E in cps              # 可打印 ASCII
    assert ord("矮") in cps                          # GB2312 一级含常用汉字
    assert ord("缆") in cps                          # 一级字集中抽样
    assert ord("𠀀") not in cps                      # 四字节扩展区不收
    assert len(cps) == len(set(cps)), "codepoint 必须去重"
    assert len(cps) < 4096, "默认字符集必须进单页(64x64)"


def test_charset_cjk_punctuation():
    cps = atlas.build_charset("ascii+cjk-punct")
    for ch in "，。、？！…—「」（）《》":
        assert ord(ch) in cps, f"缺标点 {ch}"


def test_plan_pages_splits_overflow():
    cps = list(range(1, 5001))
    pages = atlas.plan_pages(cps, cols=64, rows=64)
    assert [p["count"] for p in pages] == [4096, 904]
    assert [p["png"] for p in pages] == ["page-000.png", "page-001.png"]
    # 分页无缝: 拼回与原序列一致
    joined = [cp for p in pages for cp in p["cps"]]
    assert joined == cps


# ---- 渲染+落盘(需 Pillow) --------------------------------------------------


def _find_ttf() -> str | None:
    """任一可用 TTF/OTF(结构冒烟; 无 CJK 字形也行, 只验证管线)。"""
    candidates = [
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def test_emit_atlas_index_and_png(tmp_path):
    pytest.importorskip("PIL")
    font = _find_ttf()
    if font is None:
        pytest.skip("no ttf/ttc found on system")
    cps = atlas.build_charset("ascii")
    pages = atlas.plan_pages(cps, cols=16, rows=4)  # 强制小页
    atlas.emit(font, face_index=0, pages=pages, out=tmp_path, tile_w=8, tile_h=12)
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert index["tile_w"] == 8 and index["tile_h"] == 12
    assert sum(p["count"] for p in index["pages"]) == len(cps)
    for page in index["pages"]:
        png = tmp_path / page["png"]
        assert png.exists(), f"缺页文件 {page['png']}"
        data = png.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG 魔数"
        width, height = struct.unpack(">II", data[16:24])
        assert width == page["cols"] * 8 and height == page["rows"] * 12


# ---- 已提交图集(游戏侧实际加载物, stdlib 校验) ------------------------------


def test_committed_atlas_removed():
    """ticket-012: 字幕条图集已随字幕条 overlay 一并删除。

    fanyi-font 目录(4 页字形贴图 + index.json + 许可证)不得再存在;
    生成器本身保留(ticket-013 会以 --subset 子集模式重新产出 textviewer-font)。
    """
    assert not ATLAS_DIR.exists(), (
        "dfhack/data/fanyi-font/ 应已删除(ticket-012); "
        "若 ticket-013 重新生成, 应输出到 dfhack/data/textviewer-font/, 不是本目录"
    )


def test_committed_atlas_license_removed():
    """ticket-012: 图集已删, 许可证随目录一同移除(生成器内仍存 OFL 声明)。"""
    assert not (ATLAS_DIR / "LICENSE-OFL.txt").exists()
