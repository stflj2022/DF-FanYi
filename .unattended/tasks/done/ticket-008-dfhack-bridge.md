# ticket-008: DFHack 集成设计与 Lua 捕获/渲染桥

## 状态: done

**Blocked by:** ticket-001, ticket-002
**产出:** docs/audits/DFHACK_INTEGRATION.md + 可加载的 DFHack Lua 桥 v1

**做什么:** 游戏层(tracer bullet 之游戏侧):
1. 基于 001/002 审计写 docs/audits/DFHACK_INTEGRATION.md: 选定捕获源
   (announcement/gamelog/overlay screen API)、渲染方式(DFHack overlay/
   自绘 screen)、classic 版字符集方案、版本守卫(§43-44 Safe Mode)。
2. 实现 dfhack/plugins/ 侧 Lua: `fanyi.lua` —— capture worker(轮询新文本 →
   TextEvent JSON, 工程书 §6 字段)、unix socket 客户端(连 Python 引擎,
   JSON-RPC: translate / fetch_done / health)。
3. Python 侧 socket server(JSON-RPC over unix socket,线程安全,与队列集成)。
4. 引擎失联/崩溃 → Lua 侧静默显示原文,游戏零影响(§2.3);重连自动恢复。
5. headless 单测: socket 协议回放;提供 `dfhack-run fanyi status` 诊断命令。

**验收标准:**
- [x] DFHACK_INTEGRATION.md 含捕获源/渲染方案/classic 支持/版本守卫的定论
- [x] Lua 桥能在 DF 中加载(有 load 报告),引擎关闭时游戏正常显示原文
- [x] socket 协议有回放测试;pytest 全绿

---

## 完成记录 (2026-09-08)

**交付物**:
- `docs/audits/DFHACK_INTEGRATION.md` — 捕获/渲染/守卫/传输定论(含 luasocket 仅 TCP 的源码+文档证据)
- `dfhack/scripts/fanyi.lua` — 游戏侧桥 v1(eventful.onReport 公告捕获 + gamelog 增量回溯 + 非阻塞 TCP 客户端 + Safe Mode 版本守卫 + overlays 命令);已安装进 ~/Games/DwarfFortress/hack/scripts/
- `df_fanyi/bridge/{protocol,server}.py` — 引擎侧 JSON-RPC 服务器(tcp/unix 双传输, 结果缓冲, 队列集成)
- `tests/lua/{fanyi_harness,run_fanyi_tests}.lua` + `tests/test_bridge_lua.py` — 真实 fanyi.lua 无头回放(7 场景)
- `scripts/install-dfhack.sh` / `scripts/dfhack-load-report.sh` — 安装器与真机探针

**与工单的偏差(已在审计 §5 论证)**: 工单原文"unix socket 客户端"不可行 —
DFHack 捆绑 luasocket 仅实现 TCP(官方文档 "planned to eventually support UDP"
+ 源码无 AF_UNIX)→ 定论 loopback TCP 127.0.0.1:17486(仅绑回环, 与 DFHack
RPC 同级隔离);宿主侧 unix socket 工具保留。

**验收实现映射**:
- 捕获/渲染/守卫定论 → DFHACK_INTEGRATION.md §2-§4
- 可加载+引擎关时静默原文 → install-dfhack.sh(luac 语法关)+ load-report --no-engine
  + 无头场景 engine_down/reconnect(离线不崩、退避自愈、重连计数)
- 协议回放+全绿 → test_bridge_protocol(17)/test_bridge_server(13)/test_bridge_lua(10),
  全仓 pytest 279 passed

**坑**:
1. DFHack luasocket 是改造版: `socket.tcp` 是表(`socket.tcp:connect`), send 无
   返回值, connect 失败不返回 error string(pcall+判空), 非阻塞 `receive('*l')`
   无数据返回 nil(EWOULDBLOCK 被 C++ handle_error 静默吞掉)。
2. Lua `#` 对哈希表未定义 → `#S.sent` 恒 0, fetch_done 心跳永不触发, 引擎宕机
   检测不出来; 改显式 inflight 计数。
3. DFHack 脚本每次命令重执行整个文件 → 状态必须在全局表且**不能在加载路径上
   重置运行态**(status 会把桥停掉)。
4. 无头 harness: 系统 lua5.4 无 cjson → 自带 ~120 行 JSON 编解码; mock 环境
   `env._G = env` 让脚本的 `_G.json` 落到 mock; `local path = ...` 是经典
   vararg 陷阱(须 `{...}`)。
