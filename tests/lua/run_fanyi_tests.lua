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

-- ============ 场景 7: overlays 命令与 CJK 门控 ============
elseif scenario == 'overlays_cmd' then
    M.engine_up = true
    M.load({}, {enable = true})
    M.load({'overlays', 'on'}, {})
    assert_match(M.output_joined(), '已开启', 'overlays on')
    -- 无字体 → 渲染载荷为空(静默原文)
    local st = M.state()
    M.reports[1] = {text='x'}
    -- 渲染门控: render_cjk=false 时即使有译文也不输出
    M.add_report(1, 'Do you know where we are?')
    M.emit_report(1)
    M.step(60)
    M.engine_auto_respond('你知道我们在哪儿吗？', 0.95)
    M.step(60)
    st = M.state()
    assert_eq(st.render_cjk, false, 'cjk off by default')
    -- 开启 cjk 后渲染载荷含译文
    M.load({'overlays', 'on', 'cjk'}, {})
    assert_eq(M.state().render_cjk, true, 'cjk enabled')
    -- overlap: 已有译文应能渲染
    M.load({'clear'}, {})
    M.add_report(2, 'The wind howls.')
    M.emit_report(2)
    M.step(60)
    M.engine_auto_respond('风声呼啸。', 0.9)
    M.step(60)
    local last = M.state().render_lines[#M.state().render_lines]
    assert_eq(last.text, '风声呼啸。', 'rendered after cjk')

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