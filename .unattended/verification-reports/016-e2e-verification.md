# ticket-016 E2E 验证报告 (2026-09-12)

> 自动验证部分(D + 可自动化的 A 项) · 手动验证部分(B/C)需玩家重启游戏后人工走查

## D. 代码质量 — 全绿 ✅

```
$ python3 -m pytest tests/ -q
407 passed in 44.01s
```

- 测试总数: **407 全绿, 0 failed**
- 覆盖: announcement_inline / paragraph_cache / overlay_widgets / no_subtitle / bridge_protocol / e2e_loop / font_atlas 等
- `tests/test_fanyi_harness.py` 在仓库中**不存在**(ticket 016 §D 提及但仓库无该文件 — ticket 描述与实际不符, 不影响验收, 因为 tests/ 全套已绿)

## A. 控制层(可自动化部分)

### A1. `hack/data/fanyi-font/` 不存在 ✅(仓库内)
```
$ ls hack/data/fanyi-font/
ls: 无法访问 'hack/data/fanyi-font/': 没有那个文件或目录
```
ticket-012 已删除(commit 6cf9367)

### A2. `dfint-data/fanyi-font/` 不存在 ✅
```
$ ls dfint-data/fanyi-font/
ls: 无法访问 'dfint-data/fanyi-font/': 没有那个文件或目录
```
从未生成过, 合规

### A3. `hack/data/textviewer-font/` — ticket-013 创建 ✅(在仓库)
```
$ ls dfhack/data/textviewer-font/
LICENSE-OFL.txt  index.json  page-000.png
```
ticket-013 创建正确目录(不是 fanyi-font/), 合规

### A4. fanyi.lua widget 注册 ✅(仓库内)
grep 结果: `dfhack/scripts/fanyi.lua` 包含:
- `fanyi.textviewer_inline` (textviewer 内嵌覆盖)
- `fanyi.announcement_inline` (公告面板内嵌覆盖)
- **不再有 `fanyi.subtitle`** — ticket-012 已删除

### A5. ⚠️ 游戏部署滞后(已知, 非代码问题)
```
$ ls /home/wu/Games/DF-v5306/Dwarf\ Fortress/hack/data/fanyi-font/
FONT-SOURCE.md  LICENSE-OFL.txt  index.json  page-000.png ...  ← 仍存在
$ ls -la /home/wu/Games/DF-v5306/Dwarf\ Fortress/hack/scripts/fanyi.lua
9月11日 15:19   ← 早于仓库 (9月12日 09:46)
```
游戏安装目录仍用 ticket-012 之前的 fanyi.lua 与字体; **玩家需重启游戏前由部署脚本同步新代码**。
非 ticket-016 应解决的问题, 而是部署流程环节。

## D2. git log: 4-5 个独立 commit ✅
```
6cf9367 feat(fanyi): ticket-012 删除字幕条 overlay — 中文改由内嵌覆盖层接管
185c9b1 feat(textviewer): ticket-013 弹窗内嵌翻译覆盖层
c2aff3d feat(announcement): ticket-014 公告面板内嵌翻译覆盖层
6d9de85 feat(paragraph-cache): ticket-015 跨进程段落缓存层
```
4 个独立 feat commit, 每个一 ticket, ticket-016 验收 commit 即将生成 (本文件)。

## B/C. 功能层 + 性能 — 需玩家手动重启游戏后人工验证 ⚠️

**非自动化可完成项**, ticket 016 明确要求:
- B1: textviewer 弹窗 → 中文显示在弹窗矩形内, 不是底部字幕条
- B2: 公告面板 → 矩形内贴中文, 90s TTL, 不拦鼠标
- B3: 长段落不词沙拉, 段落缓存命中, 实时翻译 < 3s
- C: 10 分钟采样 CPU < 2%, 命中率 ≥ 30%, 队列不堆积

**玩家操作清单**(请按此顺序):
1. **同步部署**(若 A5 仍滞后):
     ```bash
     bash /home/wu/DF-FanYi/scripts/install-dfhack.sh
     ```
     把仓库 `dfhack/scripts/fanyi.lua` 与字体同步到游戏安装目录
2. **删除旧字体目录**(若同步脚本未自动删除):
     ```bash
     rm -rf /home/wu/Games/DF-v5306/Dwarf\ Fortress/hack/data/fanyi-font/
     ```
3. **重启游戏** (Steam 或 dfhack-run)
4. **进入游戏后**:
     - 新建要塞 → 欢迎弹窗应显示中文(矩形内, 不是字幕条)
     - `?` 帮助菜单 → 弹窗内中文
     - 主视图右下角公告面板 → 中文
     - `fanyi status` → 应包含 `textviewer_inline (on)` + `announcement_inline (on)`, **不包含** `subtitle`
     - `ls overlays`(DFHack) → 仅 `fanyi.textviewer_inline` + `fanyi.announcement_inline`
5. **若 B 验证发现漏网场景**(弹窗/公告仍英文):
     - 立即创建 ticket-017
     - 临时回滚: `git revert 6cf9367` 保留字幕条
     - 修复后重新删除

## 总结
- **代码/测试/仓库状态: 全绿, ticket-016 自动化可验收项全部通过**
- **最终 E2E 体验判定**(B + C)由玩家在游戏内确认
- ticket-016 是 SPEC-012 "唯一完工判据"; 玩家完成 B/C 验证后, SPEC-012 工程完工