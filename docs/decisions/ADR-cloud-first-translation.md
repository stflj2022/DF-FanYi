# ADR: 云端优先翻译 (Cloud-First Translation)

| 字段 | 值 |
|---|---|
| 状态 | **Accepted(2026-09-08)** |
| 决策者 | 用户(项目 owner) |
| 相关工单 | ticket-007、ticket-008、ticket-009、ticket-010、第三阶段 |
| 关联 | 工程书 §12(接口约束)/§17-18(云端 Provider) |

## 背景与问题

硬件为一般笔记本(AMD 4800U, 8 核/16 线程, 无独显),本地大模型(gemma-4b-trans)
CPU 推理仅 ~9-10 tok/s,单句 3.4-3.7s 热载、首载 12s。DF 翻译的真实体量
(公告/物品描述/历史/大量动态句)用本地模型译**既慢又吃力**,甚至可能拖慢游戏
事件流程。原设计"本地为先、云端留待第三阶段"需要按用户指示调整为:
**云端为默认主力,本地只作离线/降级兜底**。

## 决策

1. **默认翻译源 = 云端模型**(OpenAI-compatible, 通过现有 Pi Model Router 的
   `translation` profile 接入;key 只从环境变量读, 配置已支持)。
2. **本地 gemma-4b-trans 降为降级层**: 云端不可用(断网/key 失效/超时)
   或明确配置离线模式时,才回落到本地。
3. **云端大规模翻译优先**: 批量/长文本/历史大量翻译直接走云端;本地不承担
   主负载。
4. **不影响架构的"游戏主线程绝不等待 LLM"铁律**(工程书 §2.3): 无论云端还是
   本地,翻译均异步,游戏侧收到原文占位立即继续;回调再渲染。
5. **不破坏已建组件**: 现有 providers 抽象(local_llm + providers 列表 +
   router/pi_adapter)已为多 provider 就绪, 云端只须新增 enabled provider
   + 在 router 层加"云端优先、本地兜底"的选择逻辑, 不推翻现有内核。

## 对现有配置的影响(启动云端前)

- `config/default.yaml` 的 `providers` 列表新增云端条目(如 zhipu/glm-4-flash
   或 openrouter), `enabled: true`, key 经 `~/.config/df-fanyi/config.yaml`
   覆盖 + 环境变量注入(不经 git)。
- `local_llm.workers` 保持 1 或更低; 云端并发数由 pi_adapter 控制(第三阶段)。

## 对 008/009/010 的影响

- **008(DFHack 桥)**: socket 协议与 Lua 侧**不感知翻译源**,直接对接引擎队列,
  无需改动;引擎选择云端/本地属内核内部。
- **009(游戏端到端)**: 降级链实测需覆盖"云端不可用→本地兜底→原文"三段,
  E2E_CHECKLIST 增加云端故障注入一项。
- **010(MVP 验收)**: 复杂度阈值/延迟实测分"云端主力"与"本地兜底"两组记录;
  第三阶段工单草案更新为:实现云端 provider + pi_adapter 选择逻辑,
  云端优先正式生效。