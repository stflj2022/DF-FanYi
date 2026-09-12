"""ticket-012: 字幕条 widget 必须从 OVERLAY_WIDGETS 中删除。

ticket-009 实现的 `FanyiSubtitle = defclass(...)` 字幕条小部件, ticket-012 整体删除:
- 字幕条 widget 不再注册(变量为 nil 或不含 subtitle 键)
- 不再向 DFHack overlay 框架注册纹理页(dfhack.textures 不被调用)
- 旧 `fanyi overlays on|off [cjk]` 命令变 alias: 显式告知"字幕条已删除"

无 lua5.4 时整组跳过。
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUA = shutil.which("lua5.4") or shutil.which("lua")
SCRIPT = os.path.join(REPO_ROOT, "dfhack/scripts/fanyi.lua")
HARNESS = os.path.join(REPO_ROOT, "tests/lua/fanyi_harness.lua")


@pytest.fixture(scope="module")
def lua_runner():
    if not LUA:
        pytest.skip("lua5.4 (or lua) not found on PATH")
    return LUA


def _load_script(lua: str, tmp_path) -> subprocess.CompletedProcess:
    """通过 harness 加载 fanyi.lua, 副作用写入 M.env 全局。"""
    helper = (
        "local hchunk = assert(loadfile('" + HARNESS + "'))\n"
        "local M = hchunk('" + SCRIPT + "', '" + str(tmp_path) + "')\n"
        "M.load({}, {})\n"
        "local w = M.env.OVERLAY_WIDGETS\n"
        "local has_sub = (type(w) == 'table') and (w.subtitle ~= nil)\n"
        "io.stdout:write((type(w) == 'table' and 'PRESENT' or 'NIL') .. '|' .. tostring(has_sub))\n"
    )
    return subprocess.run(
        [lua, "-e", helper],
        capture_output=True, text=True, timeout=30,
    )


def test_overlay_widgets_has_no_subtitle_key(lua_runner, tmp_path):
    """ticket-012 验收: OVERLAY_WIDGETS 不应包含 subtitle 键。"""
    out = _load_script(lua_runner, tmp_path)
    assert out.returncode == 0, f"加载失败: stdout={out.stdout!r} stderr={out.stderr!r}"
    kind, has_sub = out.stdout.strip().split("|")
    assert has_sub == "false", (
        f"OVERLAY_WIDGETS 仍包含 subtitle 键 (kind={kind}, has_subtitle={has_sub})"
    )


def test_fanyi_font_atlas_not_loaded(lua_runner, tmp_path):
    """ticket-012 验收: 字幕条图集删除后, dfhack.textures.loadTileset 不被调用。"""
    helper = (
        "local hchunk = assert(loadfile('" + HARNESS + "'))\n"
        "local M = hchunk('" + SCRIPT + "', '" + str(tmp_path) + "')\n"
        "M.install_font({['page-000.png'] = {39118}})\n"
        "M.load({}, {enable = true})\n"
        "M.load({'overlays', 'on'}, {})\n"
        "M.load({'overlays', 'on', 'cjk'}, {})\n"
        "io.stdout:write(tostring(#M.texture_loads))\n"
    )
    out = subprocess.run(
        [lua_runner, "-e", helper],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"加载失败: stdout={out.stdout!r} stderr={out.stderr!r}"
    assert out.stdout.strip() == "0", (
        f"字幕条删除后 loadTileset 不应被调用, 实际调用次数={out.stdout.strip()}, "
        f"stderr={out.stderr}"
    )


def test_fanyi_overlays_cjk_alias_prints_removal_notice(lua_runner, tmp_path):
    """ticket-012 约束: `fanyi overlays on cjk` 旧语法必须显式告知字幕条已删除。"""
    helper = (
        "local hchunk = assert(loadfile('" + HARNESS + "'))\n"
        "local M = hchunk('" + SCRIPT + "', '" + str(tmp_path) + "')\n"
        "M.load({}, {})\n"
        "M.output = {}\n"
        "M.load({'overlays', 'on', 'cjk'}, {})\n"
        "local out = M.output_joined()\n"
        "io.stdout:write(out)\n"
    )
    out = subprocess.run(
        [lua_runner, "-e", helper],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"加载失败: stdout={out.stdout!r} stderr={out.stderr!r}"
    # 必须含"字幕条"或"subtitle"或"删除"提示之一
    assert any(token in out.stdout for token in ("字幕条", "subtitle", "已删除")), (
        f"`overlays on cjk` 必须显式提示字幕条已删除, 实得: {out.stdout!r}"
    )


def test_fanyi_status_reports_subtitle_removed(lua_runner, tmp_path):
    """ticket-012 约束: `fanyi status` 必须告知 subtitle widget 已删除。"""
    helper = (
        "local hchunk = assert(loadfile('" + HARNESS + "'))\n"
        "local M = hchunk('" + SCRIPT + "', '" + str(tmp_path) + "')\n"
        "M.load({}, {})\n"
        "M.load({'status'}, {})\n"
        "io.stdout:write(M.output_joined())\n"
    )
    out = subprocess.run(
        [lua_runner, "-e", helper],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"加载失败: stdout={out.stdout!r} stderr={out.stderr!r}"
    # status 必须告知 subtitle widget 已删除
    assert "subtitle widget: removed" in out.stdout or "字幕条" in out.stdout, (
        f"fanyi status 必须告知 subtitle widget 已删除, 实得: {out.stdout!r}"
    )
    # 不应再含 "overlay小部件: fanyi.subtitle" 这种字幕条行
    assert "fanyi.subtitle" not in out.stdout, (
        f"fanyi status 不应再包含 fanyi.subtitle 行, 实得: {out.stdout!r}"
    )