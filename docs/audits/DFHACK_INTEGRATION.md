# DFHACK_INTEGRATION — DFHack 捕获/渲染桥设计定论 (ticket-008)

日期: 2026-09-08 · 依据: ENVIRONMENT_AUDIT.md (ticket-001) + REUSE_PLAN.md (ticket-002)
+ 本机 DFHack 53.16-r1.1 源码与文档核对。本文件是 `dfhack/scripts/fanyi.lua`
与 `df_fanyi/bridge/`(Python 引擎侧)的设计依据。

## 1. 定论速览

| 议题 | 定论 |
|---|---|
| 捕获源 | **eventful.onReport(公告) + gamelog.txt 增量回溯(历史)**, 官方 Lua API |
| 渲染方式 | **overlay 插件 OverlayWidget + widgets.Label**; CJK 需字体贴图(ticket-009) |
| classic 字符集 | classic=CP437 位图字库 → CJK 走 **自定义 texture 贴图**, 不改原始字库 |
| 版本守卫 | DF==53.16 且 DFHack==53.16-r1.* 否则 **Safe Mode**(仅诊断, §43-44) |
| 传输 | **loopback TCP 127.0.0.1:17486**(DFHack 捆绑 luasocket 仅支持 TCP, 见 §5) |
| 引擎宕机 | **静默原文 + 指数退避重连**(§2.3), 游戏主循环零阻塞(§29) |

## 2. 捕获: 两个来源互补

### 2.1 公告(announcement) → eventful.onReport

- `require('plugins.eventful')`; `eventful.enableEvent(eventful.eventType.REPORT, 1)`
  后 `eventful.onReport.<名>(report_id)` 逐条回调(参考官方 `devel/annc-monitor.lua`)。
- 回调里 `df.report.find(id)` 取 `.text`, 经 `dfhack.df2console()` 做
  CP437→UTF-8 转换后作为 `source_text`。
- **去重**: report id 对 64 取模的环形集合(游戏内部可能重复回调同一 id)。
- 映射: `text_type=ANNOUNCEMENT`, `priority=80`(§6 公告档), `screen='announcement'`。

### 2.2 历史(gamelog.txt) → 增量 tail

- DF 持续把世界事件写入 `<DF根>/gamelog.txt`; 桥低频(每 30 个逻辑帧)以
  `io.open` + `seek` 记录偏移增量读取。
- **首次只记录偏移不重放**(否则每次启动都翻译整部历史); 之后每行 FNV-1a
  哈希去重(与公告重叠的事件只翻一次)。
- 映射: `text_type=HISTORY`, `priority=30`(PREFETCH 档, 后台慢慢翻),
  `screen='history'`。
- 局限: gamelog 与公告存在重叠, 靠哈希去重; 极端截断(日志轮转)时偏移重置,
  行为退化为"重新只记录偏移", 无害。

## 3. 渲染: overlay 小部件 + 字体贴图路线

- **v1 落地**: `fanyi.lua` 维护渲染载荷(纯函数 `fanyi_render_lines()`),
  命令 `fanyi overlays on|off [cjk]` 控制; CJK 门控默认关。
- **CJK 路线(ticket-009)**: classic 的 8x12 位图字库只有 CP437 字形,
  中文必须走 DFHack 的 **自定义贴图/texture** 渲染(生成字形图集注册进
  DFHack texture 系统, overlay 绘制时引用), 不替换原字库文件。
  本机无 CJK 字体依赖问题已在 ticket-001 审计标注; 生成脚本(取字→渲染
  字形→打包图集)归 ticket-009。
- **confidence 门控**: 只有 `confidence > 0` 且译文非空才进渲染载荷;
  失败(confidence=0/failed)静默 → 游戏继续显示原文(§2.3)。
- 渲染载荷与 overlay 解耦(纯函数产出), 无头测试直接断言载荷内容。

## 4. 版本守卫 → Safe Mode (§43-44)

- 脚本加载即核对 `dfhack.getDFVersion() == '53.16'` 与
  `dfhack.getDFHackVersion()` 前缀 `53.16-r1`。
- 不匹配 → `S.safe_mode=true`: **不捕获、不联网、不改 UI**, 仅 `fanyi status`
  /`fanyi debug` 打印诊断与不匹配原因。其他命令一律拒绝。
- 依据: 工程书 §43-44 —— 未知版本上任何 UI/内存修改都是禁区, 只做被动诊断。

## 5. 传输定论: loopback TCP(关键事实)

**DFHack 捆绑的 luasocket 插件只实现了 TCP, 没有 unix socket。** 证据:

1. 官方文档 `hack/docs/docs/Lua API.rst` "luasocket" 节: 仅描述 tcp
   (`socket.tcp`/`bind`/`connect`), 明确 "It is planned to eventually
   support UDP"; 全文无 unix socket API;
2. 源码 `plugins/luasocket.cpp`(53.16-r1.1): 基于
   `CPassiveSocket/CActiveSocket`(PassiveSocket 库), `lua_socket_bind`
   只接受 `ip:port`; 无 AF_UNIX 路径。

因此工程书 §21 的"unix socket 客户端"在游戏侧不可行, **定论改为
loopback TCP 127.0.0.1:17486**(与配置 `bridge.port` 对齐):

- 安全性: 只绑 127.0.0.1, 不暴露外网; 与 DFHack 自身 RPC(127.0.0.1:5000,
  `allow_remote:false`)同级隔离。
- **宿主侧工具(unix socket)不受影响**: `df_fanyi bridge --transport unix`
  保留给测试与本地工具; Python 服务器两种传输同一套协议代码
  (`df_fanyi/bridge/protocol.py`)。

### luasocket 非标准 API 备注(踩坑记录)

捆绑版对标准 LuaSocket 做了改造, 必须按下面方式用(源码核对):

| 操作 | 写法 | 注意 |
|---|---|---|
| 建连 | `socket.tcp:connect(host, port)` | `socket.tcp` 是**表**不是函数; 失败不返回 error string, 用 `pcall` + 结果判空 |
| 非阻塞 | `client:setNonblocking()` | 默认阻塞; **主线程绝不阻塞**(§29) |
| 发送 | `client:send(data)` | 无返回值; 失败抛错 → `pcall` 包裹 |
| 接收 | `client:receive('*l')` | 非阻塞无数据返回 `nil`(C++ 侧 `SocketEwouldblock` 被 `handle_error` 静默吞掉), 天然适合每帧轮询 |
| 关闭 | `client:close()` | — |

## 6. 引擎失联: 静默降级 + 自愈 (§2.3/§29/§30)

- 所有 socket 操作 `pcall` 包裹; 任一步失败 → 立刻置离线、清在途、进退避。
- **退避**: 90 帧(约 1.5s)起步 ×2 封顶 900 帧(约 15s), 重连成功重置。
- **每节拍限流**: 单节拍最多发 3 条 + 有界读回(≤16 行), 游戏主循环耗时
  与引擎状态无关(§29: 主线程从不等待翻译结果)。
- 在途(`inflight`)计数与断线即作废: 引擎重启后旧结果不可追溯, 事件由
  gamelog/公告再次触发捕获(公告 id 不重复, 历史 FNV 哈希去重防重放)。
- 心跳: 无在途时每 60 帧(约 1s)发一次 `fetch_done` 充当心跳 —— 引擎宕机
  最迟 ~1s 被感知(依赖内核 RST, loopback 上即时)。

## 7. 验收对照(ticket-008)

| 验收标准 | 状态 |
|---|---|
| DFHACK_INTEGRATION.md 含捕获源/渲染方案/classic 支持/版本守卫定论 | 本文件 §2-§4 |
| Lua 桥能在 DF 中加载(有 load 报告), 引擎关闭时游戏正常显示原文 | `scripts/install-dfhack.sh`(luac 语法关+装载)+ `scripts/dfhack-load-report.sh --no-engine`(引擎关闭探针); 无头场景 `engine_down`/`reconnect` 覆盖静默降级与自愈 |
| socket 协议有回放测试; pytest 全绿 | `tests/test_bridge_protocol.py`(17)+ `tests/test_bridge_server.py`(13)+ `tests/test_bridge_lua.py`(10, 真实 fanyi.lua 无头回放) |

## 8. 文件清单

| 文件 | 作用 |
|---|---|
| `dfhack/scripts/fanyi.lua` | 游戏侧桥(捕获/传输/渲染载荷/命令/守卫), 安装到 `hack/scripts/` |
| `df_fanyi/bridge/protocol.py` | JSON-RPC 2.0 over JSON Lines 协议(两端共用语义) |
| `df_fanyi/bridge/server.py` | 引擎侧服务器(tcp/unix, 线程化, 结果缓冲) |
| `tests/lua/fanyi_harness.lua` | 无头 DFHack 环境模拟(系统 lua5.4 跑真实脚本) |
| `tests/lua/run_fanyi_tests.lua` | 场景运行器(7 场景, pytest 子进程调用) |
| `tests/test_bridge_lua.py` | pytest 驱动 + 禁项静态检查(无 memory 钩子/无阻塞读) |
| `scripts/install-dfhack.sh` | 安装 fanyi.lua → hack/scripts/(幂等, luac 语法关) |
| `scripts/dfhack-load-report.sh` | 真机负载探针(引擎起停/dfhack-run/手工步骤) |
