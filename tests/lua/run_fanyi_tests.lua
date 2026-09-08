-- 逐场景运行 fanyi.lua 无头测试。由 pytest tests/test_bridge_lua.py 以子进程调用:
--   lua5.4 run_fanyi_tests.lua <scenario> <repo_root> <tmpdir>
-- 退出码 0=通过; 1=失败(输出诊断)。
local scenario = assert(arg[1], 'scenario required')
local repo = assert(arg[2], 'repo_root required')
local tmpdir = assert(arg[3], 'tmpdir required')

-- 装载 harness(dofile 不带 varargs → 手动 loadfile+call 传入脚本路径与临时目录)
local hpath = repo .. '/tests/lua/fanyi_harness.lua'
local hchunk = assert(loadfile(hpath))
local M = hchunk(repo .. '/dfhack/scripts/fanyi.lua', tmpdir)

local function assert_eq(got, want, what)
    if got ~= want then
        error(('%s: expected %q, got %q'):format(what, tostring(want), tostring(got)), 2)
    end
end

local function assert_match(got, pat, what)
    if not tostring(got):match(pat) then
        error(('%s: %q does not match %q'):format(what, tostring(got), pat), 2)
    end
end

local ok, err = pcall(function()

-- ============ 场景 1: 版本守卫 → Safe Mode ============
if scenario == 'safe_mode' then
    M.set_version('52.01', '52.01-r1')
    M.load({'status'}, {})
    local out = M.output_joined()
    assert_match(out, 'SAFE MODE', 'out')
    assert_match(out, '52%.01', 'out')
    -- start 被拒
    M.output = {}
    M.load({'start'}, {})
    assert_match(M.output_joined(), 'Safemode', 'start refused')

-- ============ 场景 2: 引擎离线 → 静默原文 + 退避重连(不崩溃) ============
elseif scenario == 'engine_down' then
    M.engine_up = false
    M.load({}, {enable = true})          -- 自启捕获+轮询
    M.add_report(7, 'Urist cancels Craft Barrel.')
    M.emit_report(7)
    M.step(500)
    local st = M.state()
    assert_eq(st.stats.captured >= 1, true, 'captured')
    assert_eq(st.engine_online, false, 'engine_online')
    assert_eq(#M.sent_lines, 0, 'sent')
    assert_eq(#st.render_lines, 0, 'render_lines')
    -- 定时器链仍在(自愈未死)
    assert_eq(#M.frames > 0, true, 'timer chain alive')

-- ============ 场景 3: 引擎在线 → 捕获→发送→done→渲染 ============
elseif scenario == 'roundtrip' then
    M.engine_up = true
    M.load({}, {enable = true})
    M.add_report(7, 'A goblin trampled the straw beasts.')
    M.emit_report(7)
    M.step(60)                            -- 帧步进到连上并发送
    local st = M.state()
    assert_eq(st.engine_online, true, 'engine_online')
    -- 应答全部在途请求(translate + fetch_done)
    M.engine_auto_respond('一只哥布林踩踏了稻草兽。', 0.9)
    assert_eq(#M.events_out >= 1, true, 'engine received events')
    local ev = M.events_out[1]
    assert_eq(ev.event_id, 'report-7', 'event_id')
    assert_eq(ev.source_text, 'A goblin trampled the straw beasts.', 'source_text')
    assert_eq(ev.priority, 80, 'priority')
    assert_eq(ev.text_type, 'ANNOUNCEMENT', 'text_type')
    assert_eq(ev.source_hash ~= '', true, 'source_hash')
    M.step(40)
    st = M.state()
    assert_eq(st.stats.done >= 1, true, 'done>=1')
    assert_eq(#st.render_lines >= 1, true, 'render_lines')
    -- 渲染载荷由状态中的 render_lines 直接断言(与 overlay 解耦)
    local last = st.render_lines[#st.render_lines]
    assert_eq(last.text, '一只哥布林踩踏了稻草兽。', 'rendered text')

-- ============ 场景 4: confidence=0 → 不渲染(静默原文) ============
elseif scenario == 'zero_confidence' then
    M.engine_up = true
    M.load({}, {enable = true})
    M.add_report(3, 'The cavern is silent.')
    M.emit_report(3)
    M.step(60)
    M.engine_auto_respond('洞穴一片寂静。', 0.0)
    M.step(40)
    local st = M.state()
    assert_eq(st.stats.done >= 1, true, 'done>=1')
    assert_eq(#st.render_lines, 0, 'no render when confidence=0')

-- ============ 场景 5: 引擎宕机 → 断线自愈 → 恢复重连 ============
elseif scenario == 'reconnect' then
    M.engine_up = true
    M.load({}, {enable = true})
    M.add_report(11, 'The miners strike iron.')
    M.emit_report(11)
    M.step(60)
    local st = M.state()
    assert_eq(st.engine_online, true, 'online')
    M.engine_up = false
    M.step(60)                            -- 发送失败 → 断线
    st = M.state()
    assert_eq(st.engine_online, false, 'offline after kill')
    assert_eq(#st.sent, 0, 'inflight cleared')
    M.output = {}
    M.load({'status'}, {})
    assert_match(M.output_joined(), '离线', 'status offline')
    -- 引擎恢复 → 退避后重连
    M.engine_up = true
    M.step(3000)
    st = M.state()
    assert_eq(st.engine_online, true, 'reconnected')
    assert_eq(st.stats.reconnect >= 2, true, 'reconnect counter')

-- ============ 场景 6: gamelog 历史回溯 ============
elseif scenario == 'gamelog_history' then
    local f = assert(io.open(tmpdir .. '/gamelog.txt', 'w'))
    f:write('seed history line\n')
    f:close()
    M.engine_up = true
    M.load({}, {enable = true})
    -- 阶段 1: 首次 tail(tick 30 @帧360)只设置偏移, 不重放 seed
    M.step(400)
    local st = M.state()
    assert_eq(st.stats.captured, 0, 'no full re-harvest of pre-existing history')
    -- 阶段 2: 追加两行 → 下一个 tail(tick 60 @帧720)捕获新行
    local f2 = assert(io.open(tmpdir .. '/gamelog.txt', 'a'))
    f2:write('Found granite.\nThe aquifer is gone.\n')
    f2:close()
    M.step(800)
    st = M.state()
    assert_eq(st.stats.captured >= 2, true, 'captured from gamelog')
    assert_eq(#M.sent_lines >= 1, true, 'sent from gamelog')
    -- 阶段 3: 再追加一行 → 只捕获新行(不全量重放)
    local f3 = assert(io.open(tmpdir .. '/gamelog.txt', 'a'))
    f3:write('Gems found.\n')
    f3:close()
    local before = st.stats.captured
    M.step(800)
    st = M.state()
    assert_eq(st.stats.captured >= before + 1, true, 'appended line captured')
    assert_eq(st.stats.captured > before + 5, false, 'no full re-harvest')

-- ============ 场景 7: overlays 命令与 CJK 门控(ticket-009: 图集就绪才放行) ============
elseif scenario == 'overlays_cmd' then
    M.engine_up = true
    M.load({}, {enable = true})
    M.load({'overlays', 'on'}, {})
    assert_match(M.output_joined(), '已开启', 'overlays on')
    -- 未装图集时 overlays on cjk 必须被拒(静默原文门控)
    M.output = {}
    M.load({'overlays', 'on', 'cjk'}, {})
    assert_match(M.output_joined(), '不可用', 'cjk refused without atlas')
    assert_eq(M.state().render_cjk, false, 'cjk off without atlas')
    -- 装图集后放行: 注册纹理页 + 持久启用 overlay 小部件
    M.install_font({['page-000.png'] = {39118, 22768}})  -- 风, 声
    M.output = {}
    M.load({'overlays', 'on', 'cjk'}, {})
    assert_match(M.output_joined(), '贴图就绪', 'cjk enabled with atlas')
    local st = M.state()
    assert_eq(st.render_cjk, true, 'cjk enabled')
    assert_eq(st.font.installed, true, 'font installed')
    assert_eq(#M.texture_loads, 1, 'loadTileset called once')
    assert_eq(M.texture_loads[1].tile_w, 8, 'atlas tile_w')
    assert_eq(M.texture_loads[1].tile_h, 12, 'atlas tile_h')
    assert_eq(M.rescan_calls >= 1, true, 'overlay.rescan called')
    local found_enable = false
    for _, argv in ipairs(M.run_commands) do
        if argv[1] == 'overlay' and argv[2] == 'enable' and argv[3] == 'fanyi.subtitle' then
            found_enable = true
        end
    end
    assert_eq(found_enable, true, 'overlay enable fanyi.subtitle issued')
    -- 端到端: 已有译文应能渲染
    M.load({'clear'}, {})
    M.add_report(2, 'The wind howls.')
    M.emit_report(2)
    M.step(60)
    M.engine_auto_respond('风声呼啸。', 0.9)
    M.step(60)
    local last = M.state().render_lines[#M.state().render_lines]
    assert_eq(last.text, '风声呼啸。', 'rendered after cjk')

-- ============ 场景 8: overlay 字幕条贴图(载荷 → texpos 逐字绘制) ============
elseif scenario == 'cjk_render_tiles' then
    -- "风声呼啸。" → cp 表(页内顺序即 texpos 顺序)
    M.install_font({['page-000.png'] = {39118, 22768, 21628, 21880, 12290}})
    M.engine_up = true
    M.load({}, {enable = true})
    local st = M.state()
    assert_eq(M.env.OVERLAY_WIDGETS ~= nil and M.env.OVERLAY_WIDGETS.subtitle ~= nil,
              true, 'OVERLAY_WIDGETS.subtitle registered')
    M.load({'overlays', 'on', 'cjk'}, {})
    M.add_report(5, 'The wind howls.')
    M.emit_report(5)
    M.step(60)
    M.engine_auto_respond('风声呼啸。', 0.9)
    M.step(60)
    st = M.state()
    assert_eq(#st.render_lines, 1, 'one render line')
    -- 实例化 widget(overlay 框架路径)并直接驱动 onRenderBody(渲染回调内才会画)
    local widget = M.env.OVERLAY_WIDGETS.subtitle{name = 'fanyi.subtitle'}
    assert_eq(widget.frame.w, 60, 'frame.w default')
    assert_eq(widget.frame.h, 8, 'frame.h default')
    local dc = M.make_dc()
    widget:onRenderBody(dc)
    assert_eq(#dc.tiles, 5, 'painted one tile per codepoint')
    -- 底部对齐: 单行 → y = frame.h-1 = 7; x 逐格递增
    for i, t in ipairs(dc.tiles) do
        assert_eq(t.x, i - 1, 'tile x order')
        assert_eq(t.y, 7, 'tile bottom row')
    end
    -- texpos 确定性: 第一页 base=100, cp 在页内下标 i(1 基) → texpos = 500000+100+i
    local want = {500101, 500102, 500103, 500104, 500105}
    for i, t in ipairs(dc.tiles) do
        assert_eq(t.texpos, want[i], 'texpos for glyph ' .. i)
    end
    -- 门控: 关闭后不画任何像素(§2.3 静默原文)
    M.load({'overlays', 'off'}, {})
    dc = M.make_dc()
    widget:onRenderBody(dc)
    assert_eq(#dc.tiles, 0, 'no paint when overlays off')

-- ============ 场景 9: 字形缺失 → 跳格不崩(未知 cp / 拒绝门控) ============
elseif scenario == 'cjk_missing_glyph' then
    M.install_font({['page-000.png'] = {39118}})  -- 只装"风"
    M.engine_up = true
    M.load({}, {enable = true})
    M.load({'overlays', 'on', 'cjk'}, {})
    M.add_report(6, 'The wind howls.')
    M.emit_report(6)
    M.step(60)
    -- 译文含未装字形(声/呼/啨/。): 贴图只画"风", 其余跳格
    M.engine_auto_respond('风起。', 0.9)
    M.step(60)
    local widget = M.env.OVERLAY_WIDGETS.subtitle{name = 'fanyi.subtitle'}
    local dc = M.make_dc()
    widget:onRenderBody(dc)
    assert_eq(#dc.tiles, 1, 'only installed glyph painted')
    assert_eq(dc.tiles[1].x, 0, 'wind at cell 0')
    -- UTF-8 解码器: 合法/非法序列
    local cps = M.env.fanyi_utf8_codepoints('风A\xffz')
    assert_eq(cps[1], 39118, 'utf8 3-byte')
    assert_eq(cps[2], 65, 'utf8 ascii')
    assert_eq(cps[3], 0xFFFD, 'utf8 invalid → U+FFFD')

else
    error('unknown scenario: ' .. scenario)
end

end)

if not ok then
    io.stderr:write('FAIL [' .. scenario .. ']: ' .. tostring(err) .. '\n')
    io.stderr:write('--- output ---\n' .. M.output_joined() .. '\n')
    io.stderr:write('--- events ---\n')
    for i, ev in ipairs(M.events_out or {}) do
        io.stderr:write(('%d: %s | %s | p=%s\n'):format(i, ev.event_id, ev.source_text, tostring(ev.priority)))
    end
    io.stderr:write('--- state ---\n')
    local st = M.state()
    if st then
        io.stderr:write(('state=%s online=%s tick=%d captured=%d sent=%d recv=%d done=%d failed=%d reconnect=%d render=%d retry_after=%d\n')
            :format(st.state, tostring(st.engine_online), st.tick, st.stats and st.stats.captured or 0,
                     st.stats and st.stats.sent or 0, st.stats and st.stats.recv or 0,
                     st.stats and st.stats.done or 0, st.stats and st.stats.failed or 0,
                     st.stats and st.stats.reconnect or 0, #st.render_lines, st.retry_after))
    end
    os.exit(1)
end
print('PASS [' .. scenario .. ']')
os.exit(0)