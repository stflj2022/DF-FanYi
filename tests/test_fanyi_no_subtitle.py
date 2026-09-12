"""ticket-012: fanyi_render_inline_payload 必须返回空载荷。

字幕条删除后, `fanyi_render_inline_payload()` 仅为保留签名的桩函数 —
后续 ticket-013/014 会接管 textviewer/announcement 内嵌覆盖层。
无头测试通过 lua5.4 沙箱 + 真实 fanyi.lua 加载, 断言返回值类型与长度。

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


def _invoke_render_inline_payload(lua: str, tmp_path) -> subprocess.CompletedProcess:
    """用 harness 加载 fanyi.lua, 调用 fanyi_render_inline_payload(),
    以序列化结果写到 stdout(便于 python 端稳定断言)。"""
    helper = (
        "local hchunk = assert(loadfile('" + HARNESS + "'))\n"
        "local M = hchunk('" + SCRIPT + "', '" + str(tmp_path) + "')\n"
        "M.load({}, {})\n"
        "local p = M.env.fanyi_render_inline_payload\n"
        "if type(p) ~= 'function' then\n"
        "  io.stderr:write('NOFUNC\\n')\n  os.exit(2)\n"
        "end\n"
        "local r = p()\n"
        "io.stdout:write(type(r) .. '|' .. tostring(#r) .. '|' .. tostring(r[1]) .. '\\n')\n"
    )
    return subprocess.run(
        [lua, "-e", helper],
        capture_output=True, text=True, timeout=30,
    )


def test_fanyi_render_inline_payload_returns_empty(lua_runner, tmp_path):
    """ticket-012 验收: 字幕条删除后 payload 函数必须返回 {} (空表, 长度 0)。"""
    out = _invoke_render_inline_payload(lua_runner, tmp_path)
    assert out.returncode == 0, f"加载/调用失败: stdout={out.stdout!r} stderr={out.stderr!r}"
    assert out.stdout.startswith("table|0|"), (
        f"fanyi_render_inline_payload() 应返回空表, 实得: {out.stdout!r}"
    )


def test_fanyi_render_inline_payload_signature_preserved(lua_runner, tmp_path):
    """ticket-012 约束: 函数签名保留供 ticket-013/014 复用 — 必须可调用且参数列表兼容。"""
    helper = (
        "local hchunk = assert(loadfile('" + HARNESS + "'))\n"
        "local M = hchunk('" + SCRIPT + "', '" + str(tmp_path) + "')\n"
        "M.load({}, {})\n"
        "local ok, err = pcall(M.env.fanyi_render_inline_payload)\n"
        "io.stdout:write(ok and 'OK' or ('ERR:' .. tostring(err)))\n"
    )
    out = subprocess.run(
        [lua_runner, "-e", helper],
        capture_output=True, text=True, timeout=30,
    )
    assert out.stdout.strip() == "OK", (
        f"fanyi_render_inline_payload() 调用失败: stdout={out.stdout!r} stderr={out.stderr!r}"
    )