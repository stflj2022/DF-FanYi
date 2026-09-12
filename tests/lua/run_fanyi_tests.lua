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
    -- ticket-012: 字幕条已删除, 无 render_lines 状态; 仅验证捕获与静默
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
    -- ticket-012: 译文不再进 render_lines(字幕条已删), 落 S.displayed
    -- (event_id → true), 供 ticket-013/014 inline overlay 消费
    assert_eq(st.displayed['report-7'], true, 'displayed[report-7]')

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
    assert_eq(st.displayed['report-3'], nil, 'no display when confidence=0')

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

-- ============ 场景 7: overlays 命令新语法(ticket-012: 字幕条已删) ============
elseif scenario == 'overlays_cmd' then
    M.engine_up = true
    M.load({}, {enable = true})
    -- 旧语法 cjk → 明确告知字幕条已删除, 不加载图集
    M.load({'overlays', 'on'}, {})
    M.output = {}
    M.load({'overlays', 'on', 'cjk'}, {})
    assert_match(M.output_joined(), '已删除', 'cjk alias → removed notice')
    assert_eq(#M.texture_loads, 0, 'no atlas load (subtitle removed)')
    -- 新语法 textviewer/announcement → 提示等待 ticket-013/014
    M.output = {}
    M.load({'overlays', 'on', 'textviewer'}, {})
    assert_match(M.output_joined(), 'ticket', 'textviewer pending notice')
    M.output = {}
    M.load({'overlays', 'on', 'announcement'}, {})
    assert_match(M.output_joined(), 'ticket', 'announcement pending notice')
    -- off: cjk 告知无需关闭
    M.output = {}
    M.load({'overlays', 'off', 'cjk'}, {})
    assert_match(M.output_joined(), '已删除', 'off cjk → removed notice')
    -- OVERLAY_WIDGETS 不再含 subtitle 键(ticket-012 验收)
    assert_eq(M.env.OVERLAY_WIDGETS == nil or M.env.OVERLAY_WIDGETS.subtitle == nil,
              true, 'OVERLAY_WIDGETS.subtitle gone')
    -- fanyi_render_inline_payload() 保留签名返回空表(ticket-013/014 契约)
    local payload = M.env.fanyi_render_inline_payload()
    assert_eq(type(payload), 'table', 'inline payload is table')
    assert_eq(#payload, 0, 'inline payload empty')
    -- 端到端: done 仍落 displayed(inline overlay 的数据源)
    M.load({'clear'}, {})
    M.add_report(2, 'The wind howls.')
    M.emit_report(2)
    M.step(60)
    M.engine_auto_respond('风声呼啸。', 0.9)
    M.step(60)
    assert_eq(M.state().displayed['report-2'], true, 'displayed after roundtrip')

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
        io.stderr:write(('state=%s online=%s tick=%d captured=%d sent=%d recv=%d done=%d failed=%d reconnect=%d retry_after=%d\n')
            :format(st.state, tostring(st.engine_online), st.tick, st.stats and st.stats.captured or 0,
                     st.stats and st.stats.sent or 0, st.stats and st.stats.recv or 0,
                     st.stats and st.stats.done or 0, st.stats and st.stats.failed or 0,
                     st.stats and st.stats.reconnect or 0, st.retry_after))
    end
    os.exit(1)
end
print('PASS [' .. scenario .. ']')
os.exit(0)