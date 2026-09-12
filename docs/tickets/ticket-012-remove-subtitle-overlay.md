# ticket-012 — 删除字幕条 overlay

> 父 spec：SPEC-012 · 状态：done · 优先级：P0（必须先做）
> 依赖：无 · 被依赖：013, 014, 015, 016

## 目标
彻底删除 fanyi.lua 字幕条 overlay（屏幕底部独立字幕条 widget），让游戏画面回归干净。

## 范围

### 必须删（fanyi.lua）
- `fanyi_render_lines()` 函数（行 475-）
- `fanyi_paint_subtitle()` 函数（行 698-）
- `FanyiSubtitle = defclass(...)` 类（行 781-）
- `OVERLAY_WIDGETS = {subtitle = FanyiSubtitle}`（行 806-）
- `S.render_lines`、`S.render_cjk`、`S.font`、`S.overlays_on` 状态字段
- `dfhack.textures` 调用（CJK 贴图逻辑）
- CJK 字形图集加载/初始化代码
- fanyi 命令的 `overlays on|off [cjk]` 子命令（保留兼容 alias 但功能禁用）

### 必须删（文件）
- `hack/data/fanyi-font/` 整个目录（含 4 页 3877 字形贴图）
- `dfint-data/fanyi-font/` 整个目录（引擎侧备份）

### 必须保留
- 所有捕获代码（announcement + reports + textviewer + History + 双容器轮询）
- JSON-RPC 客户端 + 引擎通信
- 断线自愈（指数退避）
- 版本守卫
- `fanyi status|start|stop|debug|clear` 命令
- `fanyi_render_lines` 改名 `fanyi_render_inline_payload` 返回空（保留函数签名供后续 ticket-013 复用）

### 必须改（fanyi 命令）
- `overlays on|off [cjk]` → `overlays on|off [textviewer|announcement]`（alias 旧命令 → 提示"字幕条已删除，使用 textviewer 或 announcement"）
- `status` 输出删除 "字幕条" / "subtitle" 行；改为 "subtitle widget: removed (replaced by inline overlays)"

## 验收
- [ ] `fanyi status` 输出不包含 "subtitle" / "字幕条"
- [ ] `hack/data/fanyi-font/` 不存在（git tracked 移除）
- [ ] `dfint-data/fanyi-font/` 不存在
- [ ] 单测 `test_fanyi_no_subtitle.py` 通过：fanyi_render_inline_payload() 默认返回 {}
- [ ] 单测 `test_overlay_widgets.py` 通过：OVERLAY_WIDGETS 不包含 subtitle 键
- [ ] hack harness 启动 fanyi.lua → 加载无错（无 dfhack.textures 警告）
- [ ] 提交 + 推送

## 风险
- 字幕条是当前唯一可见的动态翻译通道；删除后若无 013/014 接管，玩家会觉得"翻译没了"。**必须在同一 commit push 前确保 013/014 已 ready**——但允许 012 先合入，013/014 紧跟；E2E 在 016 完成。
- 删除图集后某些 fallback 路径（如 `S.font.installed` 检查）引用会变 nil；自检需覆盖。

## 估计
- 代码删除：~150 行
- 单测：~80 行
- 提交：1 commit + push

## 备注
本 ticket 是"清理"型变更，单独看测试通过很容易；但**真正的验证在 ticket-016 E2E**——必须确保 013/014 在 012 之后立即接续，不要让玩家在中间状态运行游戏。