# 规范:DF-FanYi 矮人要塞 AI 动态汉化引擎(MVP)

来源: docs/AI_GAME_TRANSLATION_ENGINE_SPEC.md v2.0 · 状态: ready-for-agent · 日期: 2026-09-08

## Problem Statement

矮人要塞(Dwarf Fortress 53.16 免费经典版)没有中文版,且其文本高度动态
(公告、人物关系、历史事件均为程序生成),传统静态汉化包无法覆盖。
玩家(中文母语)在游戏中阅读英文动态文本时理解成本极高,无法沉浸。

## Solution

建立一个运行于 DFHack 之上的 AI 本地化引擎:实时捕获游戏文本,经
两级缓存 → 精确术语词典 → 规则引擎 → 本地 LLM(ollama gemma-4b-trans)
→ (后期)Pi Model Router 云端的编排链路翻译为简体中文,验证后异步渲染回
游戏 UI。游戏主线程绝不等待 LLM;翻译系统任何故障都只降级为"显示原文"。

## User Stories

1. 作为玩家,我在游戏中看到任何英文公告时,希望屏幕上以中文叠加显示,以便即时理解。
2. 作为玩家,我希望高频短句(如 "Urist cancels Make Wooden Barrel.")在词典命中时瞬间显示中文,无需等待模型。
3. 作为玩家,我希望翻译异步进行,游戏帧率与操作完全不因翻译而卡顿。
4. 作为玩家,当翻译引擎崩溃或断网时,我希望游戏照常运行并显示英文原文,以便不受影响。
5. 作为玩家,我希望游戏术语(木桶、矮人、木匠…)全游戏统一译名,以便建立稳定心智模型。
6. 作为玩家,我希望文本中的变量({COUNT} 等)、颜色标记、数字、人名在译文中完整保留,以便信息不失真。
7. 作为玩家,我希望长文本(传记、雕刻描述)在后台翻译完成后刷新显示,以便愿意时可读。
8. 作为玩家,我希望用快捷键查看某条译文的原文/模型/置信度并纠错,以便反馈改进译文。
9. 作为开发者,我希望翻译内核可在无 DF 环境下用 CLI 独立测试,以便快速迭代。
10. 作为开发者,我希望 API Key 只存在于本地 secrets 文件,以便仓库可公开。
11. 作为开发者,我希望所有模型调用经统一 Router 接口,以便未来切换 Provider 不改内核。
12. 作为运维者,我希望系统崩溃后重启能从 SQLite 恢复未完成任务,以便不重复翻译。

## Implementation Decisions

- 三层解耦:游戏层(DFHack Lua 插件:捕获/渲染 + unix socket JSON-RPC)与
  翻译层(Python 引擎:编排/缓存/词典/规则/LLM/验证/SQLite)完全分离;
  模型基础设施层复用用户已有 Pi Model Router(仅增加 translation profile,
  禁止重写 Router;Router 指南 404,以工程书 §12 接口签名为约束)。
- 游戏侧只用 DFHack 官方 API(Lua/eventful/overlay),禁止 OCR、禁止内存偏移。
- 存储第一版只用 SQLite(terminology / translation_memory / translation_jobs /
  feedback 四表);缓存两级:内存 LRU(L1)+ SQLite(L2)。
- 复杂度评分路由:词典(≈0.1)→规则(0.25)→本地 LLM(0.45-0.65)→云端(≥0.8),
  阈值必须实测调优,不写死理论值。
- 本地模型: ollama `gemma-4b-trans`(E4B);并发 worker=1;云端仅复杂文本/失败回退。
- Prompt 版本化于 prompts/;游戏文本视为不可信输入(注入防护);Markup/Variable
  用占位符保护,Validator 校验占位/数字/行数守恒,不合格即拒绝回退原文。
- 云端 Provider 统一 OpenAI-compatible 接口,Key 走 env,代码零硬编码。
- 版本守卫:DF 与 DFHack 版本不匹配进 Safe Mode(不注入、可诊断)。

## Testing Decisions

- 只测外部行为:内核用 pytest + 假 LLM(录制回放),不 mock 内部实现。
- 引擎级:工程书 §54 Test 1-8 为验收集(变量/markup 守恒、长句异步、断网、超时回退)。
- 集成级:DFHack 桥以 socket 协议回放测试;游戏内验证手工清单(游戏 UI 无法自动化)。
- 每工单合入前 `pytest tests/ -q` 全绿;测试命令由无人值守 driver 自动执行。

## Out of Scope(第一版)

云路由接入(第三阶段)、反馈学习/自动微调、向量库、PostgreSQL、Kubernetes、
OCR、其他游戏(RimWorld 等)适配、自动训练模型。

## Further Notes

- ollama `gemma-4b-trans` 经 /api/generate 实测返回空响应 —— 需在工单 001 用
  /api/chat 复测并固化正确调用方式;基线延迟: 首载 18.4s, 生成 ~9 tok/s (CPU)。
- GitHub API token 无效:工单用本地 docs/tickets/ 文件,推送走 SSH(已验证可用)。
- 远端仓库现名 EPUB-AI-Fanyi,用户将重命名为 DF-FanYi;旧地址自动重定向。
