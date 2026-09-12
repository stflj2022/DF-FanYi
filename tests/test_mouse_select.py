"""ticket-017 屏上选词翻译 (F11/F12) 端到端无头测试。"""
from __future__ import annotations
import os
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCENARIO = "mouseselect"
LUA = "/usr/bin/lua"
TMPDIR_BASE = Path("/tmp/df-fanyi-test-ms")


def _run_lua(snippet: str, tmp: Path) -> subprocess.CompletedProcess:
    """在 harness 装载后, 跑一段 lua 测试代码。"""
    boot = (
        f"local hchunk = assert(loadfile('{REPO}/tests/lua/fanyi_harness.lua'))\n"
        f"local M = hchunk('{REPO}/dfhack/scripts/fanyi.lua', '{tmp}')\n"
    )
    out = subprocess.run(
        [LUA, "-e", boot + snippet],
        capture_output=True, text=True, timeout=120,
    )
    return out


@pytest.fixture
def tmp() -> Path:
    TMPDIR_BASE.mkdir(exist_ok=True)
    d = TMPDIR_BASE / "scratch"
    d.mkdir(exist_ok=True)
    return d


def test_mouseselect_scenario_passes() -> None:
    """mouseselect scenario 全 7 步通过(行 1: 启动+连接; 2: F11 选词+填 text;
    3: 验证发送; 4: 模拟 fetch_done 填译文; 5: 渲染浮窗; 6: F12 关闭; 7: 空文本提示)"""
    out = subprocess.run(
        [LUA, str(REPO / "tests/lua/run_fanyi_tests.lua"), SCENARIO, str(REPO), str(TMPDIR_BASE)],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, (
        f"mouseselect scenario FAILED\nstdout={out.stdout!r}\nstderr={out.stderr!r}"
    )
    assert "PASS [mouseselect]" in out.stdout


def test_mouseselect_noatlas_scenario_passes() -> None:
    """2026-09-12 豆腐修复: 无图集时 F11 提示/浮窗回退纯 ASCII 英文行,
    dc:string 收到的每一行都不含非 ASCII 字节(中文进 CP437 必豆腐)。"""
    out = subprocess.run(
        [LUA, str(REPO / "tests/lua/run_fanyi_tests.lua"),
         "mouseselect_noatlas", str(REPO), str(TMPDIR_BASE)],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, (
        f"mouseselect_noatlas scenario FAILED\nstdout={out.stdout!r}\nstderr={out.stderr!r}"
    )
    assert "PASS [mouseselect_noatlas]" in out.stdout


def test_mouseselect_widget_registered(tmp) -> None:
    """F11 widget 'mouse_select' 已在 OVERLAY_WIDGETS 注册, 可被 enable。"""
    snippet = (
        "M.load({'start'}, {})\n"
        "M.step(5)\n"
        "local ok = type(M.env.MouseSelect) == 'table'\n"
        "io.stdout:write(ok and 'OK' or 'NO')\n"
    )
    out = _run_lua(snippet, tmp)
    assert out.returncode == 0, f"stderr={out.stderr!r}"
    assert out.stdout.strip() == "OK"


def test_mouseselect_keybinding_registered(tmp) -> None:
    """F11/F12 keybinding 已被 dfhack.run_command 注册。"""
    snippet = (
        "M.load({'start'}, {})\n"
        "M.step(5)\n"
        "local has_f11, has_f12 = false, false\n"
        "for _, c in ipairs(M.keybinding_adds) do\n"
        "  if c:find('F11') and c:find('mouseselect') then has_f11 = true end\n"
        "  if c:find('F12') and c:find('mousedismiss') then has_f12 = true end\n"
        "end\n"
        "io.stdout:write(has_f11 and has_f12 and 'OK' or 'NO')\n"
    )
    out = _run_lua(snippet, tmp)
    assert out.returncode == 0, f"stderr={out.stderr!r}"
    assert out.stdout.strip() == "OK"


def test_mouseselect_overlay_enabled(tmp) -> None:
    """'overlay enable fanyi.mouse_select' 命令已发出。"""
    snippet = (
        "M.load({'start'}, {})\n"
        "M.step(5)\n"
        "local found = false\n"
        "for _, c in ipairs(M.run_commands) do\n"
        "  if c[1] == 'overlay' and c[2] == 'enable' and c[3] == 'fanyi.mouse_select' then\n"
        "    found = true\n"
        "  end\n"
        "end\n"
        "io.stdout:write(found and 'OK' or 'NO')\n"
    )
    out = _run_lua(snippet, tmp)
    assert out.returncode == 0, f"stderr={out.stderr!r}"
    assert out.stdout.strip() == "OK"
