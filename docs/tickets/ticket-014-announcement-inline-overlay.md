# ticket-014 — 公告面板内嵌翻译覆盖层

> 父 spec：SPEC-012 · 状态：pending · 优先级：P1
> 依赖：012（字幕条删除后才能注册新 widget）· 被依赖：016

## 目标
公告面板（`world.status.reports` / `world.status.announcements`）对应的渲染控件直接显示中文，覆盖英文原文。

## 范围

### 1. 公告捕获（已有，需增强）
- 已有：每 12 帧轮询 `world.status.reports` + `world.status.announcements` 双容器
- 已有：eventful.onReport（虽然 v53 失效，但保留兼容）
- 增强：捕获 announcement 控件矩形（DFHack `dfhack.gui.getPanelLayout()` 或 widgets 遍历）

### 2. announcement_inline overlay widget
```lua
AnnouncementInline = defclass(AnnouncementInline, overlay.OverlayWidget)
AnnouncementInline.ATTRS = {
    desc = 'DF-FanYi 公告面板内嵌翻译',
    default_pos = {x=0, y=0},
    default_enabled = true,
    -- 公告面板在主游戏视图 (Dwarf Mode / Adventure Mode) 显示
    viewscreens = {'dwarfmode', 'adventur'},
}
function AnnouncementInline:init()
    self.frame = {...}
end
function AnnouncementInline:onRenderFrame(painter, rect)
    -- 1. 读公告面板矩形（右下角 Embark 按钮上方）
    -- 2. 读 S.announcement_cache（key: announcement_id）
    -- 3. 区域内贴最新 N 条公告的中文（保留 2-3 条历史，避免新公告闪烁）
end
```

### 3. 公告自动过期机制
- 每条公告 translation_ttl_ms 90s（与原字幕条 TTL 一致）
- 缓存清理：90s 后从 S.announcement_cache 移除，overlay 不再显示该条（原文自然显示，避免翻译永远挂）

### 4. 翻译引擎路由
- 复用 ticket-013 的 `inline_translate(text, context='announcement')`
- 公告通常较短（<100 字），无需切句

## 实现要点

**公告面板矩形获取**
- 公告面板在主视图右下角，宽 ~40 字符、高 ~10 行
- DFHack API：`df.global.world.status.announcements[].text` 可读出原文
- 面板 widget：DFHack 公开 API 难定位；用相对位置 `{x=screen_w-40, y=screen_h-15}` 兜底（已知位置）

**贴图**
- 复用 ticket-013 生成的字形图集
- 公告矩形宽 ~40 → 每行 20 中文字

**多公告显示策略**
- 最新公告占满矩形（高优先级）
- 历史 2 条显示在面板下方（小字或缩短）
- TTL 到期后整条从缓存清除

## 验收
- [ ] fanyi.lua 启动后 OVERLAY_WIDGETS 注册 announcement widget
- [ ] `fanyi status` 输出包含 `announcement_inline (on/off)`
- [ ] 重启游戏 → 建要塞 → 任意公告（eg 钓鱼/公鸡/天气变化）→ 公告面板矩形内显示中文
- [ ] 90s 后历史公告自动消失
- [ ] 单测：缓存 get/set/expire 测试通过（与 013 共用测试组件）
- [ ] hack harness 单元测试：`AnnouncementInline:onRenderFrame(dc, 40, 10)` 在 mock announcement 数据下输出非空
- [ ] 提交 + 推送

## 风险
- 公告面板矩形与右下 Embark 按钮冲突 → 测试布局微调 default_pos；若仍冲突，widget 加 click_passthrough=true 不拦截鼠标（DFHack overlay 默认行为）
- 公告频繁触发（每帧可能有 0-3 条）→ max_send_per_tick=3 + 队列已实现；新增 translation queue 复用

## 估计
- fanyi.lua 新增：~200 行
- 单测：~80 行
- 提交：1 commit + push

## 备注
公告是动态翻译的主战场（用户最关心"我刚才发生什么"）。本 ticket + ticket-013 必须严格确保"无字幕条残留"——E2E 验证在 ticket-016。