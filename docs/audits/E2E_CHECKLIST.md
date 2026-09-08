# E2E 验收清单(ticket-009: 游戏端到端)

> 降级链验收(工单原文): 引擎停 → 原文; ollama 停 → 词典/规则兜底; 断网照常。
> 渲染: 底部字幕条 overlay(fanyi.subtitle), CJK 字形图集贴图, 见
> docs/audits/DFHACK_INTEGRATION.md §9。

## A. 自动化轮次(已执行 2026-09-08, 全部通过)

| # | 轮次 | 层 | 结果 |
|---|---|---|---|
| A1 | Lua 无头 9 场景(safe_mode/engine_down/roundtrip/zero_confidence/reconnect/gamelog_history/overlays_cmd/cjk_render_tiles/cjk_missing_glyph) | fanyi.lua 真脚本 × mock 环境 | PASS(tests/test_bridge_lua.py) |
| A2 | 字形图集: 生成(3877 字形/单页)、index 结构、OFL 许可、渲染冒烟(PIL) | 产物 | PASS(tests/test_font_atlas.py; 系统 python 无 PIL 时冒烟 skip 属预期) |
| A3 | 真 socket 端到端: 词典快路径(零 LLM 调用)、长句 queued 占位→fetch_done 轮询取回、LLM 挂→词典兜底+长句 failed 静默、引擎重启恢复、离线(无 LLM)降级、Lua 线上格式镜像(逐字段+双事件同文 FIFO 映射) | 真 BridgeServer + 假 LLM | PASS(tests/test_e2e_loop.py) |
| A4 | 全仓回归 | pytest | 全绿(见提交) |
| A5 | 安装: install-dfhack.sh 拷贝 fanyi.lua + 图集进 ~/Games/DwarfFortress, 幂等重跑 skip, luac 语法过 | 真机文件 | PASS |

自动化覆盖不到的只剩「游戏主循环里 overlay 真渲染一帧」—— 以下人工轮次。

## B. 游戏内人工轮次(需用户在桌面前执行)

前置: `bash scripts/install-dfhack.sh`(已装); 引擎未起也先做 B1/B2(验证降级)。

### B1 装载与版本守卫
1. 启动 Dwarf Fortress(DFHack 53.16-r1.1)。
2. DFHack 控制台执行 `fanyi status`。
   - 预期: 状态行出现 `版本: 53.16 vs DFHack 53.16-r1.1`, 桥进入 running;
   - 若版本不匹配: 只打印诊断, 不捕获(§43-44 Safe Mode)。

### B2 引擎停 → 原文(降级链第一段)
1. 不启动引擎(或 `pkill -f df_fanyi`)。
2. 游戏中制造公告(如挖一格墙)。预期: 游戏公告**英文原文**正常显示, 无卡顿、
   无报错刷屏; `fanyi debug` 里 reconnect 计数增长(退避重连), 引擎宕机被心跳感知。

### B3 引擎起 → 中文上屏(主链路)
1. 终端起引擎: `cd ~/DF-FanYi && python3 -m df_fanyi bridge --transport tcp --port 17486`
   (本地 LLM 用 ollama: `ollama serve` 先行; 云端配置见 providers.yaml)。
2. 游戏内 `fanyi overlays on cjk`。预期: 打印「译文悬浮已开启(CJK 贴图就绪: 3877 字形)」。
3. 游戏中触发公告/事件(矮人说话、战斗报告、 cancel 提示)。
4. 预期: 屏幕底部字幕条逐条出现中文(黑影白字), 词典词(矮人/木桶/取消)即时,
   长句 1-2s 后补上(异步占位期间无感); `fanyi status` done 计数增长。
5. `fanyi overlays off` → 字幕条消失, 公告恢复原文显示。

### B4 ollama 停 → 词典/规则兜底(降级链第二段)
1. `pkill ollama`(引擎保持运行)。
2. 触发含词典词的公告(如 "wooden barrel")。预期: 词典整句/词组命中 → 立即中文;
   长句 → 静默原文(2.3), 无异常弹窗; `fanyi status` failed 计数增长属预期。

### B5 断网照常(降级链第三段 + ADR-cloud-first)
1. 断开外网(v2rayN 关 TUN 或拔网线), 本地 ollama 运行中。
2. 预期: 本地 gemma 翻译照常(loopback), 云端 provider 连接失败被引擎吞掉回退;
   若本地也停 → 同 B4(词典/规则/原文)。恢复网络无需重启游戏。

### B6 CJK 贴图负路径
1. 临时改名 `hack/data/fanyi-font` 后 `fanyi overlays on cjk`。
   预期: 打印「CJK 渲染不可用: 未安装字形图集…」且**不**进 CJK 门控(原文显示)。
   验完改名回来, 再 `fanyi overlays on cjk` 恢复。

## C. 记录

- 自动化执行: 2026-09-08 无人值守驱动(ticket-009), 结果见上表与提交历史。
- B 组人工轮次: 待用户执行后在本文档勾选/补记(脚本无法替人看游戏画面)。
