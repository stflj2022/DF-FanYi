-- DF-FanYi 游戏侧桥(ticket-008): 捕获游戏文本 → loopback TCP → 翻译引擎(JSON-RPC)。
-- 安装在 hack/scripts/fanyi.lua 后, `fanyi` 成为 DFHack 命令(脚本自注册)。
--
-- 职责(工程书 §21-22/§43-44):
--   1. 捕获: 公告(report, eventful.onReport)+ 游戏日志回溯(History, 新行去重);
--   2. 传输: JSON-RPC 2.0 over JSON Lines, loopback TCP
--      (DFHack 自捆绑 luasocket 官方仅 TCP, 见 docs/audits/DFHACK_INTEGRATION.md §5);
--   3. 渲染: overlay 小部件(底部字幕条); 中文经字形图集(hack/data/fanyi-font/)
--      + dfhack.textures 官方 API 逐字贴图(ticket-009, 定论见
--      docs/audits/DFHACK_INTEGRATION.md §9); 图集未安装/门控关闭时不绘制
--      → 游戏显示原文(§2.3 静默降级);
--   4. 版本守卫: DF/DFHack 版本不匹配 → Safe Mode(不捕获/不渲染, 仅诊断 §43-44);
--   5. 断线自愈: 引擎未启动/宕机 → 指数退避重连, 任何时刻不阻塞游戏主循环
--      (所有套接字操作非阻塞 + pcall, §29/§30)。
--
-- 命令: fanyi status|start|stop|clear|debug|overlays on|off [cjk]
--   overlays on cjk: 字体贴图已安装后显式开启 CJK 绘制(ticket-009 落地后)。
--
-- 非标准注: 状态暴露为全局 fanyi_state(诊断/测试用), 渲染载荷由纯函数
-- fanyi_render_lines() 产出(与 overlay 解耦, 可无头测试)。
--@ enable = true

--[====[

fanyi (DF-FanYi bridge)
=======================
:start|enable:        开始捕获(status 等命令本身不需要)
:stop|disable:        停止捕获与轮询
:status:              引擎连接/队列/RPC 统计
:overlays on|off:     开启/关闭译文悬浮(默认关, 见上 CJK 说明)
:overlays on cjk:     在字体贴图就绪后启用 CJK 绘制
:clear:               清空已显示译文与去重集(重玩/新世界)
:debug:               内部状态(待发事件/重连退避/最近错误)
]====]

local JSON = (pcall(require, 'json')) and require('json') or _G.json
local overlay = require('plugins.overlay')
local defclass = defclass  -- DFHack 脚本环境全局(gui.class); 无头 harness 自带同义实现

-- 全局持久状态(脚本命令每次执行都会重跑本文件, 状态必须挂在全局)
fanyi_state = fanyi_state or {}
local S = fanyi_state

S.config = S.config or {
    host = '127.0.0.1',
    port = 17486,           -- 与引擎 config bridge.port 对齐
    df = '53.06',           -- 守卫: 锁定的 DF 版本(整合包 v53.06)
    dfhack = '53.06-r1',    -- 守卫: 锁定的 DFHack 版本前缀
    tick_frames = 12,       -- 主轮询节拍(~0.2s@60fps)
    retry_frames = 90,      -- 重连起始间隔(帧), 指数退避 ×2 至 900
    max_send_per_tick = 3,  -- 每节拍最多发送的待发事件数
}
local C = S.config

S.state = S.state or 'init'       -- init|running|stopped|safe
S.engine_online = S.engine_online or false
S.safe_mode = S.safe_mode or false
S.safe_reason = S.safe_reason or ''
S.client = S.client or nil        -- luasocket client
S.pending = S.pending or {}       -- 待发 TextEvent 队列
S.sent = S.sent or {}             -- 已发在途 {event_id=source_text}(仅排摸用)
S.inflight = S.inflight or 0       -- 在途计数(哈希表不能用 #)
S.displayed = S.displayed or {}   -- 已渲染译文 {event_id=true}(去重)
S.recent_reports = S.recent_reports or {}  -- 最近 report id 去重(环形)
S.recent_report_i = S.recent_report_i or 0
S.log_offset = S.log_offset or nil
S.retry_after = S.retry_after or 0
S.timer_euid = S.timer_euid or nil
S.tick = S.tick or 0
S.stats = S.stats or {captured=0, sent=0, recv=0, done=0, failed=0, reconnect=0}
S.readbuf = S.readbuf or ''       -- 行拆解缓冲(响应聚合)
S.render_lines = S.render_lines or {}   -- 准备绘制的 {text, confidence}
S.overlays_on = S.overlays_on or false
S.render_cjk = S.render_cjk or false
S.font = S.font or {installed = false, tile_w = 8, tile_h = 12, by_cp = {}}
S.last_error = S.last_error or ''
S.gamelog = S.gamelog or {enabled=true, path=nil}
S.seen_tv_hashes = S.seen_tv_hashes or {}  -- textviewer 弹窗内容去重(哈希)
S.debug_log = S.debug_log or {path = nil}  -- 轻量文件日志(游戏目录 fanyi-debug.log)

-- 版本守卫 §43-44 -----------------------------------------------------------

function fanyi_check_version()
    local dfver = dfhack.getDFVersion and dfhack.getDFVersion() or '?'
    local dhver = dfhack.getDFHackVersion and dfhack.getDFHackVersion() or '?'
    -- getDFVersion() 返回形如 "v0.53.06 win64 STEAM"(含前缀/平台), 用包容匹配
    local df_ok = dfver:find(C.df, 1, true) ~= nil
    local dh_ok = dhver:sub(1, #C.dfhack) == C.dfhack
    if not (df_ok and dh_ok) then
        S.safe_mode = true
        S.safe_reason = ('版本不匹配: DF=%s (expect %s), DFHack=%s (expect %s^)'):format(
            dfver, C.df, dhver, C.dfhack)
        S.state = 'safe'
        return false
    end
    S.safe_mode = false
    -- 每次命令都会重跑本脚本: 不能把运行中状态重置掉(否则 status 会停桥)
    if S.state == 'init' then
        S.state = 'stopped'
    end
    return true
end

-- 捕获: 公告(report) ---------------------------------------------------------

local function now_ms()
    local t = dfhack.getTickCount and dfhack.getTickCount() or 0
    return tostring(t)
end

local function fnv1a(s)
    local h = 2166136261
    for i = 1, #s do
        local b = s:byte(i)
        h = (h ~ b) * 16777619 % 4294967296
    end
    return tostring(h)
end

-- 轻量文件日志: 游戏侧无 dfhack-run 控制台, 关键事件落盘便于远程排障。
-- 量级: 仅 start/textviewer捕获/断连, 每会话几十行, 不做轮转。
local function flog(msg)
    pcall(function()
        if not S.debug_log.path then
            local base = (dfhack.getDFPath and dfhack.getDFPath()) or '.'
            S.debug_log.path = base .. '/fanyi-debug.log'
        end
        local fh = io.open(S.debug_log.path, 'a')
        if not fh then return end
        fh:write(os.date('%Y-%m-%d %H:%M:%S '), msg, '\n')
        fh:close()
    end)
end

local function push_event(event)
    -- 去重: 同一 event_id 可能同时被 eventful.onReport 和容器轮询捕到
    for _, ev in ipairs(S.pending) do
        if ev.event_id == event.event_id then return end
    end
    if S.sent[event.event_id] ~= nil then return end
    table.insert(S.pending, event)
    S.stats.captured = S.stats.captured + 1
    if #S.pending > 512 then table.remove(S.pending, 1) end
end

-- HH:DFHack 官方事件模式: eventful.enableEvent(...REPORT) + onReport.<名字>(id)
local EV
local function ensure_capture()
    if not EV then
        EV = require('plugins.eventful')
        if not S.eventful_hooked then
            EV.onReport.fanyi = function(report_id)
                local id = tonumber(report_id)
                if not id then return end
                -- report 环形去重(游戏内部可能重复回调)
                local k = id % 64
                if S.recent_reports[k] == id then return end
                S.recent_reports[k] = id
                S.recent_report_i = (S.recent_report_i + 1) % 64
                local rep = df and df.report and df.report.find(id)
                local text = rep and rep.text or nil
                if not text or #text == 0 then return end
                push_event({
                    event_id = 'report-' .. id,
                    timestamp = now_ms(),
                    screen = 'announcement',
                    source_text = dfhack.df2console(text),  -- CP437→UTF-8
                    text_type = 'ANNOUNCEMENT',
                    priority = 80,          -- §6 公告档
                    context_id = 'report-' .. id,
                    markup = {},
                    variables = {},
                    source_hash = fnv1a(text),
                    game_version = dfhack.getDFVersion(),
                })
            end
            EV.enableEvent(EV.eventType.REPORT, 1)
            S.eventful_hooked = true
        end
    end
end

-- 53.06 双容器轮询捕获(reports + announcements) --------------------------------
-- 实测: v53.06 的欢迎/公告走 world.status.announcements, eventful.onReport 只盯
-- world.status.reports(且 report 短命, 过期即清), 导致捕获=0。改为每 N tick
-- 直接 diff 两个容器的新增 id, 不依赖 eventful 事件机制。
S.seen_container_ids = S.seen_container_ids or {}

local function capture_from_containers()
    if not (df and df.global and df.global.world) then return end
    local ok, st = pcall(function() return df.global.world.status end)
    if not ok or not st then return end
    for _, listname in ipairs({'reports', 'announcements'}) do
        local ok2, list = pcall(function() return st[listname] end)
        if ok2 and list then
            for i = 0, #list - 1 do
                local rep = list[i]
                if rep and rep.id and rep.text and #rep.text > 0
                   and not S.seen_container_ids[rep.id] then
                    S.seen_container_ids[rep.id] = true
                    -- 有界: 条目太多时丢弃最老的(防长期游玩爆内存)
                    local keys = {}
                    for k in pairs(S.seen_container_ids) do keys[#keys + 1] = k end
                    if #keys > 2048 then
                        for j = 1, #keys - 1024 do S.seen_container_ids[keys[j]] = nil end
                    end
                    push_event({
                        event_id = 'report-' .. rep.id,
                        timestamp = now_ms(),
                        screen = 'announcement',
                        source_text = dfhack.df2console(rep.text),
                        text_type = 'ANNOUNCEMENT',
                        priority = 80,
                        context_id = 'report-' .. rep.id,
                        markup = {},
                        variables = {},
                        source_hash = fnv1a(rep.text),
                        game_version = dfhack.getDFVersion(),
                    })
                end
            end
        end
    end
end

-- 捕获: textviewer 文本弹窗(欢迎向导/教程/帮助'?'整页文本) ------------------
-- v53 新 UI 对长段落按单词逐次 top_addst 渲染 → 词典整段键永远不被查询,
-- 词级匹配只能产出词堆(dfint 日志可见 to/guide/your 逐词命中)。
-- 动态层接管: 读 viewscreen_textviewerst 完整文本 → 引擎整句翻译 → 字幕条显示。
local function capture_textviewer()
    local ok, vs = pcall(dfhack.gui.getCurViewscreen)
    if not ok or not vs then return end
    local cur = vs
    while cur do
        local okc, is_tv = pcall(function()
            return df.viewscreen_textviewerst ~= nil
               and df.viewscreen_textviewerst:is_instance(cur)
        end)
        if okc and is_tv then
            local ok_b, full = pcall(function()
                local parts = {}
                local title = tostring(cur.title or '')
                if #title > 0 then parts[#parts + 1] = title end
                for i = 0, #cur.text - 1 do
                    local ln = tostring(cur.text[i] or '')
                    if #ln > 0 then parts[#parts + 1] = ln end
                end
                return table.concat(parts, '\n')
            end)
            if ok_b and full and #full >= 10 then
                local h = fnv1a(full)
                if not S.seen_tv_hashes[h] then
                    S.seen_tv_hashes[h] = true
                    -- 有界: 哈希表膨胀时丢弃一半
                    local keys = {}
                    for k in pairs(S.seen_tv_hashes) do keys[#keys + 1] = k end
                    if #keys > 512 then
                        for j = 1, #keys - 256 do S.seen_tv_hashes[keys[j]] = nil end
                    end
                    flog(('捕获textviewer: %d字符 hash=%s'):format(#full, h:sub(1, 10)))
                    push_event({
                        event_id = 'tv-' .. h,       -- 稳定 id: 引擎侧天然幂等
                        timestamp = now_ms(),
                        screen = 'textviewer',
                        source_text = full,
                        text_type = 'TEXTVIEWER',
                        priority = 85,               -- 用户正在读的弹窗, 高于公告(80)
                        context_id = 'textviewer',
                        markup = {},
                        variables = {},
                        source_hash = h,
                        game_version = dfhack.getDFVersion(),
                    })
                end
            end
            return  -- 命中一个 textviewer 即止(嵌套罕见)
        end
        cur = cur.parent
    end
end

-- 捕获: gamelog 历史回溯(History, 新行去重) ----------------------------------

local function tail_gamelog()
    if not S.gamelog.enabled then return end
    local path = S.gamelog.path
    if not path then
        local base = (dfhack.getDFPath and dfhack.getDFPath()) or '.'
        path = base .. '/gamelog.txt'
        S.gamelog.path = path
    end
    local fh = io.open(path, 'rb')
    if not fh then return end
    if not S.log_offset then
        -- 首次: 只从当前位置起(不重放整部历史)
        S.log_offset = fh:seek('end')
        fh:close()
        return
    end
    fh:seek('set', S.log_offset)
    local chunk = fh:read('*a')
    S.log_offset = fh:seek('end')
    fh:close()
    if not chunk or #chunk == 0 then return end
    for raw_line in chunk:gmatch('[^\r\n]+') do
        local line = dfhack.df2console(raw_line)
        local h = fnv1a(line)
        if #line > 0 and not S.recent_log_hashes then S.recent_log_hashes = {} end
        if S.recent_log_hashes[h] then
            -- 已见(可能与 report 重复)
        else
            S.recent_log_hashes[h] = true
            push_event({
                event_id = 'log-' .. now_ms() .. '-' .. S.stats.captured,
                timestamp = now_ms(),
                screen = 'history',
                source_text = line,
                text_type = 'HISTORY',
                priority = 30,              -- 历史回溯 → PREFETCH 档
                context_id = '',
                markup = {},
                variables = {},
                source_hash = h,
                game_version = dfhack.getDFVersion(),
            })
        end
        local n = 0
        for _ in pairs(S.recent_log_hashes) do n = n + 1 end
        if n > 2048 then S.recent_log_hashes = {} end  -- 防无限增长
    end
end

-- 套接字(DFHack luasocket 非阻塞轮询; 官方 API 仅 TCP, 见审计 §5) --------------

local function socket_module()
    local ok, mod = pcall(require, 'plugins.luasocket')
    if ok and mod then return mod end
    return nil
end

local function connect()
    local sock = socket_module()
    if not sock then
        S.engine_online = false
        return false
    end
    local ok, client = pcall(sock.tcp.connect, sock.tcp, C.host, C.port)
    if not ok or not client then
        S.engine_online = false
        return false
    end
    local _, serr = pcall(client.setNonblocking, client)
    S.client = client
    S.engine_online = true
    S.stats.reconnect = S.stats.reconnect + 1
    S.readbuf = ''
    return true
end

local function disconnect(reason)
    if S.client then
        pcall(S.client.close, S.client)
        S.client = nil
    end
    S.engine_online = false
    S.sent = {}    -- 已发在途作废: 断线期间引擎侧结果不可追溯(§2.3 静默)
    S.inflight = 0
    S.last_error = reason or S.last_error
end

local function send_line(client, line)
    -- 非阻塞 socket: 单次 send; 失败即视为断线(下个节拍重连)
    return pcall(client.send, client, line .. '\n')
end

-- 读回(非阻塞): receive('*l') 无数据时返回 nil(EWOULDBLOCK 静默吞掉, 见 luasocket.cpp
-- handle_error(skip_timeout=true)); 残余半行风险在单次小响应(≤16KB 回环)下为零。
local MAX_READ_PER_TICK = 16

local function drain_lines(callback)
    for _ = 1, MAX_READ_PER_TICK do
        local line = S.client:receive('*l')
        if not line or line == '' then break end
        S.stats.recv = S.stats.recv + 1
        callback(line)
    end
end

local function ack_event(event_id)
    if S.sent[event_id] ~= nil then
        S.inflight = math.max(0, S.inflight - 1)
    end
    S.sent[event_id] = nil
end

local function handle_response(line)
    local ok, obj = pcall(JSON.decode, line)
    if not ok or type(obj) ~= 'table' then return end
    if obj.error and obj.error.message then
        S.last_error = 'rpc error: ' .. tostring(obj.error.message)
        return
    end
    local result = obj.result
    if not result then return end
    if result.translations then  -- fetch_done: 已完成事件批量回放
        for _, rec in ipairs(result.translations) do
            ack_event(rec.event_id)
            if rec.status == 'done' then
                S.stats.done = S.stats.done + 1
                if not S.displayed[rec.event_id]
                   and rec.confidence and rec.confidence > 0
                   and rec.translated_text and rec.translated_text ~= '' then
                    S.render_lines[#S.render_lines + 1] = {
                        event_id = rec.event_id,
                        text = rec.translated_text,
                        confidence = rec.confidence,
                    }
                    if #S.render_lines > 12 then table.remove(S.render_lines, 1) end
                    S.displayed[rec.event_id] = true
                end
            elseif rec.status == 'failed' then
                S.stats.failed = S.stats.failed + 1  -- §2.3: 静默原文
            end
        end
        return
    end
    if result.status == 'done' then
        ack_event(result.event_id)
        S.stats.done = S.stats.done + 1
        if not S.displayed[result.event_id]
           and result.confidence and result.confidence > 0
           and result.translated_text and result.translated_text ~= '' then
            S.render_lines[#S.render_lines + 1] = {
                event_id = result.event_id,
                text = result.translated_text,
                confidence = result.confidence,
            }
            if #S.render_lines > 12 then table.remove(S.render_lines, 1) end
            S.displayed[result.event_id] = true
        end
    elseif result.status == 'failed' or (result.error and result.error ~= '') then
        ack_event(result.event_id)
        S.stats.failed = S.stats.failed + 1
        -- §2.3: 失败静默 → 游戏照常显示原文, 不打扰
    end
end

-- 渲染载荷(纯函数, 与 overlay 解耦; CJK 字形未就绪时输出空 → 不绘制) -----------

function fanyi_render_lines()
    if not S.render_cjk then return '' end  -- CJK 门控关闭 → 空载荷(§2.3 静默原文)
    local payload = fanyi_render_payload(8)
    if #payload == 0 then return '' end
    local parts = {}
    for _, row in ipairs(payload) do parts[#parts + 1] = row.text end
    return table.concat(parts, '\n')
end

-- UTF-8 感知换行: 一段文本按字符数拆成 ≤max_cols 的行(优先在空格处断行)
function fanyi_wrap_line(text, max_cols)
    local cps = fanyi_utf8_codepoints(text)
    if #cps <= max_cols then return {text} end
    local rows = {}
    local start_i = 1
    while start_i <= #cps do
        local end_i = math.min(start_i + max_cols - 1, #cps)
        if end_i < #cps then
            -- 窗口内找最后一个空格作断点(至少保留 30% 长度, 防碎行)
            local space_i = end_i
            while space_i > start_i + math.floor(max_cols * 0.3) and cps[space_i] ~= 32 do
                space_i = space_i - 1
            end
            if cps[space_i] == 32 then end_i = space_i end
        end
        local seg = {}
        for i = start_i, end_i do seg[#seg + 1] = utf8.char(cps[i]) end
        rows[#rows + 1] = table.concat(seg)
        start_i = end_i + 1
    end
    return rows
end

-- 渲染载荷(行列表, 底部字幕条数据源; 与贴图/overlay 完全解耦, 可无头断言)。
-- 长译文(如 textviewer 整页)先按 '\n' 拆段再按宽度换行; 从最新条目回填,
-- 装满 max_rows 为止(新内容优先显示)。
function fanyi_render_payload(max_rows, max_cols)
    if not S.overlays_on then return {} end
    if #S.render_lines == 0 then return {} end
    max_rows = max_rows or 8
    max_cols = max_cols or 60
    local groups = {}
    local used = 0
    for i = #S.render_lines, 1, -1 do
        local rows = {}
        for seg in (S.render_lines[i].text .. '\n'):gmatch('([^\n]*)\n') do
            if #seg > 0 then
                for _, r in ipairs(fanyi_wrap_line(seg, max_cols)) do
                    rows[#rows + 1] = r
                end
            end
        end
        if used + #rows > max_rows and used > 0 then break end  -- 旧条目放不下
        if used + #rows > max_rows then                          -- 单条超行: 只留最新
            while #rows > max_rows do table.remove(rows, 1) end
        end
        table.insert(groups, 1, rows)  -- 头插恢复时间正序
        used = used + #rows
        if used >= max_rows then break end
    end
    local out = {}
    for _, rows in ipairs(groups) do
        for _, r in ipairs(rows) do
            out[#out + 1] = {text = r, confidence = 1}
        end
    end
    return out
end

-- CJK 字形贴图(ticket-009): UTF-8 解码 + 图集装载 + 贴图坐标 ------------------

-- UTF-8 → codepoint 表(自实现, 不依赖 DFHack Lua 编译选项; 非法序列→U+FFFD)
function fanyi_utf8_codepoints(s)
    local out = {}
    local i, n = 1, #s
    while i <= n do
        local b1 = s:byte(i)
        local cp, extra
        if b1 < 0x80 then cp, extra = b1, 0
        elseif b1 >= 0xC2 and b1 < 0xE0 then cp, extra = b1 - 0xC0, 1
        elseif b1 >= 0xE0 and b1 < 0xF0 then cp, extra = b1 - 0xE0, 2
        elseif b1 >= 0xF0 and b1 < 0xF5 then cp, extra = b1 - 0xF0, 3
        else cp, extra = 0xFFFD, 0 end
        local ok = true
        for k = 1, extra do
            local b = s:byte(i + k)
            if b and b >= 0x80 and b < 0xC0 then
                cp = cp * 0x40 + (b - 0x80)
            else
                ok = false
                break
            end
        end
        if not ok then cp = 0xFFFD end
        out[#out + 1] = cp
        i = i + (ok and (extra + 1) or 1)
    end
    return out
end

-- 装载字形图集(hack/data/fanyi-font/): index.json + loadTileset 注册纹理页。
-- 成功后 S.font.by_cp[codepoint]=texpos; 任何失败 → installed=false(门控静默原文)。
function fanyi_font_load()
    local tex = dfhack.textures
    if not (tex and tex.loadTileset and tex.getTexposByHandle) then
        S.font.installed = false
        return false, 'dfhack.textures API 不可用'
    end
    local dir = (dfhack.getDFPath and dfhack.getDFPath() or '.') .. '/hack/data/fanyi-font/'
    local fh = io.open(dir .. 'index.json', 'r')
    if not fh then
        S.font.installed = false
        return false, '未安装字形图集(' .. dir .. '; scripts/generate_font_atlas.py 产出)'
    end
    local data = fh:read('*a')
    fh:close()
    local ok, index = pcall(JSON.decode, data)
    if not ok or type(index) ~= 'table' or type(index.pages) ~= 'table' then
        S.font.installed = false
        return false, 'index.json 解析失败'
    end
    local by_cp = {}
    for _, page in ipairs(index.pages) do
        local handles = tex.loadTileset(
            dir .. page.png, index.tile_w or 8, index.tile_h or 12, true)
        if type(handles) ~= 'table' then
            S.font.installed = false
            return false, 'loadTileset 失败: ' .. tostring(page.png)
        end
        for i, cp in ipairs(page.cps) do
            by_cp[cp] = tex.getTexposByHandle(handles[i]) or 0
        end
    end
    S.font.by_cp = by_cp
    S.font.tile_w = index.tile_w or 8
    S.font.tile_h = index.tile_h or 12
    S.font.installed = true
    return true
end

-- 哈希表显式计数(`#` 对非数组未定义 —— ticket-008 已踩坑, 严禁回归)
function fanyi_count_map(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

-- 贴图绘制(纯逻辑, dc 由调用方注入; 返回绘制格数): 底部对齐逐字贴 texpos,
-- 缺字形跳格(保持与源文同宽对齐); 只在渲染回调内被 overlay 框架调用。
function fanyi_paint_subtitle(dc, max_cols, max_rows)
    if not (S.overlays_on and S.render_cjk and S.font.installed) then return 0 end
    local payload = fanyi_render_payload(max_rows)
    if #payload == 0 then return 0 end
    max_cols = max_cols or 60
    max_rows = max_rows or 8
    local base_row = max_rows - #payload  -- 底部对齐
    local painted = 0
    for ri, line in ipairs(payload) do
        local x = 0
        for _, cp in ipairs(fanyi_utf8_codepoints(line.text)) do
            if x >= max_cols then break end
            local tp = S.font.by_cp[cp]
            if tp and tp > 0 then
                dc:seek(x, base_row + ri - 1):tile(' ', tp)
                painted = painted + 1
            end
            x = x + 1
        end
    end
    return painted
end

-- overlay 字幕条小部件(官方 overlay 插件): 底部右侧, 8 行×60 列。
-- 渲染只在 onRenderBody 回调内发生(§29: 不占主循环); 门控关闭时不画任何像素。
FanyiSubtitle = defclass(FanyiSubtitle, overlay.OverlayWidget)
FanyiSubtitle.ATTRS = FanyiSubtitle.ATTRS or {}
FanyiSubtitle.ATTRS.desc = 'DF-FanYi 中文译文悬浮(底部字幕条, fanyi overlays on cjk)'
FanyiSubtitle.ATTRS.default_pos = {x = -2, y = -2}
FanyiSubtitle.ATTRS.default_enabled = false
FanyiSubtitle.ATTRS.viewscreens = 'all'
FanyiSubtitle.ATTRS.frame = {w = 60, h = 8}

function FanyiSubtitle:onRenderBody(dc)
    fanyi_paint_subtitle(dc, self.frame.w, self.frame.h)
end

-- overlay 插件扫描脚本全局 OVERLAY_WIDGETS 注册小部件(名字: fanyi.subtitle)
OVERLAY_WIDGETS = {subtitle = FanyiSubtitle}

-- 主轮询节拍 ----------------------------------------------------------------

local function rpc_request(method, params)
    local req = {jsonrpc='2.0', id=method .. '-' .. now_ms(), method=method, params=params or {}}
    -- DFHack 捆绑 json 库 encode 输出多行缩进 JSON(逐行 send 会被引擎 json.loads
    -- 拆行报 -32700 parse error); 压缩成单行(只去换行+行首缩进, 值内空格保留)。
    local encoded = JSON.encode(req)
    return (encoded:gsub('\n[ \t]*', ''))
end

local function tick()
    if S.state ~= 'running' then return end
    S.tick = S.tick + 1

    -- 网络状态机
    if not S.engine_online then
        if S.tick >= S.retry_after then
            if connect() then
                S.retry_after = S.tick + C.retry_frames
            else
                S.retry_after = S.tick + math.min(C.retry_frames * 2 ^ (S.stats.reconnect % 4), 900)
            end
        end
    else
        local client = S.client
        if not client then
            S.engine_online = false
        else
            -- 周期健康轮询(fetch_done 同时充当心跳)
            local poll_done = (S.tick % 60 == 0)
            -- 发送在途事件(每节拍最多 max_send_per_tick 条)
            local nsent = 0
            while #S.pending > 0 and nsent < C.max_send_per_tick do
                local ev = table.remove(S.pending, 1)
                local ok = send_line(client, rpc_request('translate', ev))
                if not ok then
                    disconnect('send failed')
                    break
                end
                S.sent[ev.event_id] = ev.source_text
                S.inflight = S.inflight + 1
                S.stats.sent = S.stats.sent + 1
                nsent = nsent + 1
            end
            if S.client and (S.inflight > 0 or poll_done) then
                if S.engine_online then
                    local ok = send_line(client, rpc_request('fetch_done', {}))
                    if not ok then disconnect('fetch send failed') end
                end
            end
            -- 读回(非阻塞)
            if S.client and S.engine_online then
                pcall(drain_lines, handle_response)
            end
        end
    end

    -- gamelog 回溯(低频)
    if S.tick % 10 == 0 then
        pcall(capture_from_containers)
    end
    if S.tick % 30 == 0 then
        pcall(tail_gamelog)
    end
    if S.tick % 20 == 0 then
        pcall(capture_textviewer)
    end

    if S.state == 'running' then
        S.timer_euid = dfhack.timeout(C.tick_frames, 'frames', tick)
    end
end

local function arm_timer()
    if S.timer_euid then
        dfhack.timeout_active(S.timer_euid, nil)
        S.timer_euid = nil
    end
    S.timer_euid = dfhack.timeout(C.tick_frames, 'frames', tick)
end

local function cancel_timer()
    if S.timer_euid then
        pcall(dfhack.timeout_active, S.timer_euid, nil)
        S.timer_euid = nil
    end
end

-- 命令 ----------------------------------------------------------------------

function fanyi_status_lines()
    local lines = {
        'DF-FanYi fanyi bridge',
        ('  引擎: %s (loopback %s:%d)'):format(
            S.engine_online and '在线' or '离线(静默原文, 指数退避重连)',
            C.host, C.port),
        ('  状态: %s  tick=%d'):format(S.state, S.tick),
        ('  统计: 捕获=%d 发送=%d 接收=%d done=%d failed=%d 重连=%d'):format(
            S.stats.captured, S.stats.sent, S.stats.recv,
            S.stats.done, S.stats.failed, S.stats.reconnect),
        ('  字幕: %s  CJK渲染: %s'):format(
            S.overlays_on and '开' or '关',
            S.render_cjk and ('就绪(' .. fanyi_count_map(S.font.by_cp) .. '字形)') or
                (S.font.installed and '图集已装(未启用)' or '未启用(需 fanyi overlays on cjk)')),
        ('  overlay小部件: fanyi.subtitle (%s)'):format(
            overlay.isOverlayEnabled and tostring(overlay.isOverlayEnabled('fanyi.subtitle')) or '?'),
    }
    local n_tv = 0
    for _ in pairs(S.seen_tv_hashes) do n_tv = n_tv + 1 end
    table.insert(lines, ('  文本弹窗捕获: 已见 %d 页 (调试日志: %s)'):format(
        n_tv, tostring(S.debug_log.path)))
    if S.safe_mode then
        table.insert(lines, '  ⛔ SAFE MODE: ' .. S.safe_reason)
        table.insert(lines, '  (§44: 不做 UI 修改, 仅诊断; 等待兼容版本或更新守卫)')
    end
    if S.last_error ~= '' then
        table.insert(lines, '  最近错误: ' .. S.last_error)
    end
    return lines
end

function fanyi_command(args)
    if S.safe_mode and args[1] ~= 'status' and args[1] ~= 'debug' then
        print('Safemode: ' .. S.safe_reason)
        print('仅 status/debug 可用(§44 诊断); 请将 DF/DFHack 更新到守卫版本。')
        return
    end
    local cmd = args[1] or 'status'
    if cmd == 'status' then
        for _, l in ipairs(fanyi_status_lines()) do print(l) end
    elseif cmd == 'start' or cmd == 'enable' then
        if S.state == 'running' then print('已在运行') return end
        S.state = 'running'
        S.stats.reconnect = 0
        S.retry_after = 0
        ensure_capture()
        arm_timer()
        flog('fanyi start (捕获: 公告+游戏日志+文本弹窗)')
        print('fanyi 捕获已启动(公告+游戏日志+文本弹窗); 引擎离线时静默显示原文')
    elseif cmd == 'stop' or cmd == 'disable' then
        if S.state ~= 'running' then print('未运行') return end
        S.state = 'stopped'
        cancel_timer()
        disconnect('user stop')
        flog('fanyi stop')
        print('fanyi 已停止')
    elseif cmd == 'overlays' then
        local sub = args[2] or ''
        if sub == 'on' then
            S.overlays_on = true
            if args[3] == 'cjk' then
                if not S.font.installed then
                    local ok, err = fanyi_font_load()
                    if not ok then
                        print('CJK 渲染不可用: ' .. err)
                        print('仍以 ASCII 门控运行(游戏显示原文, §2.3)')
                        return
                    end
                end
                S.render_cjk = true
                -- 官方路径启用小部件: overlay enable fanyi.subtitle(持久化 overlay.json)
                if overlay.rescan then pcall(overlay.rescan) end
                if dfhack.run_command then
                    pcall(dfhack.run_command, 'overlay', 'enable', 'fanyi.subtitle')
                end
                print(('译文悬浮已开启(CJK 贴图就绪: %d 字形, tile %dx%d)')
                    :format(fanyi_count_map(S.font.by_cp), S.font.tile_w, S.font.tile_h))
            else
                print('译文悬浮已开启(ASCII 可用; CJK 需字形图集: fanyi overlays on cjk)')
            end
        elseif sub == 'off' then
            S.overlays_on = false
            print('译文悬浮已关闭(游戏显示原文)')
        else
            print('用法: fanyi overlays on|off [cjk]')
        end
    elseif cmd == 'clear' then
        S.render_lines = {}
        S.displayed = {}
        S.recent_reports = {}
        S.recent_log_hashes = {}
        S.seen_tv_hashes = {}
        S.sent = {}
        S.inflight = 0
        S.stats.captured = 0
        print('已清空显示/去重状态')
    elseif cmd == 'debug' then
        print('pending=', #S.pending, 'inflight=', #S.sent,
              'readbuf=', #S.readbuf, 'retry_after=', S.retry_after)
        print('displayed=', #S.render_lines, 'client=', S.client ~= nil, 'euid=', tostring(S.timer_euid))
        print('font: installed=', S.font.installed, 'tile=', S.font.tile_w .. 'x' .. S.font.tile_h,
              'glyphs=', fanyi_count_map(S.font.by_cp))
        local n_tv = 0
        for _ in pairs(S.seen_tv_hashes) do n_tv = n_tv + 1 end
        print('textviewer 去重:', n_tv, 'debuglog=', tostring(S.debug_log.path))
    else
        print([[
用法:
  fanyi status              状态/统计
  fanyi start|stop          启停捕获+轮询(--@ enable=true 默认自启)
  fanyi overlays on|off    译文悬浮(off: 游戏显示原文 §2.3)
  fanyi overlays on cjk    启用中文贴图(需字形图集 hack/data/fanyi-font/)
  fanyi clear              清空显示与去重
  fanyi debug              内部细节(含字体图集状态)
]])
    end
end

-- 脚本入口(每次命令执行) ------------------------------------------------------

local dfhack_flags = (type(dfhack_flags) == 'table' and dfhack_flags) or {}
local args = {...}
local ran_auto = false

if #args == 0 and dfhack_flags.enable then
    table.insert(args, 'start')
    ran_auto = true
end

fanyi_check_version()
if #args > 0 then
    fanyi_command(args)
elseif not ran_auto then
    -- 无参数且未启用: 尝试过即可, 打印一行诊断
    print(dfhack.df2console('(fanyi) 引擎桥已加载'))
end