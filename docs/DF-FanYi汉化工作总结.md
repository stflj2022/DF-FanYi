# DF-FanYi 汉化工作总结（2026-08 ~ 2026-09-09）

> 本文档记录矮人要塞 v53.06 中文汉化方案的全部工作、关键技术决策与遗留问题。
> 新用户安装请看《中文版矮人要塞安装指南.md》（同目录）；本文是"做了什么、为什么"。

---

## 一、方案总览

矮人要塞 Steam 版（v53.06）的中文汉化采用**双层架构**：

| 层 | 组件 | 职责 |
|---|---|---|
| 静态层 | **dfint**（legacy 模式） | 钩住游戏字符串渲染函数，查词典（24764 行）完全匹配替换，覆盖 UI/界面/已知文本，无需联网 |
| 动态层 | **DF-FanYi 引擎**（Python）+ **DFHack fanyi.lua** 桥 | DFHack 每帧捕获公告/事件文本 → JSON-RPC 推给常驻引擎 → 云端 L2 翻译（本地 ollama 兜底）→ 回写 → 底部字幕条渲染中文 |

两层互补：静态层管"看得见的固定文字"，动态层管"玩起来才出现的活文字"（公告、事件、日志）。

## 二、组件版本

- 游戏: Dwarf Fortress v53.06（Steam 整合包，Goldberg emu，GE-Proton11-6 运行）
- DFHack: 53.06-r1（手动安装到游戏根目录）
- dfint: legacy 20251127（+ 词库多次补齐，最终 24764 行）
- DF-FanYi: https://github.com/stflj2022/DF-FanYi （main = a081666）
- 翻译引擎依赖: Python ≥3.11 + pyyaml + requests

## 三、已完成工作

### 1. DFHack 手动安装 + 桥接（已验证）
- DFHack 53.06-r1 手动安装（hack/、stonesense/、*.dll 复制进游戏根）
- fanyi.lua 挂入，`dfhack-run.exe "fanyi" "status"` 实测引擎在线（127.0.0.1:17486）、CJK 3877 字形
- **教训**: dfhack-run 子命令必须分开多参数（`"fanyi" "status"`，不能写整串）

### 2. 全自动闭环（人工零参与）
```
开机 → systemd user 服务起引擎桥（Restart=always 自愈）
     → Steam 启动游戏 → DFHack 自动加载
     → dfhack.init 自动 fanyi start（开启捕获）
     → onLoad.init 自动 fanyi overlays on cjk（世界加载后开字幕）
     → 断线时 fanyi 指数退避自动重连（实测 12s 内恢复）
```
- 服务文件: `~/.config/systemd/user/df-fanyi-bridge.service`
- 常用命令: `systemctl --user restart df-fanyi-bridge`

### 3. 修复的 bug（按发现顺序）

| Bug | 根因 | 修复 |
|---|---|---|
| fanyi.lua 版本守卫永不通过 | getDFVersion() 返回 `v0.53.06 win64 STEAM` 带前缀，`==` 精确比对失败 | 改 `string.find(dfver, "53.06", 1, true)` 包容匹配（b5c61bb） |
| 桥报 -32700 JSON parse error | DFHack 捆绑的 json.encode 输出多行缩进 JSON | fanyi.lua rpc_request 里 gsub 压缩成单行（5da84b5） |
| 公告中英掺杂 | 实时翻译硬编码走本地 ollama（不可达）→ 超时回退原文 | pipeline.py 改读配置：云端 router 优先，本地 ollama 兜底（0a0b6c8） |
| **公告捕获恒为 0**（翻译"不完全"的真相） | v53.06 公告走 `world.status.announcements` 容器，eventful.onReport 只监听 `world.status.reports`（且 report 短命过期即清） | fanyi.lua 每 10 tick 直接 diff 双容器新增 id，不依赖 eventful；注入测试验证链路 5/5 成功（02671aa） |
| overlay widget not found | dfhack.init 阶段 overlay 框架还没扫描脚本 | `fanyi overlays on cjk` 从 dfhack.init 挪到 onLoad.init（世界加载后执行） |
| 工具提示（tooltips）英文残留 | exe 内嵌句串词典缺 1121 条（覆盖率仅 72%） | strings 抽取 exe 全部 4801 句串 → 对照词典找缺失 → 云端批量翻译 1121 条（0 失败）→ 词典 23643→24764 行（a081666） |

### 4. 翻译库建设
- dfint legacy 词典补写至 24764 条（只补 ≥20 字完整句防污染；最后一轮 1121 条来自 exe 句串全量对照）
- 引擎翻译记忆（engine.db）: 5886 条 TM + 4708 条术语（pretranslate 批量入库）
- 离线翻译包: 游戏目录 `DF-FanYi-离线翻译包/engine.db`（sqlite backup 干净导出——**不能裸 cp，WAL 会丢数据**）

### 5. 分发打包
- 中文版安装指南写入游戏目录（安装/运行/引擎配置/FAQ 全流程）
- 游戏目录打包为 **Cryptomator 加密保险库**（vault 密文形态分发，破解协议文件不可见）
- 7z 分卷压缩 14 卷 × 90M（`矮人v5306.7z.001~014`）
- 已推送 GitHub（分两部分）:
  - https://github.com/stflj2022/DF-1 ← 卷 001~007
  - https://github.com/stflj2022/DF-2 ← 卷 008~014
  - README/commit 只写"备份文件"
- **还原流程**: 14 卷下载同目录 → `7z x 矮人v5306.7z.001` → 得 Cryptomator vault → 凭 vault 密码挂载 → 明文游戏

## 四、关键技术要点（给维护者）

1. **Proton 下路径必须纯 ASCII**：中文/特殊字符路径 → "Tileset not found" 黑屏
2. **版本守卫用 find() 不用 ==**：DF 版本串带前缀
3. **JSON 必须单行**：DFHack json.encode 多行输出会炸 JSON-RPC
4. **53.06 公告在 announcements 容器**：eventful 只覆盖 reports，需自轮询双容器
5. **overlay 小部件要等框架扫描**：dfhack.init 太早，onLoad.init 才稳
6. **词典完全匹配**：短句/词组不能盲目入词典（会污染其他文本）
7. **engine.db 导出必须走 sqlite backup()/VACUUM**，裸 cp 在 WAL 模式下丢数据
8. **dfint 词典游戏启动时加载**：改词典需重启游戏生效
9. **系统 luac 5.5 会误报** "assign to const variable"（for 变量 const 化），DFHack 用 Lua 5.4 不受影响

## 五、2026-09-10 排查：为什么玩家感觉“毫无变化”

**结论：两天的工作都在，但玩家看到的所有游戏时段用的都是旧组件。** 时间线还原：

| 时间(9/9) | 事件 | 玩家看到 |
|---|---|---|
| 15:59 | 建要塞，欢迎公告被 gamelog 回溯捕获 → **翻译 FAILED**（当时管线硬编码 ollama 且 ollama 不可达超时） | 欢迎公告英文 |
| 16:29~18:44 | 长会话：跑的仍是**旧 lua**（02671aa 18:24 才写入，需重启游戏才加载）+ **旧词典**（1121 条增量 19:36 才合并） | tooltip/菜单说明仍是原样 |
| 19:36 | 词典合并 23643→24764 行 | —— |
| 19:37~19:44 | 新字典+新 lua 生效的唯一会话，但只有 7 分钟菜单+建世界（无欢迎弹窗、无 tooltip 逗留） | 接近“无变化”的观感 |

另验证：
- 词典合并完整：extras 1121 条 0 缺失；引擎 FAILED 任务未污染 TM（无残留英文缓存）
- 新 lua 的公告容器捕获已在真实会话验证（18:37 钓鱼/18:40 公鸡 双容器捕获+翻译成功；19:38 TM 秒回）

## 六、2026-09-10 变更：翻译全部走云端

- 用户指令：**不再用本地模型翻译，全部走云端**
- `pipeline.py::_FallingBackLLM`：`local_llm.enabled=false` 时不再构建 ollama 兜底（15:59 欢迎公告 FAILED 的根因就是 ollama 超时）；云端失败由 worker 重试 3 次后回退原文
- `config/default.yaml`：`local_llm.enabled: false`，providers 里 ollama `enabled: false`
- 桥服务已重启生效；E2E 实测：欢迎公告句子/公告句 → router/L2 → 中文 ✓（TM 已入库）
- **待玩家验证（重启游戏后）**：①建世界/建要塞时欢迎公告应在底部字幕条出中文（公告面板内原文位置不替换，设计如此）②建筑/工坊 tooltip、菜单说明应基本全中文；仍英文的把原文发出来继续补词典

## 七、当前状态与遗留

### 已验证可用
- 游戏 + DFHack + dfint + 引擎桥全链路运行
- 公告实时翻译（云端 router 优先，本地兜底）
- 字幕条中文渲染（CJK 3877 字形）
- 分发包已推送 GitHub

### 遗留事项
- [x] **重启游戏后验证**（2026-09-10 引擎侧已全部就绪+实测，待玩家进游戏确认）：①公告欢迎词字幕条中文 ②tooltip 基本全中文；漏网的把英文原文发出来继续补
- [x] 本地 ollama 退役（2026-09-10，用户指令）：云端 router 唯一 LLM，见“六”
- [x] **exe 句串第二轮补齐**（2026-09-10， d4a49d3）：4747 条 router/L2 批量翻译 0 失败，游戏词典 24763→29510 行；重跑提取器验证待译=0。仍不覆盖：<20 字符短词(留给 lookups)、运行时拼接串(键永不相配, 无害)。可复现脚本 extract/translate/merge-exe-strings.py
- [ ] **分发包更新**: 词典增量/公告捕获修复/纯云端变更晚于打包，分发最新版需重新压缩 vault 并推送（14 卷覆盖 DF-1/DF-2）
- [ ] 可删备份: `Dwarf Fortress.bak-dfhack-20260909`（1.1G，验证公告修复后再删）
- [ ] Linux 原生截图在 fullscreen XWayland 游戏下会卡（tensaku/wl-copy 锁死，游戏本体无恙，kill 残留进程即恢复）；**游戏内截图建议用 Steam F12** 或窗口化后再截

## 八、2026-09-10 深挖：欢迎弹窗“词沙拉”真因 + 动态层接管

**现象**：新建要塞欢迎弹窗（Welcome to Dwarf Fortress / Prepare to guide your stout charges...）正文中英夹杂（“Prepare 至指引 你的肥硕...”）。

**根因（dfint 日志实锤）**：v53 新 UI 对长段落是**按单词逐次 top_addst 渲染**的——dfint 日志里 'Welcome to Dwarf Fortress' 整串命中后，正文是 'to'→至、'guide'→指导、'your'→你的、'stout'→肥硕、'charges'→管治……逐词命中。词典路线**无解**：整段键永远不被查询（补再多也没用），词级匹配只能产出词堆。第二轮补齐的 4747 条整段键对 tooltip 等整串渲染场景有效，对这类弹窗无效。

**修复（e714ea7）**：fanyi.lua 新增 textviewer 捕获——轮询 viewscreen 链上的 `viewscreen_textviewerst`（欢迎向导/教程/帮助'?'整页文本），读 title+text 完整文本去重后推引擎整句翻译，译文走字幕条显示。字幕条新增 UTF-8 感知换行（空格优先断行，长译文多行、新内容优先）。另加 `fanyi-debug.log` 轻量文件日志（游戏侧无 dfhack-run 控制台，落盘排障）。

**弹窗内英文原样是设计限制**：动态层只写字幕条，不改弹窗内容。OCR 工具链备忘：`~/桌面/models/gemma2.sh` 可拉起本地 Gemma4 视觉模型（127.0.0.1:8081）用于截图识别，用完可杀（pkill -f llama-server）。

### 相关文件速查
| 文件 | 位置 |
|---|---|
| 安装指南（新用户看这个） | 游戏目录 `中文版矮人要塞安装指南.md` |
| 引擎源码 | `~/DF-FanYi`（Python，git 仓库） |
| fanyi.lua（游戏侧桥） | 游戏根 `hack/scripts/fanyi.lua`（与仓库同步） |
| dfint 词典 | 游戏目录 `dfint-data/legacy-dictionary.csv` |
| 引擎桥服务 | `~/.config/systemd/user/df-fanyi-bridge.service` |
| 翻译引擎日志 | `journalctl --user -u df-fanyi-bridge` |
| 词典增量存档 | 仓库 `dfint-data/legacy-extras-20260909.csv` |

---
*更新于 2026-09-10 · DF-FanYi main = e714ea7(textviewer 捕获) · 待玩家重启游戏验证欢迎弹窗字幕条*

## 九、2026-09-11 根治"中英文混杂"三件套

**用户痛点**：菜单说明/公告/弹窗中英混杂、词沙拉，反复补词典不理想。

**根因三层**（dfint-rust-cjk 源码 `src/translator/mod.rs` 实锤）：
1. **词沙拉**：v53 新 UI 长段落按单词逐次渲染，词典中 181 条虚词条目
   (the/of/to/your/and...含空格变体)被逐词命中 → "Prepare 至指引 你的肥硕"词堆。
   补再多整句键也无用——整段键根本不会被查询。
2. **整串漏网**：词典没有的串显示英文。静态 exe 抽取（第五/七节）覆盖不了
   运行时拼接串（人名+模板、raw 生成文本），且此前无收集手段。
3. **公告面板原文英文**：动态层只写字幕条不改画面（设计如此）。

**修复**：
| 项 | 内容 |
|---|---|
| 词沙拉 | `scripts/prune-dict-function-words.py` 删 181 条虚词；逐条审查后回补 15 条 UI 标签(All/Done/Off/No/Yes/Any/Other/Some/Might+空格变体)，白名单 UI_KEEP 两脚本同步。词典 29510→29344 行 |
| 漏网收集 | dfint 源码证实 `LOG_LEVEL:Debug` 会记录每条 `missing translation`（未命中串）→ `scripts/collect-untranslated.py` 解析+模拟 dfint 的 `", "` 拆分行为展开 part(模板键，如 "Farmer cancels Give water: Needs empty bucket")→ 过滤 → JSONL → `translate-exe-strings.py` 云端翻译 → `merge-exe-strings.py --write` 合并 → **重启游戏生效** |
| 词典机制备忘 | simple-dictionary.csv=USER 词典优先于 legacy；匹配=小写化完全匹配；未命中含 ", " 自动拆 part 递归；`(func,bt,string)` 三元组缓存 |

**闭环操作**（玩一局后执行）：
```bash
python3 scripts/collect-untranslated.py --rotate     # 收集+归档日志
python3 scripts/translate-exe-strings.py --in dfint-data/untranslated-*.jsonl \
                                          --out dfint-data/live-translated-*.jsonl
python3 scripts/merge-exe-strings.py --in dfint-data/live-translated-*.jsonl --write
# 重启游戏
```

**验证清单**（重启游戏后）：
1. 词沙拉消失：欢迎弹窗/长段落应显示**纯英文段落**（不再中英夹杂词堆）+ 底部字幕条中文
2. UI 标签完好：过滤菜单 All/Other、确认框 Yes/No、Done/Off 按钮仍中文
3. Debug 日志在跑：`dfint-data/dfint-log.log` 出现 DEBUG missing translation 行

**遗留**：词典有 1080 个大小写不敏感重复键（HashMap 后写覆盖先写，无害未清）；
simple-dictionary 有 "path."→空 等尾部清理条目（配合前缀模板设计，勿删）。

## 十、2026-09-11 晚：字幕条大字化 + 物品描述批量根治 + 闭环首跑

### 10.1 字幕条"字太小"根因与修复
- **物理根因**：字幕条逐字贴图集字形，旧图集每字压进 8x12 tile（汉字有效笔画区仅 7x11px），物理上无法看清；dfint 的 CJK_FONT_SIZE=24 与字幕条无关（那是它自己往游戏字体图集塞字形的尺寸）。
- **修复（大字图集 scale=2）**：`generate_font_atlas.py` 新增 `--scale 2`——每字光栅化 16x24、切 4 片（TL,TR,BL,BR）占 4 连续网格位（页宽 64 列被 4 整除保证不跨行）；`fanyi.lua` 按四 texpos 2x2 贴回，每字 16x24 像素。图集 4 页 3877 字形已重生成并部署游戏+仓库。
- **配套调整**：frame 42x5→64x10（视觉仍 5 行、每行 32 汉字）、default_pos y=-11、overlay.json 里残留的旧位置 {x:-2,y:-2}（右下，挡按钮高危位）改为 {x:0,y:-11}。
- **自动消失**：render_ttl_ms 90s→15s（用户"几秒后消失不挡按钮"），`FANYI_TTL_MS` 环境变量可覆盖；status 命令现打印 scale/ttl。

### 10.2 "铜矛说明还是英文"根因与批量根治
- **根因链**：物品 tooltip 说明句是**运行时拼接**（exe 内只有分段 `This is a `，武器名来自 entity 渲染）——静态抽取抽不到整串，词典精确匹配永远追不上组合；dfint Debug 日志铁证 `### "This is a copper spear.  "`（双尾空格，bt 走 addst）。
- **修复**：`scripts/gen-item-desc-dict.py` 按**实测句式** `This is a {材质} {物品名}.  ` 做笛卡尔积（13 武器级金属 × 25 武器 + 3 弹药 = 364 键）→ translate（router，28s）→ merge 入典。**护甲/鞋类句式无样本未证实，暂不生成**——等 Debug 日志闭环确认句式后用同一脚本扩。
- 词典 29344 → 29797 行；增量包 `dfint-data/legacy-extras-20260911.csv`。

### 10.3 闭环首跑（本轮用户 Debug 日志 → 词典全链路）
- collect 90 条（138 missing → 135 去重 → 过滤）→ translate 90/90 → 专名过滤（译文==原文 0 条）→ merge +90。
- 首批入典样例：`This is a copper spear.  `→`这是一杆铜矛。`、`Histories of Gluttony and Enterprise`→`暴食与创业史`、`12th Limestone`→`石灰岩月12日`。

### 验证清单（重启游戏后）
1. 字幕条：字形明显变大（16x24），15 秒无新内容自动消失；`fanyi status` 显示 scale=2
2. 悬停任意金属武器/弹药：说明句整句中文（钢/银/精金/铜×矛/战斧/短剑/弩箭…全覆盖）
3. 主菜单串（World name / Three saves 等）+ 字幕条中文
