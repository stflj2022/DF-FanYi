# ticket-013 — textviewer 弹窗内嵌翻译覆盖层

> 父 spec：SPEC-012 · 状态：pending · 优先级：P1
> 依赖：012（字幕条删除后才能注册新 widget 不冲突）· 被依赖：016

## 目标
在游戏内 textviewer 弹窗（欢迎向导/教程/`?` 帮助页）的**原文区域**直接显示中文，覆盖英文原文。观感像官方中文版——没有独立字幕条。

## 范围

### 新增 fanyi.lua 组件

**1. textviewer 捕获（已有，需增强）**
- 已有：`viewscreen_textviewerst` 轮询捕获 title+text
- 增强：捕获 textviewer 控件的矩形坐标 `dfhack.gui.getCurViewscreen():getBounds()` 或遍历 widget 子树找 textviewer 区域
- 缓存 key: `textviewer:{title}:{text_hash}` → 译文
- 缓存过期：离开 textviewer（screen 类型变化）→ 清空该 title 的所有缓存

**2. textviewer_inline overlay widget**
```lua
TextviewerInline = defclass(TextviewerInline, overlay.OverlayWidget)
TextviewerInline.ATTRS = {
    desc = 'DF-FanYi textviewer 内嵌翻译',
    default_pos = {x=0, y=0},  -- 由 widget 内部按 textviewer 矩形自定位
    default_enabled = true,
    viewscreens = {'textviewer'},  -- 仅在 textviewer 视图下激活
}
function TextviewerInline:init()
    self.frame = {...}
end
function TextviewerInline:onRenderFrame(painter, rect)
    -- 1. 读 textviewer 控件矩形
    -- 2. 读 S.textviewer_cache[title]
    -- 3. 按文本宽度自动换行（中文宽度 = 1.0 char 宽 vs 英文 0.5）
    -- 4. 在 textviewer 矩形内贴中文（沿用 dfhack.textures 图集——复用 012 之前删除的图集？）
end
```

**3. CJK 字形图集复用**
- 012 已删除 `hack/data/fanyi-font/`；013 重新需要图集（不能复用旧的，需小幅调整）
- **决策**：013 重新生成字形图集（更小，只含 textviewer 内嵌常用字 + 高频字；不放 3877 全部）
- 新生成脚本：`scripts/generate_font_atlas.py --subset 800 --scale 2 --output hack/data/textviewer-font/`
- 800 字覆盖 ~95% textviewer 文本（欢迎弹窗 + 教程 + 帮助）

**4. 翻译引擎路由**
- 扩展 JSON-RPC：`bridge.inline_translate(text, context='textviewer')`
- 引擎侧路由：
  - 段落缓存命中（key: `inline:{title}:{text_hash}`）→ 直接返回
  - 未命中 → 调 LLM（云端 router/L2，本地兜底）→ 写入缓存 → 返回
  - 段落超过 200 字 → 切句（按句号/换行）+ 并行翻译

## 实现要点

**textviewer 控件矩形获取**
- DFHack API：`dfhack.gui.getCurViewscreen()` → viewscreen_textviewerst
- textviewer 含 `text` 字段（String） + 渲染区域
- 遍历方式：`viewscreen_textviewerst → widgets → 找 TextView → getBounds()`
- 若 DFHack API 不足 → 用 `dfhack.gui.getPanelLayout()` 兜底

**贴图（贴 CJK 中文）**
- 沿用 ticket-009 已定的 dfhack.textures API（不再用图集，直接贴字形）
- 复用 012 删除前的图集生成代码（`generate_font_atlas.py`）
- textviewer 矩形宽 ~80 字符 → 中文每行 40 字 → 自动换行

**消失机制**
- `viewscreens` 过滤：仅在 textviewer 视图下显示 widget
- 离开 textviewer → widget 自动失效（DFHack overlay 框架默认行为）

## 验收
- [ ] fanyi.lua 启动后 OVERLAY_WIDGETS 注册 textviewer widget
- [ ] `fanyi status` 输出包含 `textviewer_inline (on/off)`
- [ ] 重启游戏 → 欢迎弹窗（Prepare to guide your stout charges...）→ textviewer 矩形内显示中文（不是底部字幕条）
- [ ] 离开弹窗 → 中文消失（widget 自动失效）
- [ ] hack harness 单元测试：`TextviewerInline:onRenderFrame(dc, 80, 25)` 在 mock textviewer 数据下输出非空
- [ ] 单测：缓存 get/set/expire 测试通过
- [ ] 集成测试：JSON-RPC `inline_translate` 往返（含缓存命中/未命中两条路径）
- [ ] 提交 + 推送

## 风险
- textviewer 矩形难定位（DFHack widget 子树遍历复杂度高）：fallback 用 `dfhack.gui.getPanelLayout()` + 多位置候选
- 字形图集回归：013 重新生成字形图集，bundled 字数从 3877 → 800（覆盖 textviewer 95% 文本）；不常用的字 fall back 到英文显示（接受，不补字）
- 渲染延迟：首次进入 textviewer 要等 2-3s 翻译；UI 加 100ms 内"翻译中"标识（淡灰色"..."占位）

## 估计
- fanyi.lua 新增：~250 行
- 字形图集生成脚本调整：~30 行
- 单测：~120 行
- 集成测试：~50 行
- 提交：1 commit + push

## 备注
**字形图集删除后再生成**——保留 `generate_font_atlas.py` 但调整参数（subset 800，scale 2）。字形不直接 inline 进 fanyi.lua 而走外部文件（避免 fanyi.lua 体积过大）。