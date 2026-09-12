# SPEC-017 鼠标选词翻译 (MouseSelect)

> 2026-09-12 玩家主动召唤翻译: 游戏内按 F11 → 鼠标所在行英文自动翻译 → 中文浮窗驻留屏幕直到手动关闭。

## 1. 动机

公告/textviewer 覆盖层只在已知视图类型内生效。但 DF 53.06 还有大量角落
(tooltips/菜单项/临时浮窗/库存名/任务提示) 出现英文/半英。
玩家遇到任何英文时希望 **手动召唤一次翻译** 而不靠自动捕获。

## 2. 设计

### 2.1 触发
- **F11** (dwarfmode / dungeonmode / adventur / textviewer / default 全局)
  → 读 `dfhack.gui.getMousePos()` 取屏幕鼠标坐标
  → 拼出"鼠标所在行"的整段英文
  → 调引擎 `inline_translate({text, context='mouse_select'})`
  → 在屏幕右下角画浮窗, 显示中文 + "按 F12 关闭"
- **F12** → 关闭浮窗

注册: 在 `hack/init/dfhack.keybindings.init` 加
```
keybinding add F11@dwarfmode|dungeonmode|default|adventur|adventur_interact "fanyi mouseselect"
keybinding add F12@dwarfmode|dungeonmode|default|adventur|adventur_interact "fanyi mousedismiss"
```

### 2.2 屏幕字符读取
DFHack 53.06 提供 `df.global.gps.screen[y][x]` (1-based) 字符缓冲。
鼠标坐标转屏幕格 → 取整行 (`screen[y][1..maxw]`) → 拼串 → strip 颜色码/制表符。

### 2.3 widget 渲染
新增第 3 个 overlay widget `MouseSelect` (fanyi.mouse_select):
- `default_enabled=true`
- `viewscreens` = 与 013/014 合并的全集 (dwarfmode/adventur/textviewer/default)
- `frame = {l=0,t=0,r=0,b=0}` 全屏裁剪
- onRenderFrame 画右下角浮窗: 矩形框 + 多行中文 + `[F12] 关闭`

### 2.4 浮窗驻留
状态 S.ms_visible: { active, text, translation, time, lines, w, h }
- mouseselect 命令进入 loading → 显示 "翻译中..."
- fetch_done 收到译文 → 写入 translation → 显示完整中文
- mousedismiss 命令 → 清空状态 → 重画时 return 0 (浮窗消失)

## 3. 验收
- F11 → 浮窗出现 (loading → 译文)
- F12 → 浮窗消失
- F11 在不带英文的位置 → 显示 "无文本可翻译"
- 无头测试 (tests/test_mouse_select.py) 覆盖: 字符提取/翻译路径/浮窗渲染/关闭
- 420+ pytest 全绿

## 4. 文件
- `dfhack/scripts/fanyi.lua` 新增 MouseSelect widget + S.ms state + 命令处理
- `tests/lua/fanyi_harness.lua` mock dfhack.gui.getMousePos + df.global.gps.screen
- `tests/test_mouse_select.py` 新增
- 游戏目录 `hack/init/dfhack.keybindings.init` (在 fanyi.lua onLoad.init 中动态注册)

## 5. 不做
- 鼠标拖框选区 (DFHack overlay 框架不支持 click-through 选区; 范围=整行是 80% 场景的合理默认)
- 浮窗多历史 (一次一个, 关闭后下次 F11 再开)
- 浮窗样式主题 (固定灰底白字, 与游戏暗色 UI 一致)
