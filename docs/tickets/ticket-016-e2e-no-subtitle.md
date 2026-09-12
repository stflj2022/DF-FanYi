# ticket-016 — E2E 验证：游戏内自然中文显示

> 父 spec：SPEC-012 · 状态：pending · 优先级：P0（必须最后做）
> 依赖：012, 013, 014, 015 全部 done · 被依赖：无

## 目标
重启游戏后，端到端验证所有游戏内文本自然显示中文，**无字幕条残留**，符合用户对"官方中文版"的观感要求。

## 验证清单（重启游戏后）

### A. 控制层验证
- [ ] `fanyi status` 输出：
  - 包含 `textviewer_inline (on)`
  - 包含 `announcement_inline (on)`
  - **不包含** `subtitle` / `字幕条`
- [ ] `fanyi overlays on|off` 命令存在并可切换 widget 开关
- [ ] DFHack `ls overlays` 仅显示 `fanyi.textviewer_inline` + `fanyi.announcement_inline`，无 `fanyi.subtitle`
- [ ] `hack/data/fanyi-font/` 不存在
- [ ] `dfint-data/fanyi-font/` 不存在（如果 015 重新生成 textviewer-font，应在 `hack/data/textviewer-font/`，不是 `fanyi-font/`）

### B. 功能层验证（重启游戏后人工测试）

**B1. textviewer 弹窗**（最高优先级）
- [ ] 新建要塞 → 欢迎弹窗（Welcome to Dwarf Fortress）：textviewer 矩形内显示中文（不是底部字幕条）
- [ ] 帮助菜单 → 任意 `?` 弹窗：textviewer 矩形内显示中文
- [ ] 教程页（建要塞后第一个教程）：textviewer 矩形内显示中文
- [ ] 离开弹窗 → 中文立即消失（widget 自动失效）
- [ ] 不出现词沙拉（按词 addst 不触发，整段翻译）

**B2. 公告面板**
- [ ] 主游戏视图（建要塞后）右下角公告面板：任意公告原文区域显示中文
- [ ] 历史公告 90s 后自动消失
- [ ] Embark/右侧菜单不被遮挡
- [ ] 鼠标点击 Embark 按钮正常工作（widget 不拦鼠标）

**B3. 长段落/动态文本**
- [ ] 不出现词沙拉（按词 addst 渲染场景）
- [ ] 段落缓存命中：同一 textviewer 第二次进入，0 LLM 调用（fanyi status 显示 cache_hit）
- [ ] 实时翻译延迟：公告/弹窗首次翻译 < 3s（云端 router/L2）

### C. 性能/资源验证
- [ ] fanyi.lua CPU 占用 < 2%（10 分钟采样，3 局游戏测试）
- [ ] 段落缓存命中率 ≥ 30%（2 次访问同一段文本）
- [ ] fanyi status 队列统计：pending 不堆积（实时翻译跟得上事件流）

### D. 代码质量验证
- [ ] 单测全套通过：`pytest tests/ -q` 0 failed
- [ ] 双遍 code-review 通过（standards + spec axes）
- [ ] hack harness 全场景通过：`pytest tests/test_fanyi_harness.py -q`
- [ ] git log 显示 4-5 个独立 commit（012-015 每个一提交 + 本 ticket 验收 commit）

## 验证方式

**A. 控制层验证**：直接运行 fanyi 命令 + ls + ls 文件系统
**B. 功能层验证**：玩家手动重启游戏（dfhack-run 或 Steam 启动）→ 走一遍流程
**C. 性能验证**：游戏内 `fanyi debug` 显示 CPU/队列/命中率
**D. 代码质量验证**：`pytest` + 人工 code-review

## E2E 失败应对
若 B 验证发现漏网场景（弹窗/公告仍英文）：
1. 立即创建 ticket-017 修复
2. 临时回滚：保留字幕条代码（git revert 012）作为兜底
3. 修复后再删除字幕条

## 提交
- 验收 commit: `docs(ticket-016): E2E 验证通过 + 验收报告`
- 推送 → 完工自停

## 备注
**这是 SPEC-012 唯一完工判据**。本 ticket done = SPEC-012 done = 整个"删除字幕条 → 自然中文显示"工程完工。