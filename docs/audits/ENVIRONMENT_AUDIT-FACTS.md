# 环境已验证事实(人工实测,2026-09-08)

> 本文档由主会话人工操作写入,ticket-001 审计必须引用并补充,不要重复验证已标注 ✅ 的项。

## ✅ 已人工验证

1. **游戏可正常启动游玩**:DF + DFHack 53.16-r1.1(release)于 2026-09-08 人工启动,
   进入画面正常(1920x1080 全屏窗口),用户已亲自启动并退出,无崩溃、errorlog 为空、
   无 coredump。
2. **启动方式**:
   - 带 DFHack: `cd ~/Games/DwarfFortress && ./dfhack`(前台交互,推荐)
   - 裸游戏: `./run_df`
3. **DFHack 版本**: 53.16-r1.1 on x86_64(与工程书目标版本完全一致)。
4. **渲染参数**: Font size 8x12 位图字库,窗口 1920x1080 时网格 240x90。
   —— classic 版是 CP437 位图字库贴图,**中文渲染方案**(TTF/字库替换/
   DFHack texture-render API)是 ticket-002 调研与 ticket-008 设计的核心。
5. **DFHack 控制台行为**: 以 nohup/无 stdin 方式启动时,控制台立即
   "Console is shutting down properly"——控制台退出**不影响游戏本体**,
   但失去交互命令入口。
6. **DFHack 初始化**: dfhack.init 正常执行,插件 buildingplan/burrow/logistics/
   overlay/preserve-rooms 自动 enable,alias 正常注册。

## ⚠️ 待 ticket-001 查清

1. **RPC 未开**: `./dfhack-run` 报 "Could not connect to localhost:5000"。
   DFHack RPC server 需要确认开启方式(dfhack.init 中 remote/sockets 配置,
   或 classic 版需手动 enable)—— **这是 DFHack Lua 桥与 Python 引擎通信的关键前置**;
   备选:绕过 TCP RPC,用 unix socket + `dfhack-run`/脚本文件轮询做桥。
2. DF 数据目录结构与存档位置(data/ 下 save、init 路径)。
3. world gen 可否用命令行/脚本自动生成小世界(供 ticket-009 端到端测试用)。
