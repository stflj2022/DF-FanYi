# ticket-008: DFHack 集成设计与 Lua 捕获/渲染桥

## 状态: pending

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
- [ ] DFHACK_INTEGRATION.md 含捕获源/渲染方案/classic 支持/版本守卫的定论
- [ ] Lua 桥能在 DF 中加载(有 load 报告),引擎关闭时游戏正常显示原文
- [ ] socket 协议有回放测试;pytest 全绿
