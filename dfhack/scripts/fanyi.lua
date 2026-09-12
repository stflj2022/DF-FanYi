-- DF-FanYi 游戏侧桥(ticket-008): 捕获游戏文本 → loopback TCP → 翻译引擎(JSON-RPC)。
-- 安装在 hack/scripts/fanyi.lua 后, `fanyi` 成为 DFHack 命令(脚本自注册)。
--@ module = true
-- (module 标记必须: overlay.rescan 只扫描 module scripts 的 OVERLAY_WIDGETS,
--  无此标记则 fanyi.subtitle 永远 "widget not found" — 2026-09-10 stderr 实锤)
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
    -- ticket-012: 字幕条已删除. FANYI_TTL_MS 环境变量名保留为公共约定,
    -- 供后续 ticket-013/014 inline overlay TTL 使用(13/14 可选读此 env var).
    -- 启动时仍解析此 env var(避免被外层覆盖脚本误以为已删除), 但不再用于字幕.
    fanyi_ttl_ms = tonumber(os.getenv('FANYI_TTL_MS')) or 40000,
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
-- ticket-012: 字幕条相关状态字段已删除(S.render_lines / S.overlays_on /
-- S.render_cjk / S.font). 译文渲染改由 ticket-013/014 的内嵌 overlay 接管.
S.last_error = S.last_error or ''
S.gamelog = S.gamelog or {enabled=true, path=nil}
S.seen_tv_hashes = S.seen_tv_hashes or {}  -- textviewer 弹窗内容去重(哈希)
S.debug_log = S.debug_log or {path = nil}  -- 轻量文件日志(游戏目录 fanyi-debug.log)
S.last_vs_fingerprint = S.last_vs_fingerprint or ''  -- viewscreen 链类名指纹(变化时落盘)

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
    -- 探测: viewscreen 链类名指纹变化时落盘(定位弹窗真实类名, 无控制台排障)
    local names = {}
    local cur = vs
    while cur do
        local oks, s = pcall(tostring, cur)
        names[#names + 1] = (oks and s:match('<([^:]+):') or (oks and s or '?'))
        cur = cur.parent
    end
    local fp = table.concat(names, '<')
    if fp ~= S.last_vs_fingerprint then
        S.last_vs_fingerprint = fp
        flog('viewscreen链: ' .. fp)
    end
    cur = vs
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
                -- ticket-012: 字幕条已删除, 此处不再 push S.render_lines.
                -- ticket-013/014 的 inline overlay widget 会自行从 S.displayed 读取
                -- event_id, 调用 fanyi_fetch_translation() 取得译文文本(数据路径
                -- 不变, 仅消费者从"统一字幕条"变成"按 viewscreen 定位的 widget").
                if not S.displayed[rec.event_id] and rec.confidence
                    and rec.confidence > 0 and rec.translated_text
                    and rec.translated_text ~= '' then
                    S.displayed[rec.event_id] = true
                    flog('done [' .. tostring(rec.event_id) .. '] '
                        .. (rec.translated_text:sub(1, 60)):gsub('%s+', ' '))
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
            S.displayed[result.event_id] = true
            flog('done [' .. tostring(result.event_id) .. '] '
                .. (result.translated_text:sub(1, 60)):gsub('%s+', ' '))
        end
    elseif result.status == 'failed' or (result.error and result.error ~= '') then
        ack_event(result.event_id)
        S.stats.failed = S.stats.failed + 1
        -- §2.3: 失败静默 → 游戏照常显示原文, 不打扰
    end
end

-- 渲染载荷(纯函数, 与 overlay 解耦; ticket-012 字幕条删除后, 此函数仅保留
-- 签名供 ticket-013/014 的 textviewer_inline / announcement_inline 复用。
-- 当前实现返回空载荷, 真正绘制由 13/14 的 inline overlay widget 完成。
-- 数据契约: 返回 {{text=string, confidence=number}, ...}; 长度 0 = 无内容)
function fanyi_render_inline_payload()
    return {}
end

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

-- 每局重置去重集(SC_MAP_LOADED): 游戏对象 ID 与文本哈希都跨局复用, 永久去重
-- 会把新要塞的到达公告/同名弹窗永远吞掉 → 字幕永远无新内容("死字幕"根因,
-- 2026-09-10 实锤: 本局 report-1 被上局残留的 seen_container_ids 吞掉)。
-- TM/引擎 L1 缓存命中秒回, 不会引发 LLM 重译, 重置本身无成本。
local function fanyi_install_state_hooks()
    if S.state_hooked then return end
    S.state_hooked = true
    dfhack.onStateChange['fanyi-reset-dedup'] = function(sc)
        if sc ~= SC_MAP_LOADED then return end
        S.seen_container_ids = {}
        S.seen_tv_hashes = {}
        S.displayed = {}
        flog('新地图已加载: 去重集清空(seen_container/tv/displayed)')
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
        -- ticket-012: 字幕条 widget 已删除. status 输出明确告知替代方案:
        -- 公告由 announcement_inline (13), textviewer 弹窗由 textviewer_inline (14).
        ('  subtitle widget: removed (replaced by inline overlays) — '
            .. 'announcement_inline: ticket-013, textviewer_inline: ticket-014'),
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
        fanyi_install_state_hooks()
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
        -- ticket-012: 字幕条 widget 已删除. 命令语法变更:
        --   fanyi overlays on|off [textviewer|announcement]
        -- 后续 ticket-013/014 会分别注册 textviewer_inline / announcement_inline widget,
        -- 命令参数对应到具体 inline overlay 的开关. 当前这两个 widget 未注册,
        -- 给用户明确提示而非静默失败.
        local sub = args[2] or ''
        local which = args[3] or ''
        if sub == '' or sub == 'help' then
            print('用法: fanyi overlays on|off [textviewer|announcement]')
            print('  textviewer:   弹窗(textviewer)区域覆盖中文 (ticket-013)')
            print('  announcement: 公告面板覆盖中文 (ticket-014)')
            print('  注: 字幕条 (subtitle / cjk) 已删除 — 见 fanyi status')
        elseif sub == 'on' then
            if which == 'cjk' then
                print('字幕条已删除 (ticket-012). 请使用 textviewer 或 announcement.')
            elseif which == 'textviewer' or which == 'announcement' then
                print(('overlays on %s: 等待 ticket-013/014 注册 inline widget'):format(which))
            else
                print('用法: fanyi overlays on|off [textviewer|announcement]')
            end
        elseif sub == 'off' then
            if which == 'cjk' then
                print('字幕条已删除 (ticket-012), 无需关闭.')
            elseif which == 'textviewer' or which == 'announcement' then
                print(('overlays off %s: 等待 ticket-013/014 注册 inline widget'):format(which))
            else
                print('译文悬浮已关闭(游戏显示原文)')
            end
        else
            print('用法: fanyi overlays on|off [textviewer|announcement]')
        end
    elseif cmd == 'clear' then
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
        print('displayed=', fanyi_count_map(S.displayed), 'client=', S.client ~= nil,
              'euid=', tostring(S.timer_euid))
        print('ttl_ms=', C.fanyi_ttl_ms)
        local n_tv = 0
        for _ in pairs(S.seen_tv_hashes) do n_tv = n_tv + 1 end
        print('textviewer 去重:', n_tv, 'debuglog=', tostring(S.debug_log.path))
    else
        print([[
用法:
  fanyi status              状态/统计
  fanyi start|stop          启停捕获+轮询(--@ enable=true 默认自启)
  fanyi overlays on|off [textviewer|announcement]
                            内嵌 overlay 开关 (字幕条已删除 ticket-012)
  fanyi clear              清空显示与去重
  fanyi debug              内部细节
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
if dfhack_flags.module then return end  -- require('fanyi') 时只定义不执行(DFHack module 规范)
if #args > 0 then
    fanyi_command(args)
elseif not ran_auto then
    -- 无参数且未启用: 尝试过即可, 打印一行诊断
    print(dfhack.df2console('(fanyi) 引擎桥已加载'))
end