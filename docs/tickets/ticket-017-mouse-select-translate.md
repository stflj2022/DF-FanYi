# ticket-017 鼠标选词翻译 (F11)

> 父 spec: [SPEC-017](../specs/SPEC-017-mouse-select-translate.md)
> 状态: pending
> 优先级: P1 (玩家主动召唤, 覆盖所有视图的兜底)

## 范围
- fanyi.lua: MouseSelect widget + S.ms state + `fanyi mouseselect`/`mousedismiss` 命令 + F11/F12 keybinding 动态注册
- tests: 新增 tests/test_mouse_select.py (无头, mock mouse + screen buffer)
- harness: fanyi_harness.lua 扩展 mock dfhack.gui.getMousePos + df.global.gps.screen
- 同步到游戏目录 / 测试 420+ 全绿 / 提交推送

## 验收
- [ ] F11 在游戏内触发, 浮窗出现翻译
- [ ] F12 关闭浮窗
- [ ] 无文本时显示 "无文本可翻译"
- [ ] pytest 全绿 (含新测试)
