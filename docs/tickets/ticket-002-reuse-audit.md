# ticket-002: 现有方案复用审计(DFI18n / dwarf-fortress-chinese)

## 状态: pending

**Blocked by:** 无(可立即开始)
**产出:** docs/audits/REUSE_PLAN.md

**做什么:** 工程书 §2.1 强制:先查轮子再写码。调研以下项目并给出复用/改造清单:
- DFI18n(DFHack 生态的 DF 本地化框架)与 DFI18n Data - Simplified Chinese
- wodzys/dwarf-fortress-chinese
- DFHack 53.16 的官方能力(eventful、overlay、Lua screen API、dfhack-run)
方法: 网络搜索 + 拉取源码审计(可 shallow clone 到 /tmp)。必须回答:
1. 这些项目如何捕获文本/渲染中文?用了哪些 DFHack API?
2. classic 版(非 Steam)是否被支持?字符集/字体方案是什么(CP437? TTF?)
3. 我们的引擎能直接复用其中哪些组件(词典数据、渲染层、捕获层)?
4. 结论表: 每个组件 → 复用 / 改造 / 自研 + 理由。

**验收标准:**
- [ ] docs/audits/REUSE_PLAN.md 存在,含上述 4 问的明确答案
- [ ] 有结论表(复用/改造/自研),并据此更新 specs 中第一版范围
- [ ] 引用的仓库/文档有 URL,关键结论有代码/文档佐证
