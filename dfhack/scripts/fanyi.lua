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
--   3. 渲染: textviewer 弹窗内嵌覆盖层(ticket-013, 原文区域直接贴中文,
--      观感像官方中文版; 字幕条已于 ticket-012 删除); 中文经字形图集
--      (hack/data/textviewer-font/, subset 800, scale=2)+ dfhack.textures 官方 API
--      逐字贴图(定论见 docs/audits/DFHACK_INTEGRATION.md §9); 图集未安装/门控
--      关闭时不绘制 → 游戏显示原文(§2.3 静默降级);
--   4. 版本守卫: DF/DFHack 版本不匹配 → Safe Mode(不捕获/不渲染, 仅诊断 §43-44);
--   5. 断线自愈: 引擎未启动/宕机 → 指数退避重连, 任何时刻不阻塞游戏主循环
--      (所有套接字操作非阻塞 + pcall, §29/§30)。
--
-- 命令: fanyi status|start|stop|clear|debug|overlays on|off [textviewer|announcement]
--   overlays on textviewer: 开启弹窗内嵌中文覆盖层(ticket-013, 需图集
--   hack/data/textviewer-font/, 缺图集时静默显示原文)。
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
    -- ticket-014: 公告译文缓存 TTL(90s; 工单§3 过期后 overlay 不再显示,
    -- 原文自然显示, 避免翻译永远挂在面板上)
    ann_ttl_ms = tonumber(os.getenv('FANYI_ANN_TTL_MS')) or 90000,
    -- ticket-015 §1: 段落聚合窗口。同 hash 在 50ms 内合并为单次 push_event;
    -- 50ms 后允许再次推送(给玩家重新看同一页的余地)。
    paragraph_window_ms = tonumber(os.getenv('FANYI_PARAGRAPH_WINDOW_MS')) or 50,
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
S.tv_last_push_ts = S.tv_last_push_ts or {}  -- ticket-015 §1: 段落聚合窗口时间戳
-- ticket-013: textviewer 内嵌翻译覆盖层状态
S.tv_cache = S.tv_cache or {}        -- event_id → {text, ts, title}(译文缓存)
S.tv_meta = S.tv_meta or {}          -- event_id → title(捕获时记录)
if S.tv_on == nil then S.tv_on = true end  -- textviewer_inline 开关(默认开; 图集/引擎未就绪不绘制)
S.tv_font = S.tv_font or {installed = false}  -- 字形图集(hack/data/textviewer-font/)
S.tv_want_inline = S.tv_want_inline or {}     -- event_id → {text, title}(待发 inline 请求)
S.tv_inline_sent = S.tv_inline_sent or {}     -- event_id → true(inline_translate 已请求去重)
-- ticket-014: 公告面板内嵌翻译覆盖层状态
S.ann_cache = S.ann_cache or {}        -- event_id('report-*') → {text, ts}(译文, TTL=ann_ttl_ms)
if S.ann_on == nil then S.ann_on = true end  -- announcement_inline 开关(默认开; 无缓存/图集缺失时静默原文)
S.ann_want_inline = S.ann_want_inline or {}  -- event_id → {text}(待发 inline_translate, context=announcement)
S.ann_inline_sent = S.ann_inline_sent or {}  -- event_id → true(已请求去重)
-- ticket-017: 屏上选词翻译(F11)状态
S.ms = S.ms or {visible=false, text='', translation='', event_id=nil, rows={},
                 rect=nil, started=0}         -- 浮窗驻留状态(visible=true 时渲染)
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
                -- ticket-014: 登记 inline 路由请求(context=announcement;
                -- flush 时若常规队列已覆盖(S.sent)则跳过, 同文本不双发)
                if not S.ann_inline_sent['report-' .. id] then
                    S.ann_want_inline['report-' .. id] = {text = dfhack.df2console(text)}
                end
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
                    -- ticket-014: 登记 inline 路由(同 onReport; 引擎段落缓存让
                    -- 重复文本的公告秒回, 不重译)
                    if not S.ann_inline_sent['report-' .. rep.id] then
                        S.ann_want_inline['report-' .. rep.id] = {text = dfhack.df2console(rep.text)}
                    end
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
        fanyi_tv_cache_gc(fp)  -- ticket-013: 离开 textviewer → 清空弹窗译文缓存
        flog('viewscreen链: ' .. fp)
    end
    cur = vs
    while cur do
        local okc, is_tv = pcall(function()
            return df.viewscreen_textviewerst ~= nil
               and df.viewscreen_textviewerst:is_instance(cur)
        end)
        if okc and is_tv then
            local ok_b, full, title = pcall(function()
                local t = tostring(cur.title or '')
                local parts = {}
                if #t > 0 then parts[#parts + 1] = t end
                for i = 0, #cur.text - 1 do
                    local ln = tostring(cur.text[i] or '')
                    if #ln > 0 then parts[#parts + 1] = ln end
                end
                return table.concat(parts, '\n'), t
            end)
            if ok_b and full and #full >= 10 then
                local h = fnv1a(full)
                local now = now_ms()
                -- ticket-015 §1: 50ms 段落聚合窗口. 同一 hash 在 50ms 内多次出现合并为单次 push
                -- (防视图高频重绘/页面快速切换重复 push_event). 50ms 后允许再次推送,
                -- 但跳过永久去重 seen_tv_hashes 抑制刷新重看(仍依赖 long_term 缓存)。
                local last_ts = S.tv_last_push_ts[h] or 0
                if not S.seen_tv_hashes[h] or (now - last_ts) > C.paragraph_window_ms then
                    S.seen_tv_hashes[h] = true
                    S.tv_last_push_ts[h] = now
                    -- 有界: 哈希表膨胀时丢弃一半
                    local keys = {}
                    for k in pairs(S.seen_tv_hashes) do keys[#keys + 1] = k end
                    if #keys > 512 then
                        for j = 1, #keys - 256 do S.seen_tv_hashes[keys[j]] = nil end
                    end
                    local ts_keys = {}
                    for k in pairs(S.tv_last_push_ts) do ts_keys[#ts_keys + 1] = k end
                    if #ts_keys > 512 then
                        for j = 1, #ts_keys - 256 do S.tv_last_push_ts[ts_keys[j]] = nil end
                    end
                    S.tv_meta['tv-' .. h] = title or ''  -- ticket-013: 译文缓存元数据
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
                    if type(rec.event_id) == 'string' and rec.event_id:sub(1, 3) == 'tv-' then
                        fanyi_tv_cache_set(rec.event_id, rec.translated_text)
                    elseif type(rec.event_id) == 'string' and rec.event_id:sub(1, 7) == 'report-' then
                        fanyi_ann_cache_set(rec.event_id, rec.translated_text)  -- ticket-014
                    elseif type(rec.event_id) == 'string' and rec.event_id:sub(1, 3) == 'ms-' then
                        fanyi_ms_apply_translation(rec.event_id, rec.translated_text)  -- ticket-017
                    end
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
            if type(result.event_id) == 'string' and result.event_id:sub(1, 3) == 'tv-' then
                fanyi_tv_cache_set(result.event_id, result.translated_text)
            elseif type(result.event_id) == 'string' and result.event_id:sub(1, 7) == 'report-' then
                fanyi_ann_cache_set(result.event_id, result.translated_text)  -- ticket-014
            elseif type(result.event_id) == 'string' and result.event_id:sub(1, 3) == 'ms-' then
                fanyi_ms_apply_translation(result.event_id, result.translated_text)  -- ticket-017
            end
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

-- ===== ticket-013: textviewer 弹窗内嵌翻译覆盖层 ==============================

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

-- 剥离 DF 颜色/格式标记([C:R:G:B]/[B]/[VAR:..]/[P:..]), 保留字面方括号文本
-- (如 "[需要燃料]")。弹窗原文可能携带标记, 贴图前剥离避免乱字(2026-09-11 同款防线)。
local DF_MARKUP_RE = '%[C:%d+:%d+:%d+%]'
local function fanyi_strip_df_markup(text)
    if not text or #text == 0 then return text end
    local t = text:gsub(DF_MARKUP_RE, '')
    t = t:gsub('%[B%]', '')
    t = t:gsub('%[VAR:[^%[%]]*%]', '')
    t = t:gsub('%[P:%d+:[^%[%]]*%]', '')
    return t
end

-- UTF-8 感知换行: 一段文本按字符数拆成 ≤max_cols 的行(优先在空格处断行)
function fanyi_wrap_line(text, max_cols)
    text = fanyi_strip_df_markup(text)
    local cps = fanyi_utf8_codepoints(text)
    if #cps <= max_cols then return {text} end
    local rows = {}
    local start_i = 1
    while start_i <= #cps do
        local end_i = math.min(start_i + max_cols - 1, #cps)
        if end_i < #cps then
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

-- 哈希表显式计数(`#` 对非数组未定义 —— ticket-008 已踩坑, 严禁回归)
function fanyi_count_map(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

-- 弹窗译文缓存: get/set/expire(纯逻辑, 可无头测试) --------------------------------

function fanyi_tv_cache_set(event_id, text, title)
    if not event_id or not text or #text == 0 then return false end
    S.tv_cache[event_id] = {
        text = text,
        ts = now_ms(),
        title = title or S.tv_meta[event_id] or '',
    }
    -- 有界: 超过 32 条时按时间序丢最老(欢迎向导页数有限, 不需更大)
    local keys = {}
    for k in pairs(S.tv_cache) do keys[#keys + 1] = k end
    if #keys > 32 then
        table.sort(keys, function(a, b)
            return (S.tv_cache[a].ts or 0) < (S.tv_cache[b].ts or 0)
        end)
        for i = 1, #keys - 32 do S.tv_cache[keys[i]] = nil end
    end
    return true
end

function fanyi_tv_cache_get(event_id)
    return S.tv_cache[event_id]
end

-- 过期: 离开 textviewer(viewscreen 链指纹不含 textviewer) → 清空全部弹窗缓存;
-- 引擎侧 inline_translate 段落缓存仍在, 重进同页时秒回不重译。
function fanyi_tv_cache_gc(screen_fingerprint)
    if not screen_fingerprint or not screen_fingerprint:find('textviewer', 1, true) then
        local n = fanyi_count_map(S.tv_cache)
        S.tv_cache = {}
        return n
    end
    return 0
end

-- 装载 textviewer 字形图集(hack/data/textviewer-font/, subset 800, scale=2)。
-- 与 ticket-009 同款管线: 存 handle, 贴图时实时 getTexposByHandle 解析
-- (世界加载 reset_texpos 后 dynamic 纹理自动重注册, 防“乱字/微缩图”回归)。
function fanyi_tv_font_load()
    local tex = dfhack.textures
    if not (tex and tex.loadTileset and tex.getTexposByHandle) then
        S.tv_font.installed = false
        return false, 'dfhack.textures API 不可用'
    end
    local dir = (dfhack.getDFPath and dfhack.getDFPath() or '.') .. '/hack/data/textviewer-font/'
    local fh = io.open(dir .. 'index.json', 'r')
    if not fh then
        S.tv_font.installed = false
        return false, '未安装字形图集(' .. dir
            .. '; scripts/generate_font_atlas.py --subset 800 --scale 2 产出)'
    end
    local data = fh:read('*a')
    fh:close()
    local ok, index = pcall(JSON.decode, data)
    if not ok or type(index) ~= 'table' or type(index.pages) ~= 'table' then
        S.tv_font.installed = false
        return false, 'index.json 解析失败'
    end
    local by_cp = {}
    local scale = tonumber(index.scale) or 1
    for _, page in ipairs(index.pages) do
        local handles = tex.loadTileset(
            dir .. page.png, index.tile_w or 8, index.tile_h or 12, true)
        if type(handles) ~= 'table' then
            S.tv_font.installed = false
            return false, 'loadTileset 失败: ' .. tostring(page.png)
        end
        local cols = tonumber(page.cols) or 64
        for i, cp in ipairs(page.cps) do
            if scale == 2 then
                -- 大字模式: 每字占 scale²=4 格, 生成端字 i(0基)起始 tile=i*2,
                -- 四片 = [t, t+1, t+cols, t+cols+1](与 generate_font_atlas.py 严格对应)
                local t = (i - 1) * 2
                by_cp[cp] = {
                    handles[t + 1], handles[t + 2],
                    handles[t + cols + 1], handles[t + cols + 2],
                }
            else
                by_cp[cp] = handles[i]
            end
        end
    end
    S.tv_font.tex = tex
    S.tv_font.by_cp = by_cp
    S.tv_font.tile_w = index.tile_w or 8
    S.tv_font.tile_h = index.tile_h or 12
    S.tv_font.scale = scale
    S.tv_font.installed = true
    S.tv_font.tried = false
    return true
end

-- 当前 textviewer 页(标题+全文哈希 → event_id; 与 capture_textviewer 同一拼接规则)
local function tv_current_page()
    local ok, vs = pcall(dfhack.gui.getCurViewscreen)
    if not ok or not vs then return nil end
    local cur = vs
    while cur do
        local okc, is_tv = pcall(function()
            return df and df.viewscreen_textviewerst ~= nil
               and df.viewscreen_textviewerst:is_instance(cur)
        end)
        if okc and is_tv then
            local ok_b, full, title = pcall(function()
                local t = tostring(cur.title or '')
                local parts = {}
                if #t > 0 then parts[#parts + 1] = t end
                for i = 0, #cur.text - 1 do
                    local ln = tostring(cur.text[i] or '')
                    if #ln > 0 then parts[#parts + 1] = ln end
                end
                return table.concat(parts, '\n'), t
            end)
            if ok_b and full and #full >= 10 then
                return {event_id = 'tv-' .. fnv1a(full), title = title or '', text = full}
            end
            return nil
        end
        cur = cur.parent
    end
    return nil
end

-- textviewer 控件矩形(0 基格坐标): 优先 DFHack 面板 API(存在则精确),
-- 不可用 → 居中内容区兑底(欢迎/教程弹窗居中且边距稳定; ticket-016 E2E 可调)。
function fanyi_textviewer_rect(max_cols, max_rows)
    max_cols = tonumber(max_cols) or 80
    max_rows = tonumber(max_rows) or 25
    local ok, box = pcall(function()
        local g = dfhack.gui or {}
        if g.getPanelLayout then
            local layout = g.getPanelLayout()
            local panel = layout and layout.TEXTVIEWER
            if type(panel) == 'table' and panel.x1 and panel.y1 and panel.x2 and panel.y2 then
                return {x = panel.x1, y = panel.y1,
                        w = panel.x2 - panel.x1 + 1, h = panel.y2 - panel.y1 + 1}
            end
        end
        error('panel layout unavailable')
    end)
    if ok and type(box) == 'table' and box.w and box.h and box.w > 0 and box.h > 0 then
        return box
    end
    local x = math.max(1, math.floor(max_cols * 0.1))
    return {x = x, y = 3, w = max_cols - 2 * x, h = max_rows - 6}
end

-- 中文行贴图(ticket-013/014 共用): rows 自 rect 顶部依次绘制, 缺字形跳格;
-- 返回绘制格数(scale=2 时每字 4 格)。dc 由调用方注入(真实 painter/无头 mock 均可)。
--
-- 2026-09-12 修复: 贴 CJK 前先涂背景, 覆盖原文英文。
-- 旧实现 dc:tile(' ', tp) 仅贴字形, 但字形透明像素让英文背景透出, 视觉上
-- "中英叠印" 误认为是字幕条回来了。这里两步: 先 dc:fill() 涂黑/灰底,
-- 再贴 CJK tile。bg/fg 与 textviewer/announcement 面板底色一致(灰底白字)。
local function paint_cjk_rows(dc, rows, rect)
    local scale = S.tv_font.scale or 1
    local resolve = S.tv_font.tex and S.tv_font.tex.getTexposByHandle or nil
    if not resolve then return 0 end
    -- 1) 涂底: 物理格全覆盖(2026-09-12 修复②: 旧版按 scale 间隔涂=棋盘格,
    --    3/4 格子漏涂, 英文从缝隙透出 → "中英叠印"。涂底与 scale 无关,
    --    逐物理格涂才能完全覆盖)。
    pcall(function() dc:pen(7, 0) end)  -- fg=white bg=black
    for ri = 0, rect.h - 1 do
        local y = rect.y + ri
        for ci = 0, rect.w - 1 do
            pcall(function() dc:seek(rect.x + ci, y):tile(' ', 0) end)
        end
    end
    -- 2) 贴 CJK 字形 (原有逻辑)
    local function tile_at(h, x, y)
        if not h then return false end
        if scale == 2 and type(h) == 'table' then
            local t1, t2 = resolve(h[1]), resolve(h[2])
            local t3, t4 = resolve(h[3]), resolve(h[4])
            if t1 and t1 > 0 and t2 and t2 > 0 and t3 and t3 > 0 and t4 and t4 > 0 then
                dc:seek(x, y):tile(' ', t1)
                dc:seek(x + 1, y):tile(' ', t2)
                dc:seek(x, y + 1):tile(' ', t3)
                dc:seek(x + 1, y + 1):tile(' ', t4)
                return true
            end
            return false
        elseif type(h) ~= 'table' then
            local tp = resolve(h)
            if tp and tp > 0 then
                dc:seek(x, y):tile(' ', tp)
                return true
            end
        end
        return false
    end
    local painted = 0
    for ri, line in ipairs(rows) do
        local x, y = rect.x, rect.y + (ri - 1) * scale
        for _, cp in ipairs(fanyi_utf8_codepoints(line)) do
            if x + scale > rect.x + rect.w then break end
            if tile_at(S.tv_font.by_cp[cp], x, y) then
                painted = painted + (scale == 2 and 4 or 1)
            end
            x = x + scale
        end
    end
    return painted
end

-- 贴图绘制(纯逻辑, dc 由调用方注入; 返回绘制格数): 在 textviewer 矩形内贴中文,
-- 缺字形跳格; 门控关闭/图集未装/无译文时返回 0(§2.3 静默原文)。
function fanyi_paint_textviewer(dc, max_cols, max_rows)
    if not S.tv_on then return 0 end
    if not dc then return 0 end
    if not S.tv_font.installed then
        if S.tv_font.tried then return 0 end  -- 每会话只试一次(图集静态, 不反复 IO)
        S.tv_font.tried = true
        fanyi_tv_font_load()
        if not S.tv_font.installed then return 0 end
    end
    local page = tv_current_page()
    if not page then return 0 end
    local rect = fanyi_textviewer_rect(max_cols, max_rows)
    local rec = S.tv_cache[page.event_id]
    if not rec then
        -- 翻译未就绪: 登记 inline 请求(tick 统一发送), 画淡显 "..." 占位(ASCII)
        if not S.tv_inline_sent[page.event_id] and S.sent[page.event_id] == nil
            and not S.tv_want_inline[page.event_id] then
            S.tv_want_inline[page.event_id] = {text = page.text, title = page.title}
            local keys = {}
            for k in pairs(S.tv_want_inline) do keys[#keys + 1] = k end
            if #keys > 64 then
                for i = 1, #keys - 32 do S.tv_want_inline[keys[i]] = nil end
            end
        end
        if (S.sent[page.event_id] ~= nil or S.tv_inline_sent[page.event_id])
            and dc.string then
            pcall(function() dc:seek(rect.x, rect.y):string('...') end)
        end
        return 0
    end
    local scale = S.tv_font.scale or 1
    local vis_cols = math.floor(rect.w / scale)
    local vis_rows = math.floor(rect.h / scale)
    -- 全文 → 段 → 行(先按换行拆段, 再按宽度折行; 超行保留首屏, textviewer 自身可滚动)
    local rows = {}
    for seg in (rec.text .. '\n'):gmatch('([^\n]*)\n') do
        if #seg > 0 then
            for _, r in ipairs(fanyi_wrap_line(seg, vis_cols)) do
                rows[#rows + 1] = r
            end
        end
    end
    while #rows > vis_rows do table.remove(rows) end
    return paint_cjk_rows(dc, rows, rect)
end

-- 待发 inline 请求冲刷(tick 内调用, 引擎在线时): 每页只请求一次,
-- 重开同页命中引擎段落缓存秒回(预算铁律: 不重译)。
local function tv_flush_inline_requests()
    if not S.engine_online then return end
    local client = S.client
    if not client then return end
    for event_id, req in pairs(S.tv_want_inline) do
        if not S.tv_inline_sent[event_id] then
            S.tv_inline_sent[event_id] = true
            local ok = send_line(client, rpc_request('inline_translate', {
                text = req.text, context = 'textviewer',
                title = req.title or '', event_id = event_id,
            }))
            if not ok then
                S.tv_inline_sent[event_id] = nil
                disconnect('inline send failed')
                return
            end
        end
    end
    S.tv_want_inline = {}
    local keys = {}
    for k in pairs(S.tv_inline_sent) do keys[#keys + 1] = k end
    if #keys > 128 then
        for i = 1, #keys - 64 do S.tv_inline_sent[keys[i]] = nil end
    end
end

-- overlay 小部件(官方 overlay 插件, 名字 fanyi.textviewer):
-- 仅在 viewscreen_textviewerst 激活(离开弹窗 → overlay 框架自动不再渲染, 中文
-- 随之消失); 全屏裁剪框 + 内部按 textviewer 矩形自定位, 不挡其他界面。
TextviewerInline = defclass(TextviewerInline, overlay.OverlayWidget)
TextviewerInline.ATTRS = TextviewerInline.ATTRS or {}
TextviewerInline.ATTRS.desc = 'DF-FanYi textviewer 内嵌翻译(弹窗原文区域覆盖中文)'
TextviewerInline.ATTRS.default_pos = {x = 0, y = 0}
TextviewerInline.ATTRS.default_enabled = true
TextviewerInline.ATTRS.viewscreens = {'viewscreen_textviewerst'}
TextviewerInline.ATTRS.frame = {l = 0, t = 0, r = 0, b = 0}

function TextviewerInline:onRenderFrame(dc, arg2, arg3)
    -- 双调用形态: overlay 框架 (painter, rect表) / 无头测试 (dc, cols, rows)
    local cols, rows = 80, 25
    if type(arg2) == 'table' and arg2.w then
        cols, rows = tonumber(arg2.w) or 80, tonumber(arg2.h) or 25
    elseif type(arg2) == 'number' then
        cols, rows = arg2, (type(arg3) == 'number' and arg3 or 25)
    end
    return fanyi_paint_textviewer(dc, cols, rows)
end

-- ===== ticket-014: 公告面板内嵌翻译覆盖层 =====================================

-- 公告译文缓存: get/set/expire(纯逻辑, 可无头测试; 与 013 共用测试组件) ----------

function fanyi_ann_cache_set(event_id, text)
    if not event_id or not text or #text == 0 then return false end
    S.ann_cache[event_id] = {
        text = text,
        ts = tonumber(now_ms()) or 0,
    }
    -- 有界: 超过 32 条时按时间序丢最老(面板同时只显 ≤3 条, 32 已宽裕)
    local keys = {}
    for k in pairs(S.ann_cache) do keys[#keys + 1] = k end
    if #keys > 32 then
        table.sort(keys, function(a, b)
            return (S.ann_cache[a].ts or 0) < (S.ann_cache[b].ts or 0)
        end)
        for i = 1, #keys - 32 do S.ann_cache[keys[i]] = nil end
    end
    return true
end

function fanyi_ann_cache_get(event_id)
    return S.ann_cache[event_id]
end

-- 过期(工单§3): ts 距 now 超过 ann_ttl_ms(默认 90s) → 移除,
-- overlay 不再显示该条(原文自然显示, 避免翻译永远挂)。返回清除条数。
function fanyi_ann_cache_gc(now)
    now = tonumber(now) or tonumber(now_ms()) or 0
    local dropped = 0
    for k, rec in pairs(S.ann_cache) do
        if now - (tonumber(rec.ts) or 0) > C.ann_ttl_ms then
            S.ann_cache[k] = nil
            dropped = dropped + 1
        end
    end
    return dropped
end

-- 可见公告条目(纯逻辑): 按时间倒序(最新在前)取最多 3 条(最新占满 + 历史 2 条)。
function fanyi_ann_visible_entries()
    local keys = {}
    for k in pairs(S.ann_cache) do keys[#keys + 1] = k end
    if #keys == 0 then return {} end
    table.sort(keys, function(a, b)
        return (S.ann_cache[a].ts or 0) > (S.ann_cache[b].ts or 0)
    end)
    local entries = {}
    for i = 1, math.min(#keys, 3) do entries[#entries + 1] = S.ann_cache[keys[i]] end
    return entries
end

-- 公告显示行构建(纯逻辑, 工单实现要点): 最新公告占满矩形(最多 4 行),
-- 历史公告每条只留首行(缩短, 避免新公告闪烁); 总行数 ≤ vis_rows, 截断保留最新。
function fanyi_ann_build_rows(entries, vis_cols, vis_rows)
    vis_cols = tonumber(vis_cols) or 20
    vis_rows = tonumber(vis_rows) or 5
    local rows = {}
    for idx, e in ipairs(entries or {}) do
        local text = (type(e) == 'table' and e.text) or ''
        if #text > 0 then
            local wrapped = fanyi_wrap_line(text, vis_cols)
            local limit = (idx == 1) and 4 or 1  -- 最新最多 4 行, 历史每条 1 行
            for i = 1, math.min(#wrapped, limit) do rows[#rows + 1] = wrapped[i] end
            if #rows >= vis_rows then break end
        end
    end
    while #rows > vis_rows do table.remove(rows) end  -- 保留顶部(最新)
    return rows
end

-- 公告面板矩形(0 基格坐标): DFHack 公开面板 API 难定位, 优先 getPanelLayout
-- 的 ANNOUNCEMENT 键(存在则精确), 否则已知相对位置兑底(右下角 Embark 按钮
-- 上方, 宽~40×高~10; 工单实现要点, ticket-016 E2E 可调)。
function fanyi_announcement_rect(max_cols, max_rows)
    max_cols = tonumber(max_cols) or 80
    max_rows = tonumber(max_rows) or 25
    local ok, box = pcall(function()
        local g = dfhack.gui or {}
        if g.getPanelLayout then
            local layout = g.getPanelLayout()
            local panel = layout and (layout.ANNOUNCEMENT or layout.announcement)
            if type(panel) == 'table' and panel.x1 and panel.y1
                and panel.x2 and panel.y2 then
                return {x = panel.x1, y = panel.y1,
                        w = panel.x2 - panel.x1 + 1, h = panel.y2 - panel.y1 + 1}
            end
        end
        error('announcement panel layout unavailable')
    end)
    -- 2026-09-12 修复①: DFHack 53.06 的 getPanelLayout() 只返回 map 键
    -- (TEXTVIEWER/ANNOUNCEMENT 是 53.13+ 才有), 旧 fallback 退到右下角
    -- 40×10 固定区域 = 假"字幕条"+周围英文 = 用户看到的混杂。拿不到真实
    -- 面板矩形时宁缺毋滥: 返回 nil, 调用方静默原文(dfint 词级翻译兜底)。
    if ok and type(box) == 'table' and box.w and box.h and box.w > 0 and box.h > 0 then
        return box
    end
    return nil
end

-- 贴图绘制(纯逻辑, dc 由调用方注入; 返回绘制格数): 公告面板矩形内贴最新公告
-- 中文(最新在前, 历史 2 条缩短显示); 门控关闭/图集未装/缓存空(TTL 已过)时
-- 返回 0(§2.3 静默原文)。复用 013 字形图集(S.tv_font 同一实例)。
function fanyi_paint_announcement(dc, max_cols, max_rows)
    if not S.ann_on then return 0 end
    if not dc then return 0 end
    fanyi_ann_cache_gc()  -- 每帧先清过期(90s TTL, 工单§3)
    if not S.tv_font.installed then
        if S.tv_font.tried then return 0 end  -- 每会话只试一次(与 013 共用标记)
        S.tv_font.tried = true
        fanyi_tv_font_load()
        if not S.tv_font.installed then return 0 end
    end
    local entries = fanyi_ann_visible_entries()
    if #entries == 0 then return 0 end
    local rect = fanyi_announcement_rect(max_cols, max_rows)
    if not rect then return 0 end  -- 修复①: 无真实面板矩形 → 不画(消假字幕条)
    local scale = S.tv_font.scale or 1
    local vis_cols = math.floor(rect.w / scale)
    local vis_rows = math.floor(rect.h / scale)
    local rows = fanyi_ann_build_rows(entries, vis_cols, vis_rows)
    if #rows == 0 then return 0 end
    return paint_cjk_rows(dc, rows, rect)
end

-- 公告 inline_translate 待发请求冲刷(工单§4: 复用 013 路由, context=
-- 'announcement'; 公告短文本无需切句)。常规 translate 队列已覆盖的(S.sent
-- 已登记)不再双发(预算铁律: 同文本不双发)。
local function ann_flush_inline_requests()
    if not S.engine_online then return end
    local client = S.client
    if not client then return end
    for event_id, req in pairs(S.ann_want_inline) do
        if not S.ann_inline_sent[event_id] and S.sent[event_id] == nil then
            S.ann_inline_sent[event_id] = true
            local ok = send_line(client, rpc_request('inline_translate', {
                text = req.text, context = 'announcement',
                title = '', event_id = event_id,
            }))
            if not ok then
                S.ann_inline_sent[event_id] = nil
                disconnect('ann inline send failed')
                return
            end
        end
    end
    S.ann_want_inline = {}
    local keys = {}
    for k in pairs(S.ann_inline_sent) do keys[#keys + 1] = k end
    if #keys > 128 then
        for i = 1, #keys - 64 do S.ann_inline_sent[keys[i]] = nil end
    end
end

-- overlay 小部件(官方 overlay 插件, 名字 fanyi.announcement): 仅在主游戏视图
-- (Dwarf/Adventure Mode) 渲染; 全屏裁剪框 + 内部按公告面板矩形自定位,
-- 不拦截鼠标(overlay 默认 click-through, 避免与 Embark 按钮冲突)。
AnnouncementInline = defclass(AnnouncementInline, overlay.OverlayWidget)
AnnouncementInline.ATTRS = AnnouncementInline.ATTRS or {}
AnnouncementInline.ATTRS.desc = 'DF-FanYi 公告面板内嵌翻译(矩形内覆盖中文)'
AnnouncementInline.ATTRS.default_pos = {x = 0, y = 0}
AnnouncementInline.ATTRS.default_enabled = true
AnnouncementInline.ATTRS.viewscreens = {'dwarfmode', 'adventur'}
AnnouncementInline.ATTRS.frame = {l = 0, t = 0, r = 0, b = 0}

function AnnouncementInline:onRenderFrame(dc, arg2, arg3)
    -- 双调用形态: overlay 框架 (painter, rect表) / 无头测试 (dc, cols, rows)
    local cols, rows = 80, 25
    if type(arg2) == 'table' and arg2.w then
        cols, rows = tonumber(arg2.w) or 80, tonumber(arg2.h) or 25
    elseif type(arg2) == 'number' then
        cols, rows = arg2, (type(arg3) == 'number' and arg3 or 25)
    end
    return fanyi_paint_announcement(dc, cols, rows)
end

-- overlay 插件扫描脚本全局 OVERLAY_WIDGETS 注册小部件
-- (名字: fanyi.textviewer / fanyi.announcement)
-- ===== ticket-017: 屏上选词翻译 (MouseSelect) =====================================
--
-- F11 触发: 读鼠标所在行字符 → 调引擎 inline_translate → 浮窗驻留显示中文
-- F12 关闭: 状态清空, 浮窗消失

-- 屏幕字符读取(gps.screen[y][x], 1-based; 滤颜色码/控制符, 仅保留可打印 + 空格)
-- 读矩形区域文本: 用 dfhack.screen.readTile 而非 gps.screen:
-- DF50 gps.screen 是 3 层字节数组(ch/fg/bg/bold), Lua 侧无法按"字符表"索引;
-- readTile(x, y, true) penetrate_ui=true 可穿透 DFHack overlay 读到游戏原生文本
-- (否则会读到我们自己的中文 overlay, 翻译已译文本无意义)。
-- 读矩形区域内文本(x1<=x2, y1<=y2, 0 基)。严格只收可打印 ASCII(0x20-0x7E):
-- DF50 地图区是图形精灵渲染, readTile 返回的 ch 是精灵索引/任意码,
-- 不过滤会拼出乱码。非文本区自然读出空串。
local function fanyi_ms_read_rect(x1, y1, x2, y2)
    local rows = {}
    for y = y1, y2 do
        local chars, last_space = {}, true
        for x = x1, x2 do
            local code = 0
            pcall(function()
                local t = dfhack.screen.readTile(x, y, true)
                code = tonumber(t and t.ch) or 0
            end)
            local ch = ' '
            if code >= 32 and code <= 126 then ch = string.char(code) end
            if ch == ' ' then
                if not last_space then chars[#chars+1] = ' '; last_space = true end
            else
                chars[#chars+1] = ch; last_space = false
            end
        end
        while #chars > 0 and chars[#chars] == ' ' do chars[#chars] = nil end
        local i = 1
        while i <= #chars and chars[i] == ' ' do i = i + 1 end
        local line = table.concat(chars, '', i)
        if line ~= '' then rows[#rows+1] = line end
    end
    return table.concat(rows, ' ')
end
-- F11 命令: 进入框选模式(不再立即翻译; 拖框释放后读选区文本)
function fanyi_mouseselect_cmd()
    if not S.engine_online then
        -- 退路: 状态仍点亮浮窗, 让用户看到离线提示
        S.ms = {visible=true, text='', translation='[离线] 翻译引擎未连接, 请检查 DF-FanYi 桥。',
                event_id=nil, rows={'[离线] 翻译引擎未连接'}, rect=nil, started=dfhack.getTickCount()}
        return
    end
    S.ms = {visible=true, selecting=true, drag=nil,
            text='', translation='', event_id=nil,
            rows={'拖框选择要翻译的文本', '右键或 F12 取消'},
            rect=nil, started=dfhack.getTickCount()}
end

-- 框选释放: 读选区文本并发起 inline 翻译(由 MouseSelect:onRenderFrame 帧驱动调用)
function fanyi_ms_select_rect(x1, y1, x2, y2)
    local seg = fanyi_ms_read_rect(x1, y1, x2, y2)
    if #seg > 256 then seg = seg:sub(1, 256) end
    if seg == '' or #seg < 2 then
        S.ms.selecting = false
        S.ms.drag = nil
        S.ms.visible = true
        S.ms.rows = {'[无文本可翻译]', '选区内没有可识别的英文(地图区是图形无文本)'}
        return
    end
    local event_id = 'ms-'..tostring(dfhack.getTickCount())
    local payload = json.encode({jsonrpc='2.0', id=event_id, method='inline_translate',
        params={text=seg, context='mouse_select', title='', event_id=event_id}})
    local ok = S.client and send_line(S.client, payload)
    S.ms.selecting = false
    S.ms.drag = nil
    if not ok then
        S.ms = {visible=true, text=seg, translation='[发送失败] 翻译请求未送出, 可能是断线重连中。',
                event_id=nil, rows={seg, '[发送失败]'}, rect=nil, started=dfhack.getTickCount()}
        return
    end
    -- 记录请求, 等 fetch_done 回调填回译文
    S.ms = {visible=true, text=seg, translation='', event_id=event_id,
            rows={seg, '[翻译中...]'}, rect=nil, started=dfhack.getTickCount()}
    S.ms_pending = S.ms_pending or {}
    S.ms_pending[event_id] = seg
end

-- F12 命令: 关闭浮窗并取消框选
function fanyi_mousedismiss_cmd()
    S.ms.visible = false
    S.ms.selecting = false
    S.ms.drag = nil
    S.ms.text = ''
    S.ms.translation = ''
    S.ms.event_id = nil
    S.ms.rows = {}
end

-- 从 fetch_done 收到的事件中检查是否属于 mouseselect, 并填充译文
function fanyi_ms_apply_translation(event_id, translated_text)
    if not S.ms_pending or not S.ms_pending[event_id] then return end
    if S.ms.event_id ~= event_id then return end
    local txt = translated_text or ''
    if txt == '' then txt = '[译文为空]' end
    -- 简单 wrap(中文 2 字符/格, 假设每行 40 屏宽)
    local cols = 40
    local out = {S.ms.text or ''}
    -- 中文 wrap: 每个全角算 2, 半角算 1
    local line, used = '', 0
    for i = 1, #txt do
        local b = txt:byte(i)
        local is_full = b >= 0xE0 or (b >= 0x81 and b <= 0xFE) -- 简化: 含多字节都按 2 计
        local w = is_full and 2 or 1
        if used + w > cols then
            out[#out+1] = line; line = ''; used = 0
        end
        line = line .. txt:sub(i,i); used = used + w
    end
    if line ~= '' then out[#out+1] = line end
    out[#out+1] = ''
    out[#out+1] = '[F12] 关闭'
    S.ms.translation = txt
    S.ms.rows = out
    S.ms_pending[event_id] = nil
end

-- 渲染浮窗(右下角固定位置)
local function fanyi_paint_mouseselect(dc, max_cols, max_rows)
    if not S.ms or not S.ms.visible then return 0 end
    if not dc then return 0 end
    local rows = S.ms.rows or {}
    if #rows == 0 then return 0 end
    -- 尺寸: 宽 50, 高 #rows + 2 边框
    local w = math.min(60, math.max(20, max_cols // 2))
    local h = math.min(#rows + 2, max_rows - 2)
    if h < 3 then return 0 end
    -- 右下角对齐
    local x = math.max(1, max_cols - w - 1)
    local y = math.max(1, max_rows - h - 1)
    local rect = {x = x, y = y, w = w, h = h}
    -- 涂底(全物理格, 复用 2026-09-12 修复逻辑)
    pcall(function() dc:pen(7, 0) end)
    for ri = 0, rect.h - 1 do
        local yy = rect.y + ri
        for ci = 0, rect.w - 1 do
            pcall(function() dc:seek(rect.x + ci, yy):tile(' ', 0) end)
        end
    end
    -- 边框(简单 fg=white 占位)
    pcall(function() dc:pen(7, 0):seek(rect.x, rect.y):string(string.rep('-', rect.w)) end)
    pcall(function() dc:pen(7, 0):seek(rect.x, rect.y + rect.h - 1):string(string.rep('-', rect.w)) end)
    -- 内容行: 中文必须走 CJK 字形图集(paint_cjk_rows),
    -- 直接 dc:string 是 CP437 字体画不了 CJK → 乱码(2026-09-12 实机确认)
    local inner = {x = rect.x + 1, y = rect.y + 1, w = rect.w - 2, h = rect.h - 2}
    local painted = paint_cjk_rows(dc, rows, inner)
    if painted == 0 then
        -- 图集未装时的兜底(纯 ASCII 场景)
        for i, line in ipairs(rows) do
            local yy = rect.y + i
            if yy >= rect.y + rect.h - 1 then break end
            local s = line:sub(1, rect.w - 2)
            pcall(function() dc:pen(7, 0):seek(rect.x + 1, yy):string(s) end)
        end
    end
    S.ms.rect = rect
    return rect.h
end

MouseSelect = defclass(MouseSelect, overlay.OverlayWidget)
MouseSelect.ATTRS = MouseSelect.ATTRS or {}
MouseSelect.ATTRS.desc = 'DF-FanYi 屏上框选翻译(F11 框选, F12 关闭)'
MouseSelect.ATTRS.default_pos = {x = 0, y = 0}
MouseSelect.ATTRS.default_enabled = true
MouseSelect.ATTRS.viewscreens = {'dwarfmode', 'dungeonmode', 'default', 'adventur',
    'adventur_interact', 'textviewer', 'title'}
MouseSelect.ATTRS.frame = {l = 0, t = 0, r = 0, b = 0}

-- 框选状态机(帧驱动, 不走 onInput):
-- DFHack 无 _MOUSE_L_UP 事件, 释放检测靠 enabler.mouse_lbut 状态跳变(1→0)。
-- 每帧: 选择模式+按下→锚定; 拖动→更新终点+反色高亮; 释放→读选区发起翻译。
local function fanyi_ms_selection_tick()
    if not S.ms or not S.ms.selecting then return end
    local mx, my = -1, -1
    pcall(function() mx, my = dfhack.screen.getMousePos() end)
    mx, my = tonumber(mx) or -1, tonumber(my) or -1
    local lbut = tonumber(df.global.enabler and df.global.enabler.mouse_lbut) or 0
    local rbut = tonumber(df.global.enabler and df.global.enabler.mouse_rbut) or 0
    if rbut > 0 then
        -- 右键取消
        S.ms.selecting, S.ms.drag, S.ms.visible = false, nil, false
        S.ms.rows = {}
        return
    end
    if S.ms.drag then
        if mx >= 0 then S.ms.drag.x2, S.ms.drag.y2 = mx, my end
        if lbut == 0 then
            local d = S.ms.drag
            S.ms.drag = nil
            local x1, x2 = math.min(d.x1, d.x2), math.max(d.x1, d.x2)
            local y1, y2 = math.min(d.y1, d.y2), math.max(d.y1, d.y2)
            pcall(fanyi_ms_select_rect, x1, y1, x2, y2)
        end
    elseif lbut > 0 and mx >= 0 then
        S.ms.drag = {x1 = mx, y1 = my, x2 = mx, y2 = my}
    end
end

-- 拖框实时反色高亮(逐格 swap fg/bg, 视觉与 DF 原生选区一致)
local function fanyi_ms_paint_drag(dc)
    if not S.ms or not S.ms.drag then return end
    local d = S.ms.drag
    local x1, x2 = math.min(d.x1, d.x2), math.max(d.x1, d.x2)
    local y1, y2 = math.min(d.y1, d.y2), math.max(d.y1, d.y2)
    for y = y1, y2 do
        for x = x1, x2 do
            pcall(function()
                local t = dfhack.screen.readTile(x, y, false)
                if t then
                    dfhack.screen.paintTile({ch = t.ch, fg = t.bg, bg = t.fg}, x, y)
                end
            end)
        end
    end
end

function MouseSelect:onInput(keys)
    -- 框选模式中吞掉鼠标事件, 防止拖框误触游戏 UI/其他 DFHack widget
    if not S.ms or not S.ms.selecting then return false end
    if keys._MOUSE_L or keys._MOUSE_L_DOWN or keys._MOUSE_R or keys._MOUSE_R_DOWN
        or keys._MOUSE_M or keys._MOUSE_M_DOWN then
        return true
    end
    return false
end

function MouseSelect:onRenderFrame(dc, arg2, arg3)
    local cols, rows = 80, 25
    if type(arg2) == 'table' and arg2.w then
        cols, rows = tonumber(arg2.w) or 80, tonumber(arg2.h) or 25
    elseif type(arg2) == 'number' then
        cols, rows = arg2, (type(arg3) == 'number' and arg3 or 25)
    end
    fanyi_ms_selection_tick()
    fanyi_ms_paint_drag(dc)
    return fanyi_paint_mouseselect(dc, cols, rows)
end

OVERLAY_WIDGETS = {textviewer = TextviewerInline, announcement = AnnouncementInline,
                    mouse_select = MouseSelect}

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
            -- ticket-013: textviewer inline_translate 待发请求(引擎在线时)
            if S.client and S.engine_online then
                pcall(tv_flush_inline_requests)
            end
            -- ticket-014: announcement inline_translate 待发请求(同 context)
            if S.client and S.engine_online then
                pcall(ann_flush_inline_requests)
            end
        end
    end

    -- 公告缓存 TTL 过期(90s, 工单§3): 每 50 tick 扫一次
    if S.tick % 50 == 0 then
        pcall(fanyi_ann_cache_gc)
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
        ('  subtitle widget: removed (replaced by inline overlays) — '
            .. 'announcement_inline: ticket-014 (pending)'),
        ('  textviewer_inline (%s) — 弹窗原文区域覆盖中文 (ticket-013)'):format(
            S.tv_on and 'on' or 'off'),
        ('  announcement_inline (%s) — 公告面板矩形覆盖中文 (ticket-014)'):format(
            S.ann_on and 'on' or 'off'),
    }
    local n_tv = 0
    for _ in pairs(S.seen_tv_hashes) do n_tv = n_tv + 1 end
    table.insert(lines, ('  文本弹窗捕获: 已见 %d 页 (调试日志: %s)'):format(
        n_tv, tostring(S.debug_log.path)))
    local n_ann = 0
    for _ in pairs(S.ann_cache) do n_ann = n_ann + 1 end
    table.insert(lines, ('  公告译文缓存: %d 条 (TTL %dms)'):format(
        n_ann, C.ann_ttl_ms))
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
        -- ticket-017: 注册 F11/F12 全局热键(选词/关闭浮窗)
        pcall(function() dfhack.run_command('keybinding add F11@dwarfmode|dungeonmode|default|adventur|adventur_interact|textviewer "fanyi mouseselect"') end)
        pcall(function() dfhack.run_command('keybinding add F12@dwarfmode|dungeonmode|default|adventur|adventur_interact|textviewer "fanyi mousedismiss"') end)
        pcall(function() dfhack.run_command('overlay', 'enable', 'fanyi.mouse_select') end)
        flog('fanyi start (捕获: 公告+游戏日志+文本弹窗; F11 选词/F12 关闭)')
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
            elseif which == 'textviewer' then
                S.tv_on = true
                local ok, err = fanyi_tv_font_load()
                pcall(function()
                    dfhack.run_command('overlay', 'enable', 'fanyi.textviewer')
                end)
                if ok then
                    print('textviewer_inline 已开启: 弹窗原文区域将覆盖中文'
                        .. '(缺字形的字回退英文; 重开同页命中缓存秒回)')
                else
                    print('textviewer_inline 已开启(图集未装: ' .. tostring(err)
                        .. '; 弹窗显示英文原文)')
                end
            elseif which == 'announcement' then
                S.ann_on = true
                local ok, err = fanyi_tv_font_load()
                pcall(function()
                    dfhack.run_command('overlay', 'enable', 'fanyi.announcement')
                end)
                if ok then
                    print('announcement_inline 已开启: 公告面板矩形内覆盖中文'
                        .. '(TTL ' .. tostring(C.ann_ttl_ms) .. 'ms, 缺字形回退英文)')
                else
                    print('announcement_inline 已开启(图集未装: ' .. tostring(err)
                        .. '; 公告面板显示英文原文)')
                end
            else
                print('用法: fanyi overlays on|off [textviewer|announcement]')
            end
        elseif sub == 'off' then
            if which == 'cjk' then
                print('字幕条已删除 (ticket-012), 无需关闭.')
            elseif which == 'textviewer' then
                S.tv_on = false
                S.tv_cache = {}
                S.tv_want_inline = {}
                pcall(function()
                    dfhack.run_command('overlay', 'disable', 'fanyi.textviewer')
                end)
                print('textviewer_inline 已关闭(弹窗显示英文原文)')
            elseif which == 'announcement' then
                S.ann_on = false
                S.ann_cache = {}
                S.ann_want_inline = {}
                pcall(function()
                    dfhack.run_command('overlay', 'disable', 'fanyi.announcement')
                end)
                print('announcement_inline 已关闭(公告面板显示英文原文, 缓存已清空)')
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
        S.ann_cache = {}  -- ticket-014: 公告译文缓存一并清空
        S.stats.captured = 0
        print('已清空显示/去重状态')
    elseif cmd == 'mouseselect' then
        -- ticket-017: F11 触发, 读鼠标所在行字符 → 调 inline_translate → 浮窗驻留
        pcall(function() fanyi_mouseselect_cmd() end)
        arm_timer()
    elseif cmd == 'mousedismiss' then
        -- ticket-017: F12 关闭浮窗
        fanyi_mousedismiss_cmd()
        -- 脚本被 loadfile 重载后 timer 仍指向旧 chunk 的 tick, 重新 arm 用本 chunk
        arm_timer()
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
                            内嵌 overlay 开关 (字幕条已删除 ticket-012;
                            textviewer: 弹窗原文区域覆盖中文, ticket-013)
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