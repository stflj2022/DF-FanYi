# ticket-001: 环境与版本审计 + 本地模型调用固化

## 状态: pending

**Blocked by:** 无(可立即开始)
**产出:** docs/audits/ENVIRONMENT_AUDIT.md

**做什么:** 端到端查清本机 DF 环境,为后续所有工单提供事实依据。
1. 确认 DF 与 DFHack 精确版本: `cd ~/Games/DwarfFortress && ls hack/libs/ | grep dfhack`、
   `strings dwarfort | grep -m2 -E "^v[0-9]+\.[0-9]+"`、hack/changelog-ToDo.txt 等;
   确认 DFHack 该版本是否支持 classic(非 Steam)构建。
2. 修复本地模型调用: ollama `gemma-4b-trans` 经 /api/generate 返回空响应;
   改用 /api/chat(messages 格式)复测,固化正确调用(含中文翻译系统提示),
   记录首载延迟与生成速度(tok/s)、单句端到端延迟。
3. 验证 DFHack 可 headless 交互: `./dfhack-run` 需要游戏运行 —— 记录如何启动
   (run_df)、如何在不进图形的情况下确认插件加载(ls hack/plugins/ 找 eventful/overlay)。
4. 把全部事实(版本号、命令、延迟数据、坑)写入 docs/audits/ENVIRONMENT_AUDIT.md。

**验收标准:**
- [ ] docs/audits/ENVIRONMENT_AUDIT.md 存在,含 DF 版本、DFHack 版本、classic 支持结论
- [ ] 含 ollama chat API 可用调用样例与实测延迟数据(单句 <5s 或记录实际值)
- [ ] 含"如何启动 DF 并确认 DFHack 活着"的可复制命令
