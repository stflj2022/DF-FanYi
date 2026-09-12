"""ticket-013: textviewer 弹窗内嵌翻译覆盖层。

覆盖验收项:
1. 字形图集生成器 --subset 子集模式(纯逻辑, 无 Pillow);
2. 已提交图集 dfhack/data/textviewer-font/ 的 stdlib 校验(scale=2, PNG 魔数);
3. 引擎 JSON-RPC inline_translate 往返: 缓存未命中(queued→fetch_done)与
   命中(cached=True, 不再调 LLM)两条路径;
4. fanyi.lua 无头 harness:
   - OVERLAY_WIDGETS 注册 textviewer widget;
   - 捕获→翻译→S.tv_cache→TextviewerInline:onRenderFrame(dc, 80, 25) 非空;
   - 缓存 get/set/expire(离开 textviewer 指纹 → 清空);
   - `fanyi overlays on|off textviewer` 开关 + overlay enable 命令;
   - `fanyi status` 输出包含 textviewer_inline (on/off)。

无 lua5.4 时 Lua 组跳过。
"""
from __future__ import annotations

import json
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from df_fanyi.bridge.protocol import encode_line, make_request
from df_fanyi.bridge.server import BridgeServer
from df_fanyi.core.orchestrator import Orchestrator
from df_fanyi.core.queue import TranslationScheduler

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
import sys  # noqa: E402

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import generate_font_atlas as atlas  # noqa: E402

TV_ATLAS_DIR = REPO / "dfhack" / "data" / "textviewer-font"
LUA = subprocess.run(["which", "lua5.4"], capture_output=True, text=True).stdout.strip() \
    or subprocess.run(["which", "lua"], capture_output=True, text=True).stdout.strip()
FANYI_LUA = REPO / "dfhack" / "scripts" / "fanyi.lua"
HARNESS = REPO / "tests" / "lua" / "fanyi_harness.lua"

CN_REPLY = "古老的矮人厅中回荡着镐子敲击石头的声音。"


# ---- 生成器: --subset 子集字符集(纯逻辑) ------------------------------------


class _Recorder:
    """假 LLM(§15 回放), 与 test_bridge_server.Recorder 同义。"""

    def __init__(self, reply: str | None = None) -> None:
        self.calls: list[str] = []
        self.reply = reply

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        return self.reply if self.reply is not None else CN_REPLY * max(1, round(len(text) / 90))


def test_build_subset_charset_frequency_order(tmp_path: Path) -> None:
    (tmp_path / "a.jsonl").write_text(
        json.dumps({"key": "k", "zh": "矮人矮人矿工"}, ensure_ascii=False) + "\n"
        + json.dumps({"key": "k2", "zh": "石头的门"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "legacy-dictionary.csv").write_text(
        "text,translation\nhello,你好\n", encoding="utf-8",
    )
    cps = atlas.build_subset_charset(2, tmp_path)
    # 前 2 位是语料 top-2 高频汉字: 矮(2 次) 人(2 次) → 按次数并列取码点小者在前
    han = [cp for cp in cps if 0x4E00 <= cp <= 0x9FFF]
    assert han == [ord("人"), ord("矮")], f"子集应按 (-频次, 码点) 排序: {han!r}"
    # 基础集恒在: 可打印 ASCII + CJK 标点
    assert 0x20 in cps and 0x7E in cps
    for ch in "，。、？！…—":
        assert ord(ch) in cps, f"缺标点 {ch}"
    # 无重复
    assert len(cps) == len(set(cps))


def test_build_subset_charset_excludes_non_han(tmp_path: Path) -> None:
    (tmp_path / "b.jsonl").write_text(
        json.dumps({"zh": "abc 123 ，。"}, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    cps = atlas.build_subset_charset(10, tmp_path)
    han = [cp for cp in cps if 0x4E00 <= cp <= 0x9FFF]
    assert han == [], f"非汉字不应进入子集: {han!r}"


# ---- 已提交图集 dfhack/data/textviewer-font/ --------------------------------


def test_committed_textviewer_atlas_present() -> None:
    """ticket-013: 图集随仓库提交(subset 800, scale=2), 游戏侧可加载。"""
    assert TV_ATLAS_DIR.is_dir(), f"缺目录 {TV_ATLAS_DIR}(scripts/generate_font_atlas.py --subset 800 --scale 2)"
    index = json.loads((TV_ATLAS_DIR / "index.json").read_text(encoding="utf-8"))
    assert index["tile_w"] == 8 and index["tile_h"] == 12
    assert index["scale"] == 2, "textviewer 图集应为大字模式(scale=2)"
    total = sum(p["count"] for p in index["pages"])
    # ascii(95) + cjk 标点(≥10) + 子集 800
    assert 900 <= total <= 1024, f"字形总数应≈922(单页 64x64/scale2): {total}"
    assert len(index["pages"]) == 1, "922 字形应进单页(1024 格/页)"
    for page in index["pages"]:
        png = TV_ATLAS_DIR / page["png"]
        assert png.exists(), f"缺页文件 {page['png']}"
        data = png.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG 魔数"
        width, height = struct.unpack(">II", data[16:24])
        assert width == page["cols"] * 8 and height == page["rows"] * 12
    # 子集高频字必在(来自已提交语料 top-800)
    all_cps = {cp for p in index["pages"] for cp in p["cps"]}
    for ch in "矮人石头把铜铁金在了一是":
        assert ord(ch) in all_cps, f"高频字 {ch} 缺失"


# ---- 引擎 JSON-RPC: inline_translate 往返 ------------------------------------


class _JsonLineClient:
    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""
        self._rid = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._rid += 1
        self.sock.sendall(
            encode_line(make_request(method, params or {}, rid=self._rid)).encode("utf-8")
        )
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        return json.loads(raw)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def inline_bridge():
    recorder = _Recorder()
    scheduler = TranslationScheduler(
        Orchestrator(llm=recorder), workers=1, max_attempts=3, max_queue=32, autostart=True
    )
    server = BridgeServer(scheduler, transport="tcp", port=0, version="0.1.0-test")
    server.start()
    yield server, recorder
    server.stop()
    scheduler.stop() if hasattr(scheduler, "stop") else None


class TestInlineTranslate:
    def test_miss_async_then_hit(self, inline_bridge) -> None:
        """未命中(长文→queued) → fetch_done 回放 → 再请求命中 cached=True, 不重调 LLM。"""
        server, recorder = inline_bridge
        client = _JsonLineClient("127.0.0.1", server.bound_port)
        try:
            long_text = ("Prepare to guide your stout charges through a world of "
                         "unimaginable depth and complexity where every dwarf has "
                         "hopes and dreams. ") * 4
            r1 = client.call("inline_translate", {
                "text": long_text, "context": "textviewer",
                "title": "Welcome", "event_id": "tv-test-1",
            })["result"]
            assert r1["status"] == "queued", f"首请求应未命中走异步: {r1}"
            assert r1["cached"] is False
            assert r1["event_id"] == "tv-test-1"
            done = None
            for _ in range(100):
                got = client.call("fetch_done")
                for rec in got["result"]["translations"]:
                    if rec.get("status") == "done" and rec.get("translated_text"):
                        done = rec  # inline 路径 event_id 回放为 ""(无 _pending 登记), 按内容断言
                        break
                if done is not None:
                    break
                time.sleep(0.05)
            assert done is not None, "inline 异步完成未回放"
            assert done["status"] == "done" and done["translated_text"]

            r2 = client.call("inline_translate", {
                "text": long_text, "context": "textviewer",
                "title": "Welcome", "event_id": "tv-test-1",
            })["result"]
            assert r2["status"] == "done" and r2["cached"] is True
            assert r2["translated_text"] == done["translated_text"]
            assert len(recorder.calls) == 1, f"缓存命中不应再调 LLM: {recorder.calls}"
        finally:
            client.close()

    def test_sync_fast_path_then_cached(self, inline_bridge) -> None:
        """词典快路径(同步 done) → 首次入缓存, 二次命中。"""
        server, recorder = inline_bridge
        client = _JsonLineClient("127.0.0.1", server.bound_port)
        try:
            r1 = client.call("inline_translate", {
                "text": "Dwarf", "context": "textviewer", "event_id": "tv-dict-1",
            })["result"]
            assert r1["status"] == "done" and r1["cached"] is False
            assert r1["translated_text"] == "矮人"
            r2 = client.call("inline_translate", {
                "text": "Dwarf", "context": "textviewer", "event_id": "tv-dict-1",
            })["result"]
            assert r2["cached"] is True
            assert len(recorder.calls) == 0
        finally:
            client.close()

    def test_version_lists_inline_translate(self, inline_bridge) -> None:
        server, _ = inline_bridge
        client = _JsonLineClient("127.0.0.1", server.bound_port)
        try:
            methods = client.call("version")["result"]["methods"]
            assert "inline_translate" in methods
        finally:
            client.close()

    def test_invalid_text_rejected(self, inline_bridge) -> None:
        server, _ = inline_bridge
        client = _JsonLineClient("127.0.0.1", server.bound_port)
        try:
            resp = client.call("inline_translate", {"text": ""})
            assert resp["error"]["code"] == -32602
            resp = client.call("inline_translate", {"text": 123})
            assert resp["error"]["code"] == -32602
        finally:
            client.close()


# ---- fanyi.lua 无头 harness --------------------------------------------------

pytestmark_lua = pytest.mark.skipif(not LUA, reason="lua5.4 (or lua) not found on PATH")


def _run_lua(code: str, tmp_path) -> subprocess.CompletedProcess:
    helper = (
        "local hchunk = assert(loadfile('" + str(HARNESS) + "'))\n"
        "local M = hchunk('" + str(FANYI_LUA) + "', '" + str(tmp_path) + "')\n"
        + code
    )
    return subprocess.run([LUA, "-e", helper], capture_output=True, text=True, timeout=60)


@pytestmark_lua
class TestLuaTextviewerInline:
    def test_widget_registered(self, tmp_path) -> None:
        out = _run_lua(
            "M.load({}, {})\n"
            "local w = M.env.OVERLAY_WIDGETS\n"
            "assert(type(w) == 'table', 'OVERLAY_WIDGETS 缺失')\n"
            "assert(w.textviewer, 'textviewer widget 未注册')\n"
            "assert(M.env.TextviewerInline, 'TextviewerInline 类未定义')\n"
            "local vs = M.env.TextviewerInline.ATTRS.viewscreens\n"
            "local found = false\n"
            "for _, n in ipairs(vs) do if n == 'viewscreen_textviewerst' then found = true end end\n"
            "assert(found, 'viewscreens 应含 viewscreen_textviewerst')\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r}"
        assert out.stdout.strip() == "OK"

    def test_roundtrip_paint_nonempty(self, tmp_path) -> None:
        """捕获 textviewer → 引擎应答 → S.tv_cache → onRenderFrame(dc,80,25) 非空。"""
        out = _run_lua(
            "M.engine_up = true\n"
            "M.install_font({['page-000.png'] = {0x4EBA, 0x4E00}}, 8, 12, 'textviewer-font', 2)\n"
            "M.set_textviewer('Welcome to the fortress', {'Prepare to guide your stout charges carefully.'})\n"
            "M.load({}, {enable = true})\n"
            "M.load({'overlays', 'on', 'textviewer'}, {})\n"
            # tick 每 12 帧, 捕获在 tick%20==0 → 需 ≥21 tick
            "M.step(260)\n" 
            "M.engine_auto_respond('欢迎人的到来。', 0.9)\n"
            "M.step(40)\n"
            "local st = M.state()\n"
            "local n = 0; for _ in pairs(st.tv_cache) do n = n + 1 end\n"
            "assert(n >= 1, 'tv_cache 应有译文')\n"
            "assert(st.tv_on == true, 'tv_on')\n"
            "assert(st.tv_font.installed, '图集应已装载(scale=2)')\n"
            "assert(st.tv_font.scale == 2, 'scale 应为 2')\n"
            "assert(#M.texture_loads >= 1, 'loadTileset 应被调用')\n"
            "local dc = M.make_dc()\n"
            "local w = M.env.TextviewerInline({})\n"
            "local painted = w:onRenderFrame(dc, 80, 25)\n"
            "assert(painted and painted > 0, 'onRenderFrame 应绘制非空: ' .. tostring(painted))\n"
            "assert(#dc.tiles > 0, 'dc.tiles 非空')\n"
            "io.stdout:write('PAINTED=' .. tostring(painted))\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r} stdout={out.stdout!r}"
        assert out.stdout.startswith("PAINTED=")

    def test_cache_get_set_expire(self, tmp_path) -> None:
        out = _run_lua(
            "M.load({}, {})\n"
            "local env = M.env\n"
            "assert(env.fanyi_tv_cache_set('tv-1', '你好，矮人。', '欢迎'), 'set 失败')\n"
            "local rec = env.fanyi_tv_cache_get('tv-1')\n"
            "assert(rec and rec.text == '你好，矮人。' and rec.title == '欢迎', 'get 失败')\n"
            "-- expire: 离开 textviewer(指纹不含) → 清空\n"
            "local dropped = env.fanyi_tv_cache_gc('viewscreen_dwarfmodest<viewscreen_titleless')\n"
            "assert(dropped == 1, '应清掉 1 条: ' .. tostring(dropped))\n"
            "assert(env.fanyi_tv_cache_get('tv-1') == nil, '清空后 get 应为 nil')\n"
            "-- 仍在 textviewer → 不清\n"
            "env.fanyi_tv_cache_set('tv-2', '第二页', '教程')\n"
            "assert(env.fanyi_tv_cache_gc('<viewscreen_textviewerst:0x1>') == 0, '不应清')\n"
            "assert(env.fanyi_tv_cache_get('tv-2'), '不应被清掉')\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r}"
        assert out.stdout.strip() == "OK"

    def test_overlays_toggle_and_status(self, tmp_path) -> None:
        out = _run_lua(
            "M.load({}, {})\n"
            "local st = M.state()\n"
            "assert(st.tv_on == true, '初始应 on(默认开; 图集/引擎未就绪时不绘制)')\n"
            "M.load({'overlays', 'off', 'textviewer'}, {})\n"
            "assert(M.state().tv_on == false, 'off 后应 false')\n"
            "M.load({'overlays', 'on', 'textviewer'}, {})\n"
            "assert(M.state().tv_on == true, 'on 后应 true')\n"
            "local enabled = false\n"
            "for _, c in ipairs(M.run_commands) do\n"
            "  if c[1] == 'overlay' and c[2] == 'enable' and c[3] == 'fanyi.textviewer' then enabled = true end\n"
            "end\n"
            "assert(enabled, '应执行 overlay enable fanyi.textviewer')\n"
            "M.output = {}\n"
            "M.load({'status'}, {})\n"
            "local out = M.output_joined()\n"
            "assert(out:find('textviewer_inline %(on%)'), 'status 应含 textviewer_inline (on): ' .. out)\n"
            "M.load({'overlays', 'off', 'textviewer'}, {})\n"
            "assert(M.state().tv_on == false, 'off 后应 false')\n"
            "M.output = {}\n"
            "M.load({'status'}, {})\n"
            "assert(M.output_joined():find('textviewer_inline %(off%)'), 'status 应含 off')\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r}"
        assert out.stdout.strip() == "OK"
