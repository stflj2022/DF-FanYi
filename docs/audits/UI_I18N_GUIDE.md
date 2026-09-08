# UI 实时汉化(dfint Lite)安装指南

> 2026-09-08 在本机(DF 53.16 + DFHack 53.16-r1.1, Steam Linux 二进制)落地成功。
> 目的: **让不懂英文的玩家看懂游戏 UI**(菜单/按钮/物品/生物名), 与 DF-FanYi 引擎(公告字幕/LLM)互补。
> 一键复现: `bash scripts/install-ui-i18n.sh`

## 为什么自编译

- 新版 DFI18n 工坊版需要 Steam 拥有游戏(本机是 Steam 二进制但非 Steam 拥有, 无 appmanifest)。
- 官方 Lite 整包只挂在 df.viz.link 网盘, **页面无公开下载直链**(2026-09 实测, raw/Jina/链接提取均确认)。
- Lite 的全部材料都在公开仓库: 源码 gitee `vizv/dfint-rust-cjk`(lite 分支) + 词典 gitee `vizv/df-translations` + 字体 notofonts/noto-cjk → 可完全自建。

## 原理(关键机制)

1. DF Steam Linux 版会自动加载游戏根目录的 `libdfhooks.so`(DFHack 的 **dfhooks 链加载器**)。
2. 链加载器读取 `dfhooks_dfhack.ini` 加载 DFHack, **并自动发现/加载根目录所有 `libdfhooks_{name}.so`**, 把 dfhooks API 转发给它们(官方 README: github.com/DFHack/dfhooks)。
3. 所以把 dfint 编译产物改名 `libdfhooks_dfint.so` 放入根目录 → **与 DFHack 自动并存, 零替换零冲突**(DF-FanYi 公告桥照常跑)。
4. dfint 用**内存模式搜索**(符号名定位, `dfint-data/offsets.txt` Linux 段)跨版本适配, 不依赖硬编码地址 → 53.16 直接可用。
5. 渲染走 TTF(自带 fontdue + Noto CJK 字体), 游戏内 **Ctrl+F2** 开关。

## 装机清单(本机现状, 2026-09-08 已装)

| 文件 | 来源 |
|---|---|
| `~/Games/DwarfFortress/libdfhooks_dfint.so` (4MB) | 源码自编译 (rustup nightly, retour 需 nightly) |
| `~/Games/DwarfFortress/dfint-data/` (19MB) | 见下 |
| ├ `offsets.txt` | 源码仓库 `data/` |
| ├ `config.txt` | 生成(Info 日志, 关 legacy 词典) |
| ├ `fonts/NotoSansMonoCJKsc-Bold.otf` | github notofonts/noto-cjk |
| ├ `simple-dictionary.csv` (167 条界面/帮助) | **转换生成**(见下) |
| ├ `lookups/` (27 词库: 技能/物品/材料/武器/任务…) | gitee df-translations/lookups |
| ├ `dictionaries/` (creatures/plants) | gitee df-translations/dictionaries |
| └ `legacy/user-*-dictionary.csv` 空占位 | 程序对缺文件直接 panic, 必须存在 |

**simple-dictionary 的由来**: `translations/`(interfaces.csv/help-texts.csv/help-documents.csv)表头是
`viewscreen,context,alignment,text,text_translation`, 而程序只认 `text,translation` → 脚本做了列转换。
这是 UI 菜单文本(玩家最需要的部分)的主要来源。

**代码事实**(读源码确认, 排障时用):
- `load_csv` 内 `File::open(...).unwrap()` → **缺任一必需文件直接崩**, 空表头占位即可。
- `lookups/` `dictionaries/` 目录 `read_dir().unwrap()` → 目录必须存在。
- `USE_LEGACY_DICTIONARY:NO` 时 legacy 词典不加载(官方提醒旧词典有误翻, 默认关)。
- 配置路径全部相对 CWD (`./dfint-data/...`) → **必须从游戏根目录启动**(启动器 `./dfhack` 已 cd, ✓)。
- 热键 Ctrl+F2 走 device_query/X11 → Wayland 下经 XWayland, 可用。

## 使用 / 回滚

- 启动照旧(`dwarffortress-classic` 启动器或 `cd ~/Games/DwarfFortress && ./dfhack`), 游戏内 **Ctrl+F2** 开关中文。
- 回滚: `rm libdfhooks_dfint.so && rm -rf dfint-data`(纯新增文件, 删了就还原)。
- 安装脚本自动备份 `dfhooks_dfhack.ini` + `libdfhooks.so` 到 `.backup-before-dfint-<时间戳>/`。

## 排障

1. 没变中文: 看 `dfint-data/dfint-log.log`(改 config.txt 的 `LOG_LEVEL:Debug` 更详细)。
2. 崩溃(官方声明测试版可能崩): 回滚两件套即可, 不影响存档结构; 另有 `.backup-before-dfint-*/`。
3. 游戏升级后失效: 先看日志; offsets 是模式搜索一般自适应, 真失效需更新 df-translations/源码重跑脚本。

## 画面美化:HEWN 图形包(2026-09-08 已装)

经典免费版只有 ASCII 字符画(原版 art 目录仅 curses 字形集, png 数=0)。旧一代 Phoebus/DFgraphics
已停更(最高支持 47.05), **v53 经典版现役方案是 HEWN**(DFFD id=17744, 目标 53.10, 经典版实测可用):

- 已装: `data/mods/HEWN`(模组本体 6.5MB) + `data/init/colors.txt` 配色 + `data/art/black_background.bmp`
  + `hack/data/art/`(DFHack 界面贴图), 备份在 `.backup-before-hewn-*/`
- **激活方式(经典版特有)**: 经典免费版**没有主菜单 Mods 入口**(付费版才有, interfaces.csv 有
  "Mods→模组"词条可证); 经典版在**创建新世界流程中**出现模组勾选步骤, 勾 HEWN → 生成世界。
  已存在的世界无法追加模组。也可在标题屏跑 DFHack `gui/mod-manager`。
- 装后建议把 设置→`Use Classic ASCII Glyphs` 设为 No, 否则强制回字符画
- 回滚: 删 `data/mods/HEWN` + 还原备份里的 colors.txt/black_background.bmp/hack-data-art
- 53.11~53.16 新增的少量对象暂无专属贴图, 会自动回退为字符, 属预期行为

## 已知限制

- 词库覆盖有限: 未收录文本保持英文(官方词库持续扩充, 重跑 `install-ui-i18n.sh` 即更新词典)。
- 旧词典误翻问题(如 ash log→灰烬原木)已通过关闭 legacy 词典规避, 代价是覆盖略降。
- 与 DF-FanYi 引擎分工: dfint=静态 UI 文本; 引擎=公告字幕/动态句 LLM(见 docs/audits/DFHACK_INTEGRATION.md)。
