# ENVIRONMENT_AUDIT — 本机 DF 环境与本地模型审计 (ticket-001)

日期: 2026-09-08 · 审计人: 无人值守 agent · 全部数据为本机实测, 非理论值。
本文件是后续所有工单(ticket-004+)的事实依据; 环境变更时须重跑本审计。

## 1. Dwarf Fortress 版本

| 项 | 值 | 证据 |
|---|---|---|
| DF 版本 | **53.16** | `file changes.txt` "Auxiliary file changes for 53.16"; `release notes.txt` 同版本 |
| 安装路径 | `~/Games/DwarfFortress` | 目录实测 |
| 构建类型 | **classic(非 Steam / Bay12 官方分发)** | 见 §2 |
| 主程序 | `dwarfort` (ELF 64-bit x86-64, 动态链接) | `file dwarfort` |

## 2. DFHack 版本与 classic 支持结论

| 项 | 值 | 证据 |
|---|---|---|
| DFHack 版本 | **53.16-r1.1** | `hack/docs/docs/NEWS.txt` changelog 首条 "DFHack 53.16-r1.1" |
| 插件数 | 75 个 `hack/plugins/*.plug.so` | `ls hack/plugins/ \| grep -c` |
| 关键插件 | **eventful.plug.so / overlay.plug.so 均存在** | 捕获(工单-008)与渲染依赖 |
| RPC | `dfhack-run` → `127.0.0.1:5000`, **游戏运行时默认即开, 无需手动 enable** | `dfhack-config/remote-server.json` (`allow_remote:false` 仅本地); 实测 `ss -tlnp` 见 dwarfort 监听 5000, `./dfhack-run plug` 成功返回插件表 |

**classic 支持结论: 支持, 且本机安装即 classic 构建并已验证可用。** 证据:

1. 目录含 classic 专属文件: `g_src/`、`libg_src_lib.so`、`command_line.txt`(经典版命令行世界生成文档), 无任何 `steam*` 文件;
2. `./dfhack` 启动脚本为 classic 专用加载器(LD_PRELOAD `hack/libdfhack.so`, 内部检测 `SteamAppId=2346660` 走 Steam 分支 —— 本机非该分支);
3. dfhooks 机制在位: `dfhooks_dfhack.ini` → `hack/libdfhooks_dfhack.so`;
4. **历史运行日志实证**: `stderr.log` 含 DFHack init 脚本执行记录
   (`Running script: ".../dfhack-config/init/onLoad.init"`、`default.onUnload.init` 等),
   即本 classic 构建此前已成功加载 DFHack 运行过。DFHack 官方亦声明支持 Itch/Classic 安装
   (见 `hack/docs/docs/Installing.txt`)。

版本守卫(规格 Implementation Decisions): DF 53.16 ↔ DFHack 53.16-r1.1 **匹配, 无需 Safe Mode**。

## 3. 如何启动 DF 并确认 DFHack 活着(可复制命令)

```bash
# 启动(加载 DFHack; classic 唯一正确入口):
cd ~/Games/DwarfFortress && ./dfhack
# 注意: ./run_df 是不带 DFHack 的原版启动器, 翻译引擎场景禁用。

# 确认 DFHack 活着(游戏运行后, 另开终端):
cd ~/Games/DwarfFortress && ./dfhack-run ls plugins        # RPC 走 localhost:5000
cd ~/Games/DwarfFortress && ./dfhack-run plug              # 已加载插件详单

# 不启动图形也能预检插件在位(离线检查):
ls ~/Games/DwarfFortress/hack/plugins/eventful.plug.so \
   ~/Games/DwarfFortress/hack/plugins/overlay.plug.so

# 游戏未运行时 dfhack-run 的表现(实测):
#   "Could not connect to localhost:5000" —— 以此判断游戏/RPC 是否存活
```

**RPC 实测结论(2026-09-08)**: 游戏运行期间 `ss -tlnp` 实测
dwarfort(pid) 监听 `127.0.0.1:5000`, `./dfhack-run plug` 成功列出插件表
(含 RemoteFortressReader loaded/enabled); 游戏正常退出后(onUnload 脚本执行)
即报 Could not connect。→ RPC 随 DFHack core 自启, **无需在 dfhack.init 额外开启**;
`allow_remote:false` 只允许本机连接, 对本机引擎恰为正确配置。

headless 注意: DF 本体需要图形会话(Hyprland 下正常启动); 无头环境只能做 §上的
离线预检 + `dfhack-run` 连接探测, 无法完成游戏内验证(与工单-009 手工清单一致)。

## 3.5 DF 数据目录与存档 / worldgen 自动化

- `data/` 结构: `art/ credits.txt/ init/ installed_mods/ sound/ vanilla/ save/`
- `data/save/` 当前为空(无存档) —— 引擎测试不涉及现有存档, 无污染风险
- **worldgen 可命令行自动化**(工单-009 端到端测试前置, 官方 `command line.txt`):
  ```bash
  cd ~/Games/DwarfFortress && ./dfhack -gen 1 3498 "MEDIUM ISLAND"
  # 静默无 intro 生成世界→导出 region 文件→自动退出; 同号世界已存在则 abort
  ```

## 4. 本地模型 ollama 实测 (gemma-4b-trans)

| 项 | 值 |
|---|---|
| ollama 版本 | **0.33.2** (`curl 127.0.0.1:11434/api/version`) |
| 模型 | `gemma-4b-trans:latest` — family gemma4, **7.5B 参数, Q4_K_M**, 5.3G, ctx 131072 |
| 服务 | systemd `ollama.service` active, `127.0.0.1:11434` |

### 4.1 固化调用方式(唯一正确路径)

**`POST /api/chat`, messages 格式, 非流式。** `/api/generate` 不采用(规格记录其曾返回空响应;
且无 messages 结构, 无法挂系统提示)。实测无系统提示时输出会混入英文注释
(如 "*(Note: ...)*"), 固化的中文系统提示已消除该问题。

```bash
curl -s http://127.0.0.1:11434/api/chat -d '{
  "model": "gemma-4b-trans",
  "messages": [
    {"role": "system", "content": "你是矮人要塞(Dwarf Fortress)游戏文本翻译引擎。把用户给出的英文游戏文本翻译成简体中文。规则:1)只输出译文,不要任何解释、注释或原文;2)保留专有名词(人名如Urist保留原文);3)保留数字、标点结构与占位符;4)使用简洁的游戏公告语气。"},
    {"role": "user", "content": "Urist cancels Make Wooden Barrel: needs barrel."}
  ],
  "stream": false,
  "options": {"temperature": 0.3, "num_predict": 256}
}'
```

- 代码固化: `providers/ollama_client.py` (`OllamaChatClient`, 系统提示常量
  `TRANSLATION_SYSTEM_PROMPT`); 回放测试: `tests/test_ollama_client.py`
  (fixtures 为真实录制响应, 见 `tests/fixtures/ollama/`)。

### 4.2 实测延迟(Ryzen 7 4800U, 纯 CPU)

| 场景 | 端到端 | 模型加载 | prompt tokens | tok/s | 译文 |
|---|---|---|---|---|---|
| 首载(冷载)首句 | **12.06s** | 8.05s | 106 | — | Urist 取消制作木桶:需要一个木桶。 |
| 热载短句 | **3.38s** | ~0 | 106 | **9.0** | 同上 |
| 热载短句2 | **3.72s** | ~0 | ~100 | **10.0** | 流浪狗(驯化)生下了幼崽了!欢庆吧! |

- 满足验收标准"单句 <5s": 热载 3.4-3.7s ✅; **冷载 12s 不满足**, 引擎必须常驻
  `keep_alive`(默认 5 分钟, 需在工单-007 worker 保活), 首句冷载走"先显原文后刷新"。
- tok/s ≈ 9-10 (CPU), 与规格基线 ~9 tok/s 一致; prompt 评估 ~1.8s/106 tok。
- 对照: 无系统提示的 `/api/generate` 冷盘首调实测 82.7s(含磁盘首次加载), 不可作为稳态依据。

### 4.3 坑记录

1. `/api/generate` 空响应: 规格记录在案; 复测时该端点能出译文但混英文注释 ——
   无论根因为何, 统一走 `/api/chat`, 不再使用 generate。
2. 冷载时长受页缓存影响巨大(8s vs 82.7s), 延迟预算必须按冷载 12s+ 预留。
3. `keep_alive` 默认 5 分钟后自动卸载 → 引擎需周期保活或 `keep_alive: -1`(工单-007 决策)。
4. ollama 服务依赖: `systemctl is-active ollama` 应纳入引擎启动自检(工单-004)。
5. 无人值守启动 DF: 人工实测(ENVIRONMENT_AUDIT-FACTS.md) nohup/无 stdin 启动时
   DFHack 控制台立即 "Console is shutting down properly"(不影响游戏本体, 但失去
   交互命令入口)。RPC 监听 socket 由 core 线程持有, 理论上不受终端关闭影响,
   待工单-008 实装时以 `dfhack-run plug` 验证。
6. 主会话人工事实请看 `docs/audits/ENVIRONMENT_AUDIT-FACTS.md`(渲染字库 8x12、
   网格 240x90 等); 中文渲染方案是 classic 位图字库, 为工单-002/008 核心调研项。

## 5. 测试工具链

- `python3` = /usr/bin/python3 (3.14, Arch); pytest 经
  `pip3 install --user --break-system-packages pytest` 安装(**9.1.1**)。
- 驱动命令 `python3 -m pytest tests/ -q` 实测可用(本工单 14 项测试)。
