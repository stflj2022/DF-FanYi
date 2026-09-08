# ticket-009: 游戏端到端集成(游戏内看到中文)

## 状态: pending

**Blocked by:** ticket-005, ticket-007, ticket-008
**产出:** DF 内公告 → 中文 overlay 显示的完整链路

**做什么:** tracer bullet 全线贯通:
1. Lua 捕获的 TextEvent → socket → 引擎队列 → 翻译完成回调 → Lua 拉取 →
   overlay 渲染中文(字体方案按 ticket-002 结论;classic 需确认中文渲染可行路径,
   若需中文字体补丁/替换字库,把方案与文件落进 dfhack/)。
2. 渲染策略: 原文位置叠加中文或底部字幕条(按 DFHACK_INTEGRATION.md 定论)。
3. 降级链实测: 引擎停 → 原文;ollama 停 → 词典/规则兜底;断网(无云端)一切照常(Test 7)。
4. 手工验证清单写入 docs/audits/E2E_CHECKLIST.md(游戏内无法自动化,列人工步骤)。

**验收标准:**
- [ ] 启动 DF + DFHack + 引擎,触发公告(如 dwarf cancel job)屏幕出现中文
- [ ] 关闭引擎,游戏无报错、显示原文;重启引擎自动恢复翻译
- [ ] E2E_CHECKLIST.md 存在且本工单内人工执行过一轮并记录结果
