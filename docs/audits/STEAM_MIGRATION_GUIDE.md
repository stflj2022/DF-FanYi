# Steam 版对接指南(经典版 → Steam 版迁移)

> 2026-09-09 起经典免费版退役(无图形包可用, HEWN 三连败, 详见 UI_I18N_GUIDE.md)。
> 本文记录 Steam 付费版(¥36 区价, 以商店页为准)的完整对接流程:
> **彩色图形画面 + DFI18n 中文界面 + DF-FanYi 公告字幕引擎**, 三者分工如下:

| 层 | 职责 | 提供方 |
|---|---|---|
| 画面 | 彩色像素贴图 | Steam 版自带(经典版没有的部分) |
| 界面汉化 | 菜单/按钮/UI 词条 | **DFI18n** 工坊模组(替代经典版 dfint) |
| 公告/动态文本 | 战报、公告滚动字幕、预翻缓存 | **DF-FanYi 引擎**(本仓库, 与游戏版本无关) |
| 桥载体 | Lua 捕获/渲染宿主 | **DFHack**(Steam 版独立构建) |

引擎侧代码零修改——只有**配置路径**和 **fanyi.lua 安装位置**两处对接点。

---

## 阶段 0: 购买与安装

1. Steam 搜索 `Dwarf Fortress`(Kitfox Games), 购买并安装。
2. 安装路径(本机): `~/.local/share/Steam/steamapps/common/Dwarf Fortress`
3. 先裸启一次确认能进主菜单, 再装 DFHack。

## 阶段 1: DFHack(二选一)

Steam 版与经典版的 DFHack 是**不同构建**(符号/偏移不同), 不可混用。

**方法 A: 工坊订阅(推荐, 自动跟随游戏更新)**
- Steam 工坊订阅 `DFHack`(合作官方条目), 游戏主菜单 Mods 里启用。
- 我们的 `fanyi.lua` 要装进工坊内容目录:
  `~/.local/share/Steam/steamapps/workshop/content/975370/<DFHack条目ID>/hack/scripts/`

**方法 B: 手动安装(布局与经典版一致, 引擎脚本零改动)**
- 从 dfhack.org 下载 **Steam 版** 53.x-r1.x tar 包, 解压进游戏根目录。
- 然后照旧:

```bash
cd ~/DF-FanYi
DF_ROOT=~/.local/share/Steam/steamapps/common/"Dwarf Fortress" \
    bash scripts/install-dfhack.sh
```

> 注意: Steam 的"验证文件完整性"可能清除手动放入游戏根的文件(含手动装的 DFHack)。
> 用方法 B 时建议关闭该游戏的自动更新; 方法 A 无此顾虑(工坊条目不受完整性校验影响)。

## 阶段 2: 中文界面(DFI18n)

1. Steam 工坊订阅: `DFI18n`(框架)+ 其中文语言包(工坊搜 "DFI18n"/"Chinese";
   此前调研线索 3613958631/3635900931 **待复核**, 以工坊搜索结果为准)。
2. **创建新世界前**在主菜单 → Mods 里勾选启用(与经典版不同, Steam 版有真正的
   Mods 菜单, 不再需要 hewn-enable 之类的绕路脚本)。
3. 进游戏确认主菜单中文; 字体由 DFI18n 自带 CJK 字库渲染。

## 阶段 3: 引擎对接(两处配置)

**① raws 路径**(`config/default.yaml`, 供预翻扫描; 经典版目录已删, 必须改):

```yaml
pretranslate:
  vanilla_dir: "/home/wu/.local/share/Steam/steamapps/common/Dwarf Fortress/data/vanilla"
```

**② 桥启动**(与经典版完全相同, TCP loopback 17486):

```bash
cd ~/DF-FanYi && python3 -m df_fanyi bridge --transport tcp --port 17486
```

游戏内 DFHack 控制台验证: `fanyi status` → 版本守卫行显示 Steam 版本号, 桥 running。

> 字幕层的 CJK 渲染用引擎自生成字库(scripts/generate_font_atlas.py 产物),
> 与游戏版本无关, 无需重新生成。

## 阶段 4: 预翻收尾(继承 4691 条 + 补缺口)

经典版跑批已入库 **4691/6964 条译文 + 3542 条锁定名称**(L2 TM, provider=cloud-pretranslate),
按"英文原文哈希"命中, Steam 版同版本 raws 文本一致 → **自动继承, 无需重翻**。

```bash
# 0. 探活 model-router(教训: 09-08 晚 router 掉线, 跑批整夜空转重试)
curl -sS -m 3 http://127.0.0.1:8010/v1/models || echo "router 未起, 先启它!"

# 1. Steam raws 重扫(增量, 拾取版本差异带来的新文本)
python3 -m df_fanyi pretranslate scan

# 2. 续跑补缺口(断点续传; ≈1h 后台)
nohup python3 -m df_fanyi pretranslate run >> logs/pretranslate.log 2>&1 &

# 3. 再次入库(幂等, 新条目追加)
python3 -m df_fanyi pretranslate install
```

## 阶段 5: 验收

按 `docs/audits/E2E_CHECKLIST.md` B1-B6 人工清单走一遍:
装载/版本守卫 → 公告捕获 → 云端翻译 → 字幕渲染 → 降级链 → 延迟实测。

---

## 常见坑(前车之鉴)

| 坑 | 对策 |
|---|---|
| Steam 自动更新先于 DFHack 适配 | 游戏属性里暂缓更新, 等 DFHack 发布对应版 |
| "验证文件完整性"清掉手动文件 | DFHack 用工坊版, 或关自动更新 |
| 模组在世界创建时激活 | 已有世界不能追加模组, 先配 Mods 再 Create World |
| model-router 掉线 → 跑批整夜空转 | 跑批前 curl 探活; 跑批时 tail 日志确认有进度 |
| 云端批量 max_tokens 被思维链吃空 | 已修(4096), 若再现空 content 属瞬时故障, 退避重试自愈 |

## 回滚/卸载

- 界面汉化: 游戏内 Mods 取消勾选 DFI18n。
- 引擎: 删 `hack/scripts/fanyi.lua`(工坊 DFHack 则删工坊内容目录里的同名文件)+ 停 bridge 进程。
- 预翻数据(engine.db L2 + 术语层)独立于游戏, 保留不影响任何东西。
