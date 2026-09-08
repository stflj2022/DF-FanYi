-- DF-FanYi 无头测试装备: 在系统 lua5.4 下模拟 DFHack 环境, dofile 真实 fanyi.lua,
-- 以帧步进驱动其主循环, 由 pytest 以子进程方式运行(逐场景)。
-- 用法: run_fanyi_tests.lua <scenario> <repo_root> <tmpdir>
local path = {...}

-- ---------- 最小 JSON 编码/解码(模拟 DFHack 捆绑 json) ----------
local json = {}

local function qs(s)
    local out = {'"'}
    for i = 1, #s do
        local b = s:byte(i)
        local c = s:sub(i, i)
        if b == 34 then out[#out+1] = '\\"'          -- "
        elseif b == 92 then out[#out+1] = '\\\\'     -- backslash
        elseif b == 10 then out[#out+1] = '\\n'
        elseif b == 13 then out[#out+1] = '\\r'
        elseif b == 9 then out[#out+1] = '\\t'
        elseif b < 32 then out[#out+1] = ('\\u%04x'):format(b)
        else out[#out+1] = c end
    end
    out[#out+1] = '"'
    return table.concat(out)
end

function json.encode(v)
    local t = type(v)
    if t == 'nil' then return 'null'
    elseif t == 'boolean' then return v and 'true' or 'false'
    elseif t == 'number' then
        if v ~= v or v == math.huge or v == -math.huge then return 'null' end
        return ('%g'):format(v)
    elseif t == 'string' then return qs(v)
    elseif t == 'table' then
        local isarr = true
        for k in pairs(v) do
            if type(k) ~= 'number' or k < 1 or k > #v or math.floor(k) ~= k then isarr = false break end
        end
        if #v > 0 and isarr then
            local parts = {}
            for i = 1, #v do parts[i] = json.encode(v[i]) end
            return '[' .. table.concat(parts, ',') .. ']'
        end
        local parts = {}
        for k, val in pairs(v) do
            parts[#parts+1] = json.encode(tostring(k)) .. ':' .. json.encode(val)
        end
        return '{' .. table.concat(parts, ',') .. '}'
    end
    return 'null'
end

local P = {pos = 1}
local function skipws() while P.s:match('^%s', P.pos) do P.pos = P.pos + 1 end end
local function parse_val()
    skipws()
    local s = P.s
    local c = s:sub(P.pos, P.pos)
    if c == '{' then
        P.pos = P.pos + 1
        local t = {}
        skipws()
        if s:sub(P.pos, P.pos) == '}' then P.pos = P.pos + 1 return t end
        while true do
            skipws()
            local k = parse_val()
            skipws()
            assert(s:sub(P.pos, P.pos) == ':', 'json: expect :')
            P.pos = P.pos + 1
            t[k] = parse_val()
            skipws()
            local d = s:sub(P.pos, P.pos)
            if d == ',' then P.pos = P.pos + 1
            elseif d == '}' then P.pos = P.pos + 1 return t
            else error('json: expect }') end
        end
    elseif c == '[' then
        P.pos = P.pos + 1
        local t = {}
        skipws()
        if s:sub(P.pos, P.pos) == ']' then P.pos = P.pos + 1 return t end
        local i = 0
        while true do
            i = i + 1
            t[i] = parse_val()
            skipws()
            local d = s:sub(P.pos, P.pos)
            if d == ',' then P.pos = P.pos + 1
            elseif d == ']' then P.pos = P.pos + 1 return t
            else error('json: expect ]') end
        end
    elseif c == '"' then
        local out = {}
        P.pos = P.pos + 1
        while true do
            local ch = s:sub(P.pos, P.pos)
            if ch == '"' then P.pos = P.pos + 1 return table.concat(out) end
            if ch == '\\' then
                local e = s:sub(P.pos+1, P.pos+1)
                if e == 'n' then out[#out+1] = '\n'
                elseif e == 'r' then out[#out+1] = '\r'
                elseif e == 't' then out[#out+1] = '\t'
                elseif e == 'u' then
                    local hex = s:sub(P.pos+2, P.pos+5)
                    local cp = tonumber(hex, 16)
                    out[#out+1] = utf8.char(cp)
                    P.pos = P.pos + 5
                else out[#out+1] = e end
                P.pos = P.pos + 1
            else
                out[#out+1] = ch
                P.pos = P.pos + 1
            end
        end
    elseif c == 't' then P.pos = P.pos + 4 return true
    elseif c == 'f' then P.pos = P.pos + 5 return false
    elseif c == 'n' then P.pos = P.pos + 4 return nil
    else
        local val = s:match('^-?%d+%.?%d*[eE]?[+-]?%d*', P.pos)
        assert(val, 'json: bad number @' .. string.sub(s, P.pos, P.pos+20))
        P.pos = P.pos + #val
        return tonumber(val)
    end
end

function json.decode(s)
    P = {s = s, pos = 1}
    local v = parse_val()
    skipws()
    assert(P.pos > #P.s, 'json: trailing data')
    return v
end
-- --------------------------------------------------------------

-- ---------- 模拟 DFHack 环境 ----------
local M = {
    df_version = '53.16',
    dfhack_version = '53.16-r1.1',
    engine_up = false,
    sent_lines = {},
    server_buffer = '',
    reports = {},
    tmpdir = nil,
    output = {},
    tick = 0,
    euid = 0,
    frames = {},          -- {euid=, left=, fn=}
    auto_enable = true,
    events_out = {},      -- 测试可断言: 抓到的 TextEvent(引擎侧收到的)
}

function M.set_version(dfver, dhver)
    M.df_version = dfver or '53.16'
    M.dfhack_version = dhver or '53.16-r1.1'
end

function M.add_report(id, text)
    M.reports[id] = {text = text}
end

function M.emit_report(id)
    M.eventful.onReport.fanyi(id)
end

-- 引擎(服务器)侧: 收到一行请求
local function engine_on_line(line)
    local req = json.decode(line)
    local ev = req.method == 'translate' and req.params or nil
    if ev then
        M.events_out[#M.events_out + 1] = ev
    end
    return req
end

-- 测试辅助: 让 mock 服务器处理当前已收到的全部请求并自动应答,
-- done_text=翻译文本, confidence=置信度; 返回收到的请求数
function M.engine_auto_respond(done_text, confidence)
    local n = 0
    for _, line in ipairs(M.sent_lines) do
        local req = engine_on_line(line)
        n = n + 1
        if req.method == 'translate' then
            local ev = req.params
            local result = {status='done', event_id=ev.event_id,
                            translated_text=done_text or '译文' .. tostring(ev.event_id),
                            confidence=confidence or 0.9,
                            model='fake', provider='fake'}
            M.server_respond({jsonrpc='2.0', id=req.id, result=result})
        else
            -- fetch_done: 回放已 done 的事件(由测试通过 M.done_records 提供)
            local recs = {}
            for _, ev in ipairs(M.events_out) do
                local seen = false
                for _, r in ipairs(M.done_records or {}) do if r.event_id == ev.event_id then seen = true break end end
                if not seen then
                    recs[#recs+1] = {status='done', event_id=ev.event_id,
                                     translated_text=done_text or ('译文' .. ev.event_id),
                                     confidence=confidence or 0.9, model='fake', provider='fake'}
                    M.done_records = M.done_records or {}
                    M.done_records[#M.done_records+1] = recs[#recs]
                end
            end
            M.server_respond({jsonrpc='2.0', id=req.id, result={translations=recs}})
        end
    end
    M.sent_lines = {}
    return n
end

function M.server_respond(obj)
    if type(obj) == 'string' then
        M.server_buffer = M.server_buffer .. obj
    else
        M.server_buffer = M.server_buffer .. json.encode(obj) .. '\n'
    end
end

-- frame 步进
function M.step(nframes)
    for _ = 1, nframes do
        M.tick = M.tick + 1
        local due = {}
        for i, ent in ipairs(M.frames) do
            ent.left = ent.left - 1
            if ent.left <= 0 then
                due[#due+1] = ent
                M.frames[i] = nil
            end
        end
        -- 重排(移除 nil)
        local compact = {}
        for _, ent in ipairs(M.frames) do if ent then compact[#compact+1] = ent end end
        M.frames = compact
        for _, ent in ipairs(due) do
            local ok, err = pcall(ent.fn)
            if not ok then
                M.runtime_error = err
                error('harness callback error: ' .. tostring(err), 0)
            end
        end
    end
end

-- ---------- 全局 mock 注入 ----------
local env = setmetatable({}, {__index = _G})

function env.print(...)
    local parts = {}
    for i = 1, select('#', ...) do parts[i] = tostring(select(i, ...)) end
    M.output[#M.output + 1] = table.concat(parts, '\t')
end

M.print = env.print

env.dfhack = {
    getDFVersion = function() return M.df_version end,
    getDFHackVersion = function() return M.dfhack_version end,
    getTickCount = function() return M.tick end,
    getDFPath = function() return M.tmpdir end,
    df2console = function(s) return s end,
    timeout = function(frames, unit, fn, id)
        M.euid = M.euid + 1
        M.frames[#M.frames + 1] = {euid = M.euid, left = frames, fn = fn, id = id}
        return M.euid
    end,
    timeout_active = function(euid, fn)
        for i, ent in ipairs(M.frames) do
            if ent.euid == euid then M.frames[i] = nil end
        end
        local compact = {}
        for _, ent in ipairs(M.frames) do if ent then compact[#compact+1] = ent end end
        M.frames = compact
    end,
}
env.dfhack_flags = {}
env.df = {
    report = {
        find = function(id) return M.reports[id] end,
    },
}
M.eventful = {
    eventType = {REPORT = 1},
    onReport = {},
    enableEvent = function(ev, n) M.eventful.enabled_event = ev end,
}
local function make_client()
    local client = {
        setNonblocking = function() end,
        send = function(self, data)
            if not M.engine_up then error('connection reset', 2) end
            M.sent_lines[#M.sent_lines + 1] = data
            return #data
        end,
        receive = function(self, pattern)
            -- 非阻塞字节读取模拟: 无完整行 → nil(EWOULDBLOCK 静默)
            if #M.server_buffer == 0 then return nil end
            local i = M.server_buffer:find('\n', 1, true)
            if not i then return nil end
            local line = M.server_buffer:sub(1, i - 1)
            M.server_buffer = M.server_buffer:sub(i + 1)
            return line
        end,
        close = function() end,
        isBlocking = function() return false end,
    }
    return client
end
local luasocket = {
    tcp = {
        connect = function(self, addr, port)
            if not M.engine_up then error('connection refused', 2) end
            return make_client()
        end,
    },
}
env.json = json
env._G = env  -- 脚本以 `_G.json` 方式访问 json; 让 _G 指向 mock env
env.fanyi_state = nil  -- script 自行创建

package.preload['plugins.eventful'] = function() return M.eventful end
package.preload['plugins.luasocket'] = function() return luasocket end

-- overlay 为惰性: 脚本目前只在 fanyi overlays on 时创建 widget? 否——脚本不实例化
-- overlay(渲染纯函数化), 故无需 mock overlay/widgets。

-- ---------- 装载真实脚本 ----------
local script_path = path[1]
local tmpdir = path[2]
M.tmpdir = tmpdir

local env_saved = env

function M.load(args, flags)
    -- 模拟一次 `fanyi ...` 命令(重新执行脚本文件; DFHack 每次命令即重新执行)
    env_saved.dfhack_flags = flags or {}
    local c = assert(loadfile(script_path, 'bt', env_saved))
    local ok, err = pcall(c, table.unpack(args or {}))
    if not ok then error('fanyi command error: ' .. tostring(err), 3) end
    return M
end

function M.run(args, flags) return M.load(args, flags) end

function M.output_joined()
    return table.concat(M.output, '\n')
end

M.json = json
M.state = function() return env_saved.fanyi_state end

return M