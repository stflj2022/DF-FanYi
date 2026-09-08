# 复用审计:DFI18n / dwarf-fortress-chinese / DFHack 53.16 官方能力

来源: 工程书 §2.1「不重复造轮子」· ticket-002-reuse-audit
审计日期: 2026-09-08 · 目标版本: DF 53.16 classic + DFHack 53.16-r1.1

> 方法: 网络检索 + shallow clone 源码审计(`/tmp/dfi18n-audit`、`/tmp/dfi18n-data-audit`、
> `/tmp/dfzh-audit`、`/tmp/dfhack-docs`)+ 本机 DFHack 安装实物核对
> (`~/Games/DwarfFortress/hack/`)。关键结论均标注代码/文档出处。

---

## 0. 一句话结论

**两个现成中文方案(DFI18n、dfzh)的「捕获 + 渲染」机制都是直接挂钩游戏内存/函数
(ELF 内存偏移搜索、Windows Detours),与工程书「禁止内存偏移、游戏侧只用 DFHack
官方 API(Lua/eventful/overlay)」的强制约束冲突,不能照搬;但其「两层翻译引擎
(词典 + 规则集)」思路、词典/规则/字体**数据**可以复用,DFHack 官方 API
(eventful/overlay/screen/dfhack-run)正是本引擎应走的路线。**

---

## 1. 调研对象清单

| 项目 | 仓库/文档 | 性质 |
|---|---|---|
| DFI18n | https://github.com/DFI18n/dfi18n | Rust 翻译 mod(beta),Steam 创意工坊发布 |
| DFI18n Data - 简体中文 | https://github.com/DFI18n/dfi18n-data-zh-hans | 简中翻译数据(词典/规则集/字体) |
| dwarf-fortress-chinese (dfzh) | https://github.com/wodzys/dwarf-fortress-chinese | DFHack C++ 插件,实时汉化 |
| DFHack 53.16 官方能力 | 本机 `~/Games/DwarfFortress/hack/` + https://docs.dfhack.org/en/latest/docs/dev/Lua%20API.html、`docs/dev/overlay-dev-guide.html`、`docs/dev/Remote.html`、`docs/plugins/eventful.rst` | eventful / overlay / Lua screen API / dfhack-run |

---

## 2. Q1: 这些项目如何捕获文本、如何渲染中文?用了哪些 API?

### 2.1 DFI18n —— 挂钩游戏文本绘制函数(内存偏移 + SDL2 钩子)

- **捕获**:直接挂钩 classic/Steam 文本绘制的三个底层函数 `addst`、`addst_flag`、
  `addcoloredst`(见 `dfi18n/src/hooks.rs` 中同名 hook 函数)。挂钩方式是通过
  **扫描游戏二进制 ELF 符号/模式得到函数偏移**后 patch(`crates/executable/src/elf.rs`
  用 `goblin` 解析 ELF 并 `function_offsets()`,README 自述沿袭 dfint/search_offsets
  与 IDA FindFunc 的记忆体搜索技术)。原始字符串以 `cp437_string` crate 解码
  (DF 文本是 CP437 编码),翻译后**用空格串调用原函数把原文盖掉**,再在自绘层画出译文。
- **渲染**:SDL2 钩子 + 纯 Rust 光栅化器 `fontdue` 把 TTF 字模画到屏幕
  (依赖里 `fontdue = "0.9.3"`、`sdl2-sys` crate;字体数据来自数据包
  `fonts/zh-Hans/NotoSansMonoCJKsc-Bold.otf`,18MB)。`dfi18n/src/screen.rs`、
  `glyph.rs`、`cjk.rs` 负责屏幕层/字形/中日韩处理。
- **API 性质**:自研内存挂钩,**不是 DFHack 官方 API**。README 注明需要安装 DFHack,
  但仅作为兼容环境(其 Known Issues 提到 "The DFHack overlay may sometimes render
  incorrectly"——它会与 DFHack 自绘 UI 打架)。
- **已知缺陷**(README Known Issues):可能掉 FPS;markup 基本不支持(仅 Help 文本);
  多行彩色文本处理错误;Tab 宽度错误;省略号/缩写词不翻译。

### 2.2 dfzh(wodzys/dwarf-fortress-chinese)—— Windows Detours 挂钩 SDL2 + SDL2_ttf

- **捕获**:注册为 DFHack 插件(`dfzh/dfzh.cpp` 中 `DFHACK_PLUGIN("dfzh")`,
  `plugin_init/plugin_enable/plugin_onstatechange`),但文本取自**游戏内存**,
  关键帧处理挂在 **SDL2 渲染函数上**:用微软 **Detours** 挂钩 `SDL_RenderPresent`、
  `SDL_PollEvent` 等(`dfzh/sdl2_hooks.cpp` 通过 `GetModuleHandle(TEXT("SDL2.dll"))`
  动态装载;`dfzh/hooks.cpp` 中 `ATTACH_SDL_HOOK(SDL_RenderPresent)`),
  `SDLRenderPresent` 前调用 `ScreenManager::preSDLRenderPresent()` 绘制译文。
- **渲染**:动态加载 **SDL2_ttf**,`dfzh/ttf_manager.cpp` 的 `TTFManager::init()`
  依次 `LoadSDL2TTF → TTFInit → LoadFont(Config::getFontFile(), 20)`;
  自带字体 `data/fonts/MapleMonoNL-CN-{Regular,Bold,Light}.ttf`。支持多字号、
  颜色保留(动态改色)、字符级句子重组(`sentence_detector.cpp`)。
- **API 性质**:DFHack 插件外壳 + **Windows 私有 API(Detours)+ 直接读游戏内存**,
  非 DFHack 官方绘制 API;**仅 Windows 平台**(README badge「Platform-Windows」,
  sdl2_hooks.cpp 全部是 Win32 API)。

### 2.3 小结对照

| | 捕获方式 | 渲染方式 | 用的 DFHack API |
|---|---|---|---|
| DFI18n | 二进制内存偏移挂钩 addst/addcoloredst | SDL2 钩子 + fontdue 光栅化 TTF | 无(仅共存) |
| dfzh | 直接读内存 + Detours 钩 SDL2 | SDL2_ttf 光栅化 TTF | 仅插件生命周期 |
| 本引擎(目标) | DFHack Lua + eventful/viewscreen 事件 | DFHack overlay + screen 绘制 API | eventful/overlay/screen/dfhack-run |

---

## 3. Q2: classic(非 Steam)支持吗?字符集/字体方案(CP437? TTF?)

### 3.1 classic 支持度

| 项目 | classic 支持结论 | 依据 |
|---|---|---|
| DFI18n | **不保证**:仅测试过 Steam 53.08(Windows + Linux),README 原文 "It may also work with the Itch or Classic versions, but this is **not guaranteed**";且其内存偏移方案对版本极敏感(每次 DF 更新需重新找偏移) | dfi18n README "Supported Versions" |
| dfzh | **不支持**:README 明确 "Steam version";发布物仅 `dfzh-v*-win64.zip`;v0.8.3 兼容 DFHack 最高 **53.15-r2,尚无 53.16 版本**(本机 53.16-r1.1 超出其兼容矩阵) | README + Releases 页(53.15-r2/53.14/53.13…) |

### 3.2 字符集/字体方案

- **classic 53.16 现状(本机实测)**:
  - 位图字库:`data/init/init_default.txt` 中 `[FONT:curses_640x300.png]`、
    `[FULLFONT:curses_640x300.png]`、`[BASIC_FONT:curses_640x300.png]`,
    即 **CP437 字符集的 PNG 位图字库**;本机配置字号 8x12,1920x1080 下网格 240x90
    (人工验证事实,见 docs/audits/ENVIRONMENT_AUDIT-FACTS.md)。
  - **init_default.txt 中不存在 `[TRUETYPE:]` 配置项**;DF Wiki(Graphics 页)确认
    classic 用 IBM Code Page 437 位图字符集,现代版界面文本可用 TrueType 字体渲染,
    但那是 Steam/新版特性,classic 53.16 是否支持 `[FONT:*.ttf]` **需 ticket-008 实测**。
- **两个项目的字体方案**:都放弃改游戏字库,改为**外部 TTF + 运行时光栅化**:
  DFI18n = `NotoSansMonoCJKsc-Bold.otf`(SIL OFL);dfzh = `MapleMonoNL-CN-*.ttf`
  (SIL OFL 1.1)。这印证:在 8x12 位图网格上直接铺 3000+ 汉字字形不可行
  (CP437 只有 256 槽),CJK 必须走 TTF/纹理覆盖渲染。

---

## 4. Q3: 我们能直接复用哪些组件?

| 组件 | 复用结论 | 理由 / 出处 |
|---|---|---|
| **词典数据**(术语表) | ✅ **复用/改造**(seed 数据) | dfi18n-data `simple/zh-Hans.csv` 901 行(格式 `text,translation,tags`);
dfzh `dfzh_dict_exact.csv` 1884 行 + `dfzh_dict_word.csv` 368 行。含
"Continue active game→继续游戏""Quickstart guide→快速入门指南"等静态 UI 高频词,
可直接转成我们 database/ 的 seed 词典(ticket-004 要求 50+ 术语,远够)。
注意:**CC BY-NC 4.0**(见 §6 许可) |
| **规则集数据**(模板句) | 🟡 **改造后引用** | 两项目均为 TOML 规则集(dfi18n-data 143 个文件;dfzh 181 个),语法类似
`"{name} cancels {job}." → 中文模板`、`{::items::material}` 递归引用(见
`rulesets/zh-Hans/items/altar.toml`、`creatures/caste.toml`)。与工程书 §19.1 的
规则引擎设计同构,但格式需转换、且只覆盖静态/组合文本。**不作为第一版依赖,作参考** |
| **字体文件** | ✅ **复用** | `NotoSansMonoCJKsc-Bold.otf`(SIL OFL)、`MapleMonoNL-CN-*.ttf`(SIL OFL 1.1)
都是自由许可的 CJK 等宽字体,可直接用于 ticket-008 的渲染方案(若走 TTF 覆盖) |
| **捕获层** | ❌ **自研** | DFI18n=内存偏移挂钩(工程书「禁止内存偏移」);dfzh=Windows Detours +
读内存(非 DFHack 官方 API、仅 Windows)。我们的路线:DFHack Lua +
eventful(`onReport` 等)+ viewscreen/overlay 钩子,零内存偏移 |
| **渲染层** | ❌ **自研** | 两项目的 TTF 覆盖渲染都依赖非 DFHack 官方钩子。我们的路线:DFHack
overlay widget + `dfhack.screen.paintTile/paintString` + `dfhack.textures.loadTileset`
(官方纹理注册 API);CJK 渲染方案在 ticket-008 落地 |
| **DFHack 官方基础设施** | ✅ **复用** | eventful / overlay / screen API / dfhack-run 已在本机安装
(`hack/plugins/eventful.plug.so`、`overlay.plug.so`,overlay 已在 dfhack.init 自动启用)
且是官方文档定义的稳定接口(见 §5) |
| **Pi Model Router** | 复用(接口) | 工程书 §12 接口签名为约束,不复制实现(本 ticket 不展开) |

---

## 5. DFHack 53.16 官方能力(本引擎可依赖的基础)

证据:本机 `~/Games/DwarfFortress/hack/` 实物 + DFHack 53.16-r1.1 官方文档
(Lua API / overlay dev guide / Remote / eventful,文档版本与 53.16-r1.1 一致)。

1. **eventful 插件**(`eventful.plug.so` 在位,`require "plugins.eventful"`):
   提供 Lua 事件,**`onReport(reportId)`** 正是公告/报告文本的来源
   (文档:report 发生频率比你想的高);EventManager 事件族需 `enableEvent(type, frequency)`
   启用,含 `onJobInitiated/onJobCompleted`、`onUnitDeath`、`onInventoryChange`、
   `onReport` 等 —— 与工程书 TextEvent 的 `screen: announcement` 场景直接对应。
2. **overlay 框架**(`overlay.plug.so` 在位,dfhack.init 已 enable):
   覆写 widget = 继承 `overlay.OverlayWidget`(→ `widgets.Panel`)的 Lua 类,
   `onInput/onRenderFrame/onRenderBody` 回调,绘制叠加在既有 viewscreen 之上
   —— 正是「译文异步叠加回 UI」所需,且自带启用/停用/位置管理 UI。
3. **Lua Screen API**:`dfhack.screen.paintString(pen,x,y,text)`、
   `paintTile(pen,x,y,char,tile)`、`fillRect`、`clear`、`invalidate`、
   `readTile`;pen 含 ch(字符)/fg/bg/tile 字段;`gui` 库(Painter/View/Screen/
   widgets)、**Textures 模块 `dfhack.textures.loadTileset(file,w,h)`** 可注册自定义
   字形纹理并返回稳定句柄。注意:绘制只在渲染回调(viewscreen onRender /
   overlay 渲染)中有效 —— 契合「主线程不等 LLM、异步刷新」的设计。
4. **dfhack-run / 远程接口**:TCP + protobuf RPC,默认 `127.0.0.1:5000`,
   配置 `dfhack-config/remote-server.json`(`allow_remote:false` 默认仅本机,
   `port` 可改,`DFHACK_PORT` 环境变量优先);`dfhack-run` 经 RPC 调 DFHack 命令
   /Lua。ticket-001 已实测游戏运行时 `./dfhack-run plug` 可用
   (见 ENVIRONMENT_AUDIT.md)—— 本引擎「unix socket JSON-RPC 桥」之外的
   外部命令通道备选。

---

## 6. 许可审计(复用数据的前提)

| 资产 | 许可 | 对本项目影响 |
|---|---|---|
| dfi18n-data-zh-hans 数据(词典/规则集/logo) | **CC BY-NC 4.0**(矮人要塞中文维基翻译组) | 可复用须署名、非商业;第一版 seed 词典如引入需在 database/ 记录来源与许可 |
| NotoSansMonoCJKsc-Bold.otf | SIL OFL 1.1 | 可自由使用/再分发(含商用),须保留版权声明 |
| MapleMonoNL-CN-*.ttf | SIL OFL 1.1 | 同上 |
| dfzh 代码 | MIT(仓库 LICENSE 头) | 可借鉴思路;但数据部分 README 标注 CC BY-NC 4.0 |
| dfi18n 代码 | 见其仓库 LICENSE(项目本身 beta) | 不采纳代码,仅思路 |

---

## 7. Q4: 结论表(组件 → 复用/改造/自研 + 理由)

| # | 组件 | 决策 | 理由(摘要) |
|---|---|---|---|
| 1 | 捕获层(游戏文本 → 引擎) | **自研** | DFI18n/dfzh 均用内存偏移/Detours,违反「禁止内存偏移、只用 DFHack 官方 API」;本引擎用 eventful + viewscreen 钩子 + unix socket |
| 2 | 渲染层(中文叠加显示) | **自研** | 现有方案的非官方挂钩不可移植;用 overlay + screen API + Textures;字体数据可复用(OFL) |
| 3 | 术语词典数据 | **复用(改造为 seed)** | dfi18n-data simple csv + dfzh dict csv,量级足够 ticket-004 的 50+ 术语;转我们的 YAML/CSV;记录 CC BY-NC 署名 |
| 4 | 规则引擎数据 | **改造参考** | 两项目 TOML 规则集语法与我们模板句规则同构,作格式参考,不直接引入 |
| 5 | 字体 | **复用** | NotoSansMonoCJKsc / MapleMonoNL-CN,均 SIL OFL |
| 6 | DFHack 基础设施 | **复用** | eventful/overlay/screen/dfhack-run 官方 API,已在本机确认在位并可用 |
| 7 | LLM 适配层 | 自研(接口按工程书 §12) | 无现成轮子;ollama /api/chat 调用已在 ticket-001 固化 |
| 8 | 存储(SQLite 四表) | 自研 | 现有项目均无持久化翻译记忆/反馈闭环,按工程书 schema 自建 |

> 结论符合工程书 §2.1 的"先查轮子":**没有可直接照搬的成品轮子** ——
> 中文汉化领域现有成果集中在"静态词典+规则"与"非官方挂钩渲染",
> 我们的增量价值 = 官方 API 之上 + LLM 动态文本 + 持久化闭环。

---

## 8. 对 SPEC-001 第一版范围的更新(已回写)

依据本审计,在 docs/specs/SPEC-001-mvp-translation-engine.md 增加「Reuse
Decisions(ticket-002)」小节,明确:
1. **捕获/渲染全部走 DFHack 官方 API**,任何内存偏移/外部挂钩不得进入代码库;
2. seed 词典从 dfi18n-data / dfzh 词典数据**改造导入**(标注 CC BY-NC 来源);
3. CJK 渲染方案(字体复用 + screen/Textures API)列为 ticket-008 设计前置;
4. 第一版范围**新增**「复用数据导入 + 许可署名」动作,其余范围不变。

---

## 附:审计证据索引

- DFI18n 源码: `dfi18n/src/hooks.rs`(addst/addcoloredst 挂钩)、
  `crates/executable/src/elf.rs`(ELF 偏移)、`Cargo.toml`(fontdue/sdl2-sys/cp437_string)
- dfzh 源码: `dfzh/dfzh.cpp`(插件注册)、`dfzh/sdl2_hooks.cpp`(Win32 Detours)、
  `dfzh/ttf_manager.cpp`(SDL2_ttf)、`data/fonts/LICENSE.txt`(OFL)
- 数据: dfi18n-data `simple/zh-Hans.csv`(901 行)、`rulesets/zh-Hans/`(143 个 TOML);
  dfzh `data/dfzh_dict_exact.csv`(1884 行)、`data/rulesets/zh-Hans/`(181 个 TOML)
- DFHack 文档(53.16-r1.1): Lua API Screen 段(`paintString/paintTile/fillRect/readTile`、
  Textures `loadTileset`)、overlay-dev-guide、eventful(含 `onReport`)、Remote(`dfhack-run`,
  `remote-server.json` port 5000 / allow_remote:false)
- 本机: `~/Games/DwarfFortress/data/init/init_default.txt`(PNG 位图字库、无 TRUETYPE)、
  `hack/plugins/{eventful,overlay}.plug.so`、ENVIRONMENT_AUDIT.md(dfhack-run 实测通过)
