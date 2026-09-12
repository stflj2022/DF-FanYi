# SPEC-012 — 删除字幕条 · 让游戏内文本自然显示中文

> 用户决策（2026-09-12）：当前 DF-FanYi 用底部字幕条 overlay 显示动态文本（公告/弹窗/教程），但字幕条不修改游戏内画面，与"官方中文版"观感差距大。本 spec 把所有动态文本的译文**直接显示在原文区域**（覆盖/替换），删除字幕条 overlay。

## 一、目标与反目标

### 目标
1. **删除 fanyi.lua 字幕条 overlay** —— 不再有底部独立字幕条；游戏界面内**自然**显示中文。
2. **公告面板内嵌翻译** —— `world.status.reports` / `world.status.announcements` 对应的渲染控件直接显示中文。
3. **textviewer 弹窗内嵌翻译** —— 欢迎向导/教程/`?` 帮助页整段中文（覆盖原英文正文）。
4. **长段落 addst 段落缓存** —— v53 按词逐次 addst 渲染时，dfint 钩子上层做段落聚合 + LLM 回填 + 缓存命中。
5. **E2E 全场景中文** —— 重启游戏后无字幕条残留；任意弹窗/公告/长段落中文显示。

### 反目标
- 不改 dfint-rust-cjk 源码（它已经是 Rust detour，编译链重；做段落缓存需在 DFHack Lua 层或 Python 引擎层，不下沉到 Rust）。
- 不删 dfint 静态词典（仍作为第一层兜底）。
- 不动 vanilla raws（pretranslate 已批量入库 6964 句）。
- 不改游戏 EXE 二进制。

## 二、架构变更

### 旧架构
```
addst 渲染钩子 → dfint 词典命中 → 直接显示中文        [静态层, 已实现]
                      ↓ 未命中
addst → 英文原文 + 词沙拉                          [静态层局限]

fanyi.lua 捕获 (announcement/textviewer) → 引擎 LLM 翻译
                                          ↓
                                S.render_lines + FanyiSubtitle overlay
                                          ↓
                                  屏幕底部字幕条中文    [动态层, 本轮删]
```

### 新架构
```
addst 渲染钩子 → dfint 词典命中 → 直接显示中文        [静态层, 不变]
                      ↓ 未命中
                段落聚合缓存 (50ms 窗内合并)            [本轮新增]
                      ↓
              引擎 LLM 整段翻译 → 写回段落缓存           [本轮新增]
                      ↓
                后续 addst 命中缓存 → 直接显示中文

fanyi.lua 捕获 (announcement/textviewer) → 引擎 LLM 翻译
                                          ↓
                          announcement_panel_overlay / textviewer_overlay
                                          ↓
                              原文区域覆盖中文显示    [动态层, 本轮重写]
```

### 关键设计点

**A. 删除字幕条 overlay（ticket-012）**
- 清理 fanyi.lua：删除 `fanyi_render_lines`、`fanyi_paint_subtitle`、`FanyiSubtitle`、`OVERLAY_WIDGETS`、`S.render_lines`、`S.render_cjk`、`S.font`、`S.overlays_on`、`dfhack.textures` 调用、CJK 图集加载逻辑
- 删除 dfint-data/fanyi-font/（4 页 × 64×16 字形贴图，不再需要）
- 删除 hack/data/fanyi-font/
- 保留 `FANYI_TTL_MS` 环境变量读取代码（虽然不再渲染，但为兼容清理）
- 保留所有捕获代码（announcement + textviewer + History + 双容器轮询）——本轮复用

**B. textviewer 弹窗覆盖 overlay（ticket-013）**
- 新增 `textviewer_inline` overlay widget（仍在 `OVERLAY_WIDGETS` 注册）
- 通过 DFHack `dfhack.gui.getCurViewscreen()` 找到 `viewscreen_textviewerst`
- paint 时读取 textviewer 控件矩形区域，**只在区域内**贴中文（不挡其他界面）
- 中文仍走引擎翻译结果（缓存 S.textviewer_cache，命中不调引擎）
- 字幕消失机制：从 textviewer 离开时（如按 ESC/Back）→ 清理 cache → overlay 自然隐藏

**C. 公告面板覆盖 overlay（ticket-014）**
- 新增 `announcement_inline` overlay widget
- 监听 announcement 容器写入 → 调引擎翻译 → 缓存
- paint 时找到 announcement 控件矩形（DFHack `gui.getPanelLayout` 或 widgets 遍历）
- 区域内覆盖中文（不挡 Embark/右侧菜单）
- 公告自动过期机制：translation_ttl_ms 90s 后从缓存移除，overlay 自然显示原文

**D. 长段落 addst 段落缓存（ticket-015）**
- fanyi.lua 在 DFHack 层 hook `addst` 查询入口（不直接 detour Rust，而是监听 `dfhack.console.print`/字符串渲染日志不靠谱；改走 **dfint Debug 日志** + **dfhack.translate API** 兜底）
- 实际可行路径：在 Python 引擎侧监听 dfint Debug 日志 `missing translation: "word1 word2 word3..."` 连续行 → 聚合窗口（100ms）→ 拼接段落 → LLM 翻译 → 写入引擎 `inline_cache` SQLite 表
- fanyi.lua 启动时拉一次 inline_cache（按段落 hash 索引）→ 渲染时若发现词命中段落缓存，整段替换（用一次性 `addst` patch 或 `dfhack.translate` 注入）
- 不下沉到 Rust：dfint 的 `cache: HashMap<u64, TranslatedText>` 是 Rust 内存缓存，写入路径要走 Rust 函数调用；Lua 层只能通过 dfint 的 `simple-dictionary.csv` USER 字典热重载（游戏启动时载入，热重载需要重启游戏）——这不满足"动态注入"
- **妥协方案**：长段落 addst 段落缓存只覆盖"长静态段落"（欢迎弹窗/教程页），不试图注入 Rust 内存缓存；具体做法是 textviewer 捕获已覆盖的段落（同 B），段落缓存与 B 复用同一路径

**E. E2E 验证（ticket-016）**
- 重启游戏（Steam / dfhack-run）
- 验证清单：
  1. fanyi status 显示 "subtitle widget: removed"
  2. 欢迎弹窗：textviewer 区域显示中文（不是底部字幕条）
  3. 公告面板：进入世界后任意公告在原文面板内显示中文
  4. 长段落（教程页）：不出现词沙拉，整段中文
  5. fanyi 状态/统计：overlay widgets 只剩 `fanyi.textviewer_inline` + `fanyi.announcement_inline`，没有 `fanyi.subtitle`
  6. dfint-data/fanyi-font/ 已不存在

## 三、依赖与里程碑

```
ticket-012 (删字幕条) ─┐
                       ├─→ ticket-013 (textviewer 覆盖) ─┐
                       │                                  ├─→ ticket-016 (E2E)
                       ├─→ ticket-014 (公告面板覆盖) ─────┤
                       │                                  │
                       └─→ ticket-015 (段落缓存辅助) ─────┘
```

- ticket-012 必须先做（清理公共代码 + 删除图集）
- ticket-013/014 可并行（独立 widget）
- ticket-015 是 ticket-013/014 的优化路径（减少重复翻译），不强依赖但建议
- ticket-016 必须最后做（端到端验证）

## 四、验收标准

### 功能验收
- [ ] DFHack `fanyi status` 输出不再包含 "subtitle" widget；只列 `textviewer_inline` + `announcement_inline`
- [ ] `hack/data/fanyi-font/` 目录已删除
- [ ] `dfint-data/fanyi-font/` 目录已删除
- [ ] 游戏中底部无独立字幕条 overlay
- [ ] textviewer 弹窗（欢迎向导/教程/`?`）原文区域显示中文
- [ ] 公告面板任意公告原文区域显示中文
- [ ] 不出现词沙拉（段落整句翻译，按词 addst 不再触发）

### 性能/资源验收
- [ ] fanyi.lua CPU 占用 < 2%（10 分钟采样，n=3 局）
- [ ] 段落缓存命中率 ≥ 30%（同一弹窗 2 次进入，第二次命中 cache）
- [ ] 公告翻译平均延迟 < 3s（云端 router/L2）

### 代码质量验收
- [ ] 单测：fanyi_render_inline() 纯函数测试（无头可跑）
- [ ] 单测：fanyi_textviewer_cache() get/set/expire 测试
- [ ] 集成：hack harness 测试 textviewer/announcement 捕获 + 渲染解耦函数
- [ ] 代码审查：双遍（standards + spec）通过

## 五、风险与缓解

| 风险 | 缓解 |
|------|------|
| 覆盖 overlay 仍可能在某些 view 错位 | widget 内部 lazy-init 位置（每 5 tick 重读控件矩形） |
| 公告面板 widget 难定位（DFHack 没有公开 API） | 用 `dfhack.gui.getPanelLayout()` 兜底 + 多位置候选 |
| 段落缓存写入 Rust 内存失败 | 退化为下次重启游戏后命中（写入 simple-dictionary.csv） |
| 渲染延迟（首次进入弹窗要等 LLM） | UI 上加 200ms "loading..." 标识 + 优先级队列（当前视图优先） |
| 字幕条删除后某些场景无中文 | ticket-016 端到端验证发现漏网场景 → 即时补 ticket |
| 删除图集后某些 fallback 路径误用 | fanyi.lua 启动自检：无 render_cjk 字段，无 dfhack.textures 调用 |

## 六、回滚方案

每 ticket 提交独立 commit + 推送。若 E2E 验证失败：
```bash
git revert <last-3-commit>   # 回滚到字幕条版本
# 或按 ticket 单独 revert
git revert <ticket-016-commit>
git revert <ticket-013-commit>
git revert <ticket-012-commit>   # 重建字幕条 + 图集
```

## 七、相关文件清单

| 文件 | 用途 | 本轮变更 |
|------|------|----------|
| `hack/scripts/fanyi.lua` | DFHack 桥 | 删除字幕条 widget；新增 textviewer_inline + announcement_inline |
| `hack/data/fanyi-font/*` | 字幕条字形贴图 | 删除（不再需要） |
| `dfint-data/fanyi-font/*` | 字幕条字形贴图（引擎侧备份） | 删除 |
| `df_fanyi/bridge/*` | JSON-RPC 引擎 | 新增 `inline_cache` 路由（段落缓存） |
| `docs/specs/SPEC-012-inline-chinese-rendering.md` | 本文档 | 新增 |
| `docs/tickets/ticket-012~016*.md` | 拆解工单 | 新增 |
| `scripts/install-df-fanyi-omarchy.sh` | 一键部署 | 适配无图集（不部署 fanyi-font） |
| `tests/test_fanyi_render_inline.py` | 单测 | 新增 |

## 八、与现有系统的兼容

- **pretranslate 批量入库（ticket-011）**：不变；其生成的 vanilla raws 翻译仍生效。
- **dfint legacy 词典（29797 行）**：不变；其静态词典匹配仍生效。
- **collect-untranslated 闭环**：不变；继续收集 missing translation 补词典。
- **引擎桥（bridge）**：扩展 `inline_cache` 路由；JSON-RPC 协议向后兼容（旧 `translate` 方法保留）。
- **fanyi 命令**：保留 `status/start/stop/debug`；删除 `overlays on|off|cjk` 改为 `overlays on|off [textviewer|announcement]`。

---

*作者：DF-FanYi 工程 · 2026-09-12*
*驱动：auto-engineering-workflow skill → tickets 012-016 → unattended-dev-system driver*