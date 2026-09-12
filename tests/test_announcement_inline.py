"""ticket-014: 公告面板内嵌翻译覆盖层。

覆盖验收项:
1. fanyi.lua 无头 harness:
   - OVERLAY_WIDGETS 注册 announcement widget(viewscreens 含 dwarfmode/adventur);
   - 捕获(onReport)→翻译→S.ann_cache→AnnouncementInline:onRenderFrame(dc,40,10) 非空;
   - 公告缓存 get/set/expire(默认 TTL 90s, 超时清除/原文自然显示);
   - 多公告行构建: 最新在前, 历史每条 1 行(缩短), 总行数有界;
   - `fanyi overlays on|off announcement` 开关 + overlay enable/disable 命令;
   - `fanyi status` 输出包含 announcement_inline (on/off);
   - inline_translate 路由: context='announcement'; 常规队列已覆盖的不双发;
     引擎同步 done 应答 → 入公告缓存。
2. 引擎 JSON-RPC: inline_translate(context='announcement') 二次命中 cached=True。

无 lua5.4 时 Lua 组跳过。
"""
from __future__ import annotations

import json
import socket
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
LUA = subprocess.run(["which", "lua5.4"], capture_output=True, text=True).stdout.strip() \
    or subprocess.run(["which", "lua"], capture_output=True, text=True).stdout.strip()
FANYI_LUA = REPO / "dfhack" / "scripts" / "fanyi.lua"
HARNESS = REPO / "tests" / "lua" / "fanyi_harness.lua"

CN_REPLY = "古老的矮人厅中回荡着镐子敲击石头的声音。"


# ---- 引擎 JSON-RPC: inline_translate(context='announcement') -----------------


class _Recorder:
    """假 LLM(§15 回放), 与 test_bridge_server.Recorder 同义。"""

    def __init__(self, reply: str | None = None) -> None:
        self.calls: list[str] = []
        self.reply = reply

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        return self.reply if self.reply is not None else CN_REPLY * max(1, round(len(text) / 90))


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


class TestEngineAnnouncementInline:
    def test_inline_translate_announcement_context_cached(self, inline_bridge) -> None:
        """context='announcement' 走同一 inline 段落缓存: 二次请求 cached=True。"""
        server, recorder = inline_bridge
        client = _JsonLineClient("127.0.0.1", server.bound_port)
        try:
            r1 = client.call("inline_translate", {
                "text": "Dwarf", "context": "announcement", "event_id": "report-1",
            })["result"]
            assert r1["status"] == "done" and r1["cached"] is False
            assert r1["translated_text"] == "矮人"
            r2 = client.call("inline_translate", {
                "text": "Dwarf", "context": "announcement", "event_id": "report-2",
            })["result"]
            assert r2["status"] == "done" and r2["cached"] is True
            assert r2["translated_text"] == "矮人"
            assert len(recorder.calls) == 0, "词典快路径不应调 LLM"
        finally:
            client.close()

    def test_announcement_context_isolated_from_textviewer(self, inline_bridge) -> None:
        """同一文本重复公告(同 context)第二次不再调 LLM(段落缓存命中)。"""
        server, recorder = inline_bridge
        client = _JsonLineClient("127.0.0.1", server.bound_port)
        try:
            text = "A stray rooster has wandered into the fortress."
            client.call("inline_translate", {
                "text": text, "context": "announcement", "event_id": "report-1",
            })
            calls_after_first = len(recorder.calls)
            assert calls_after_first == 1, f"首请求未命中应调 LLM 一次: {recorder.calls}"
            r2 = client.call("inline_translate", {
                "text": text, "context": "announcement", "event_id": "report-2",
            })["result"]
            assert r2["cached"] is True, "同 context 重复文本应命中段落缓存"
            assert len(recorder.calls) == calls_after_first, "缓存命中不应再调 LLM"
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
class TestLuaAnnouncementInline:
    def test_widget_registered(self, tmp_path) -> None:
        out = _run_lua(
            "M.load({}, {})\n"
            "local w = M.env.OVERLAY_WIDGETS\n"
            "assert(type(w) == 'table', 'OVERLAY_WIDGETS 缺失')\n"
            "assert(w.announcement, 'announcement widget 未注册')\n"
            "assert(w.textviewer, 'textviewer widget 不应丢失')\n"
            "assert(M.env.AnnouncementInline, 'AnnouncementInline 类未定义')\n"
            "local vs = M.env.AnnouncementInline.ATTRS.viewscreens\n"
            "local has_d, has_a = false, false\n"
            "for _, n in ipairs(vs) do\n"
            "  if n == 'dwarfmode' then has_d = true end\n"
            "  if n == 'adventur' then has_a = true end\n"
            "end\n"
            "assert(has_d and has_a, 'viewscreens 应含 dwarfmode+adventur')\n"
            "assert(M.env.AnnouncementInline.ATTRS.default_enabled == true, '默认开')\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r}"
        assert out.stdout.strip() == "OK"

    def test_roundtrip_paint_nonempty(self, tmp_path) -> None:
        """捕获公告 → 引擎应答 → S.ann_cache → onRenderFrame(dc,40,10) 非空。"""
        out = _run_lua(
            "M.engine_up = true\n"
            # 图集装到 textviewer-font/(014 复用 013 图集), 含译文全部码点
            "M.install_font({['page-000.png'] = {0x516C, 0x9E21, 0x8D70, 0x8FDB, 0x4E86,"
            " 0x8981, 0x585E, 0x3002}}, 8, 12, 'textviewer-font', 2)\n"
            "M.add_report(101, 'A stray rooster has wandered into the fortress.')\n"
            "M.load({}, {enable = true})\n"
            "M.emit_report(101)\n"
            "M.step(36)\n"
            "M.engine_auto_respond('公鸡走进了要塞。', 0.9)\n"
            "M.step(24)\n"
            "local st = M.state()\n"
            "local rec = st.ann_cache['report-101']\n"
            "assert(rec and rec.text == '公鸡走进了要塞。', 'ann_cache 应有译文')\n"
            "assert(st.ann_on == true, 'ann_on')\n"
            "local dc = M.make_dc()\n"
            "local w = M.env.AnnouncementInline({})\n"
            "local painted = w:onRenderFrame(dc, 40, 10)\n"
            "assert(painted and painted > 0, 'onRenderFrame 应绘制非空: ' .. tostring(painted))\n"
            "assert(#dc.tiles > 0, 'dc.tiles 非空')\n"
            "-- paint 触发字形图集懒加载(与 013 共用 S.tv_font)\n"
            "assert(st.tv_font.installed, '图集应已装载')\n"
            "assert(st.tv_font.scale == 2, 'scale 应为 2(复用 013 图集)')\n"
            "assert(#M.texture_loads >= 1, 'loadTileset 应被调用')\n"
            "io.stdout:write('PAINTED=' .. tostring(painted))\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r} stdout={out.stdout!r}"
        assert out.stdout.startswith("PAINTED=")

    def test_cache_get_set_expire(self, tmp_path) -> None:
        out = _run_lua(
            "M.load({}, {})\n"
            "local env = M.env\n"
            "assert(M.state().config.ann_ttl_ms == 90000, '默认 TTL 应为 90s')\n"
            "assert(env.fanyi_ann_cache_set('report-1', '天气放晴了。'), 'set 失败')\n"
            "local rec = env.fanyi_ann_cache_get('report-1')\n"
            "assert(rec and rec.text == '天气放晴了。', 'get 失败')\n"
            "local ts = tonumber(rec.ts)\n"
            "assert(env.fanyi_ann_cache_gc(ts + 89999) == 0, 'TTL 内不应清除')\n"
            "assert(env.fanyi_ann_cache_get('report-1'), 'TTL 内应保留')\n"
            "assert(env.fanyi_ann_cache_gc(ts + 90001) == 1, '超 TTL 应清除 1 条')\n"
            "assert(env.fanyi_ann_cache_get('report-1') == nil, '清除后 get 应为 nil')\n"
            "-- 空/非法参数拒绝\n"
            "assert(env.fanyi_ann_cache_set(nil, 'x') == false)\n"
            "assert(env.fanyi_ann_cache_set('report-2', '') == false)\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r}"
        assert out.stdout.strip() == "OK"

    def test_ann_build_rows_latest_first_bounded(self, tmp_path) -> None:
        """多公告显示策略: 最新在前占满, 历史每条 1 行(缩短), 总行数有界。"""
        out = _run_lua(
            "M.load({}, {})\n"
            "local env = M.env\n"
            "env.fanyi_ann_cache_set('report-1', '第一条公告内容')\n"
            "env.fanyi_ann_cache_set('report-2', '第二条公告内容')\n"
            "env.fanyi_ann_cache_set('report-3', '第三条公告内容')\n"
            "M.state().ann_cache['report-1'].ts = 100\n"
            "M.state().ann_cache['report-2'].ts = 200\n"
            "M.state().ann_cache['report-3'].ts = 300\n"
            "local ents = env.fanyi_ann_visible_entries()\n"
            "assert(#ents == 3, '应取 3 条')\n"
            "assert(ents[1].text == '第三条公告内容' and ents[3].text == '第一条公告内容',\n"
            "       '应按时间倒序(最新在前)')\n"
            "local rows = env.fanyi_ann_build_rows(ents, 20, 5)\n"
            "assert(rows[1] == '第三条公告内容', '最新应在首行')\n"
            "assert(#rows == 3, '3 条公告各 1 行: ' .. #rows)\n"
            "-- 有界: vis_rows=2 截断且保留最新\n"
            "local rows2 = env.fanyi_ann_build_rows(ents, 20, 2)\n"
            "assert(#rows2 == 2 and rows2[1] == '第三条公告内容'\n"
            "       and rows2[2] == '第二条公告内容', '截断应保留最新')\n"
            "-- 最新公告长文最多 4 行, 历史只留 1 行\n"
            "M.state().ann_cache['report-3'].text = string.rep('长', 60)\n"
            "local rows3 = env.fanyi_ann_build_rows(env.fanyi_ann_visible_entries(), 20, 6)\n"
            "assert(#rows3 <= 6, '总行数有界')\n"
            "local n_long = 0\n"
            "for _, r in ipairs(rows3) do if #r == 20 then n_long = n_long + 1 end end\n"
            "assert(n_long <= 4, '最新公告最多占 4 行: ' .. n_long)\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r}"
        assert out.stdout.strip() == "OK"

    def test_overlays_toggle_and_status(self, tmp_path) -> None:
        out = _run_lua(
            "M.load({}, {})\n"
            "assert(M.state().ann_on == true, '初始应 on')\n"
            "M.load({'overlays', 'off', 'announcement'}, {})\n"
            "assert(M.state().ann_on == false, 'off 后应 false')\n"
            "local disabled = false\n"
            "for _, c in ipairs(M.run_commands) do\n"
            "  if c[1] == 'overlay' and c[2] == 'disable' and c[3] == 'fanyi.announcement' then disabled = true end\n"
            "end\n"
            "assert(disabled, '应执行 overlay disable fanyi.announcement')\n"
            "M.output = {}\n"
            "M.load({'status'}, {})\n"
            "assert(M.output_joined():find('announcement_inline %(off%)'),\n"
            "       'status 应含 announcement_inline (off)')\n"
            "M.load({'overlays', 'on', 'announcement'}, {})\n"
            "assert(M.state().ann_on == true, 'on 后应 true')\n"
            "local enabled = false\n"
            "for _, c in ipairs(M.run_commands) do\n"
            "  if c[1] == 'overlay' and c[2] == 'enable' and c[3] == 'fanyi.announcement' then enabled = true end\n"
            "end\n"
            "assert(enabled, '应执行 overlay enable fanyi.announcement')\n"
            "M.output = {}\n"
            "M.load({'status'}, {})\n"
            "assert(M.output_joined():find('announcement_inline %(on%)'),\n"
            "       'status 应含 announcement_inline (on)')\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r}"
        assert out.stdout.strip() == "OK"

    def test_inline_route_context_announcement_no_double_send(self, tmp_path) -> None:
        """inline 路由: context='announcement'; 队列已覆盖(S.sent)的不双发; 同步 done 入缓存。"""
        out = _run_lua(
            "M.engine_up = true\n"
            "M.load({}, {})\n"
            "local st = M.state()\n"
            "st.ann_want_inline['report-777'] = {text = 'The weather has cleared.'}\n"
            "M.load({'start'}, {})\n"
            "M.step(24)\n"
            "local found = 0\n"
            "for _, l in ipairs(M.sent_lines) do\n"
            "  if l:find('inline_translate', 1, true) and l:find('\"context\":\"announcement\"')\n"
            "     and l:find('report-777', 1, true) then found = found + 1 end\n"
            "end\n"
            "assert(found == 1, '应恰好发出 1 条 inline_translate(context=announcement): ' .. found)\n"
            "assert(st.ann_inline_sent['report-777'], 'ann_inline_sent 应登记')\n"
            "-- 常规队列已覆盖(S.sent 已登记)的不双发(预算铁律)\n"
            "st.ann_want_inline['report-101'] = {text = 'already queued'}\n"
            "st.sent['report-101'] = 'already queued'\n"
            "M.step(24)\n"
            "for _, l in ipairs(M.sent_lines) do\n"
            "  assert(not (l:find('inline_translate', 1, true) and l:find('report-101', 1, true)),\n"
            "         '已入队事件不应再走 inline 双发')\n"
            "end\n"
            "-- 引擎同步应答(done) → 公告缓存\n"
            "M.server_respond({jsonrpc='2.0', id='inline_translate-1', result={status='done',\n"
            "  event_id='report-777', translated_text='天气放晴了。', confidence=0.9,\n"
            "  model='fake', provider='fake'}})\n"
            "M.step(24)\n"
            "local rec = M.state().ann_cache['report-777']\n"
            "assert(rec and rec.text == '天气放晴了。', '同步 done 应入公告缓存')\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r} stdout={out.stdout!r}"
        assert out.stdout.strip() == "OK"

    def test_paint_off_or_empty_silent(self, tmp_path) -> None:
        """门控关闭/缓存空 → 返回 0(§2.3 静默原文)。"""
        out = _run_lua(
            "M.load({}, {})\n"
            "local env = M.env\n"
            "local dc = M.make_dc()\n"
            "local w = M.env.AnnouncementInline({})\n"
            "assert(w:onRenderFrame(dc, 80, 25) == 0, '缓存空应返回 0')\n"
            "env.fanyi_ann_cache_set('report-1', '有译文但开关已关')\n"
            "M.state().ann_on = false\n"
            "assert(w:onRenderFrame(M.make_dc(), 80, 25) == 0, '关闭后应返回 0')\n"
            "io.stdout:write('OK')\n",
            tmp_path,
        )
        assert out.returncode == 0, f"stderr={out.stderr!r}"
        assert out.stdout.strip() == "OK"
