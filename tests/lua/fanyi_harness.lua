-- DF-FanYi 无头测试装备: 在系统 lua5.4 下模拟 DFHack 环境, dofile 真实 fanyi.lua,
-- 以帧步进驱动其主循环, 由 pytest 以子进程方式运行(逐场景)。
-- 用法: run_fanyi_tests.lua <scenario> <repo_root> <tmpdir>
local path = {...}

-- ---------- 最小 defclass(gui.class 同义实现, 供 overlay widget 类定义) ----------
-- 支持: defclass(名字(可为nil), 父类)、ATTRS 合成、cls{attrs} 构造、方法沿父链解析。
local function defclass(name, parent)
    local cls = {}
    cls.__parent = parent
    cls.ATTRS = {}
    cls.__index = cls
    setmetatable(cls, {
        __call = function(c, attrs)
            local obj = setmetatable({}, c)
            local defaults = {}
            local p = c
            while p do
                for k, v in pairs(p.ATTRS or {}) do
                    if defaults[k] == nil then defaults[k] = v end
                end
                p = p.__parent
            end
            for k, v in pairs(defaults) do obj[k] = v end
            for k, v in pairs(attrs or {}) do obj[k] = v end
            if obj.init then obj:init(attrs) end
            return obj
        end,
        __index = function(c, k)
            local p = rawget(c, '__parent')
            while p do
                local v = rawget(p, k)
                if v ~= nil then return v end
                p = rawget(p, '__parent')
            end
            return nil
        end,
    })
    return cls
end

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
    df_version = 'v0.53.06 STEAM win64',
    dfhack_version = '53.06-r1.1',
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
    run_commands = {},    -- dfhack.run_command 记录 {'overlay','enable','fanyi.subtitle'}
    texture_loads = {},   -- dfhack.textures.loadTileset 参数记录
    rescan_calls = 0,
}

function M.set_version(dfver, dhver)
    M.df_version = dfver or '53.16'
    M.dfhack_version = dhver or '53.16-r1.1'
end

function M.add_report(id, text)
    M.reports[id] = {text = text}
end

-- 设置当前 viewscreen 为 textviewer 弹窗(ticket-013 捕获/渲染无头测试用)
function M.set_textviewer(title, lines)
    local tv = {
        __is_textviewer = true,
        title = title,
        text = lines or {},
        parent = nil,
    }
    setmetatable(tv, {__tostring = function() return '<viewscreen_textviewerst:0xmock>' end})
    M.cur_viewscreen = tv
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
    getHackPath = function() return M.tmpdir .. '/hack/' end,
    df2console = function(s) return s end,
    run_command = function(...)
        local argv = {}
        for i = 1, select('#', ...) do argv[i] = tostring(select(i, ...)) end
        M.run_commands[#M.run_commands + 1] = argv
    end,
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
    -- onStateChange hook 表(fanyi.lua fanyi_install_state_hooks 需要)
    onStateChange = {},
}
-- ticket-013: textviewer 弹窗 mock(getCurViewscreen + viewscreen 判型由 env.df 提供)
env.dfhack.gui = {
    getCurViewscreen = function() return M.cur_viewscreen end,
    -- 2026-09-12: mock 53.13+ 面板布局(修复①后无矩形则不画; 无头测试需供给矩形)
    getPanelLayout = function()
        return {
            map = {x1 = 0, y1 = 0, x2 = 79, y2 = 24},
            ANNOUNCEMENT = {x1 = 1, y1 = 18, x2 = 40, y2 = 24},
        }
    end,
}
env.dfhack_flags = {}
env.df = {
    report = {
        find = function(id) return M.reports[id] end,
    },
    viewscreen_textviewerst = {
        is_instance = function(_, obj)
            return type(obj) == 'table' and obj.__is_textviewer == true
        end,
    },
}
-- dfhack.textures mock: loadTileset 返回无限句柄表(句柄=base+i),
-- getTexposByHandle 确定性映射 500000+handle → 测试可反推每个 cp 的 texpos。
env.dfhack.textures = {
    next_handle = 100,
    loadTileset = function(path, w, h, is32)
        local base = env.dfhack.textures.next_handle
        env.dfhack.textures.next_handle = base + 100000
        M.texture_loads[#M.texture_loads + 1] = {path = path, tile_w = w, tile_h = h}
        return setmetatable({}, {__index = function(_, i) return base + i end})
    end,
    getTexposByHandle = function(handle) return 500000 + handle end,
}

-- plugins.overlay mock: OverlayWidget 基类 + rescan 计数
local overlay_mod = {
    OverlayWidget = defclass(nil, nil),
    rescan = function() M.rescan_calls = M.rescan_calls + 1 end,
}
package.preload['plugins.overlay'] = function() return overlay_mod end

env.defclass = defclass
env.overlay = overlay_mod  -- 便于测试直接取 OverlayWidget 基类

-- ---------- 测试辅助: 字形图集安装 / mock painter ----------

-- 在 tmpdir/hack/data/<subdir>/ 写假图集(真实 PNG 二进制不重要 — textures 已 mock)
function M.install_font(pages, tile_w, tile_h, subdir, scale)
    subdir = subdir or 'fanyi-font'
    scale = scale or 1
    tile_w = tile_w or 8
    tile_h = tile_h or 12
    local dir = M.tmpdir .. '/hack/data/' .. subdir .. '/'
    os.execute('mkdir -p "' .. dir .. '"')
    for name, cps in pairs(pages) do
        local f = assert(io.open(dir .. name, 'wb'))
        f:write('FAKEPNG')
        f:close()
    end
    local page_list = {}
    for name, cps in pairs(pages) do
        page_list[#page_list + 1] = {png = name, cols = 64, rows = 64,
                                     count = #cps, cps = cps}
    end
    table.sort(page_list, function(a, b) return a.png < b.png end)
    local f = assert(io.open(dir .. 'index.json', 'w'))
    f:write(json.encode({version = 1, tile_w = tile_w, tile_h = tile_h,
                         scale = scale, pages = page_list}))
    f:close()
    return dir
end

-- mock painter: 记录 tile 贴图调用 {x, y, texpos}; seek 链式
function M.make_dc()
    local dc = {tiles = {}, texts = {}, x = 0, y = 0}
    function dc:seek(x, y) self.x = x; self.y = y; return self end
    function dc:tile(ch, texpos, pen)
        self.tiles[#self.tiles + 1] = {x = self.x, y = self.y, texpos = texpos, ch = ch}
        return self
    end
    function dc:string(text, pen)
        self.texts[#self.texts + 1] = {x = self.x, y = self.y, text = text}
        return self
    end
    return dc
end

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

-- overlay: ticket-009 起 fanyi.lua require('plugins.overlay') 并定义 OVERLAY_WIDGETS;
-- 上方已 preload mock(OverlayWidget 基类 + rescan 计数), 无需真实 widget 框架。

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
M.env = env_saved  -- 脚本全局(如 OVERLAY_WIDGETS / fanyi_utf8_codepoints)

return M