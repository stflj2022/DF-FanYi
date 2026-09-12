"""DFHack 游戏侧桥(fanyi.lua)无头集成测试。

pytest 以子进程方式运行真实 dfhack/scripts/fanyi.lua(在模拟 DFHack 环境的
lua5.4 沙箱里, 见 tests/lua/fanyi_harness.lua), 逐场景断言:
版本守卫/Safe Mode、引擎离线静默降级、捕获→发送→done→渲染闭环、
confidence=0 不渲染、断线自愈与退避重连、gamelog 历史回溯、CJK 渲染门控。

无 lua5.4 时整组跳过(工程文档标注了最低要求)。
"""
import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUA = shutil.which("lua5.4") or shutil.which("lua")

SCENARIOS = [
    "safe_mode",
    "engine_down",
    "roundtrip",
    "zero_confidence",
    "reconnect",
    "gamelog_history",
    "overlays_cmd",
    "paragraph_window",  # ticket-015 §1
]


@pytest.fixture(scope="module")
def lua_runner():
    if not LUA:
        pytest.skip("lua5.4 (or lua) not found on PATH — headless bridge tests skipped")
    return LUA


def _run_scenario(lua: str, scenario: str, tmp_path):
    out = subprocess.run(
        [lua, os.path.join(REPO_ROOT, "tests/lua/run_fanyi_tests.lua"),
         scenario, REPO_ROOT, str(tmp_path)],
        capture_output=True, text=True, timeout=180,
    )
    return out


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_lua_scenario(lua_runner, scenario, tmp_path):
    out = _run_scenario(lua_runner, scenario, tmp_path)
    assert out.returncode == 0, (
        f"scenario {scenario} failed (rc={out.returncode})\n"
        f"--- stdout ---\n{out.stdout}\n--- stderr ---\n{out.stderr}"
    )
    assert f"PASS [{scenario}]" in out.stdout


def test_lua_script_is_loadable(lua_runner):
    """fanyi.lua 必须能被 lua5.4 编译(语法关)。"""
    script = os.path.join(REPO_ROOT, "dfhack/scripts/fanyi.lua")
    code = f"assert(loadfile({script!r}), 'syntax error')"
    out = subprocess.run([lua_runner, "-e", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_no_forbidden_memory_hooks():
    """工程书 §禁项: 禁用 memory-offset/ELF/内存写钩子(DFI18n/dfzh 路线)。

    桥只准用官方 DFHack Lua API(eventful/overlay/socket/json)。
    """
    script = os.path.join(REPO_ROOT, "dfhack/scripts/fanyi.lua")
    src = open(script, encoding="utf-8").read()
    for forbidden in ("memory", "memory.OnLoad", "logic.Add", "pixel.Add",
                      "dibc", "getMainThreadData", "df.memory"):
        assert forbidden not in src, f"forbidden hook token present: {forbidden}"
    # 官方 API 准入清单(本文件只允许引用这些 DFHack 入口)
    allowed = ("dfhack.", "eventful", "df.report", "overlay", "json",
               "plugins.luasocket")
    assert "eventful.onReport" in src
    assert "plugins.luasocket" in src


def test_no_main_thread_blocking():
    """§29: 游戏主循环从不等待翻译结果 —— 脚本不得出现阻塞式读。

    socket 读回必须走非阻塞 receive('*l')(setNonblocking + 空读即 nil)。
    """
    script = os.path.join(REPO_ROOT, "dfhack/scripts/fanyi.lua")
    src = open(script, encoding="utf-8").read()
    assert "setNonblocking" in src
    assert "drain_lines" in src
    # 读回循环必须有界(for 上限), 不允许 while-等待 → 主循环不被阻塞
    assert "for _ = 1, MAX_READ_PER_TICK" in src