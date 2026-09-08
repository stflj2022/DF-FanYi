# DF-FanYi — 矮人要塞 AI 动态汉化引擎

**项目**: Dwarf Fortress 53.16 免费经典版 → 简体中文 实时 AI 翻译引擎
**规格书**: [docs/AI_GAME_TRANSLATION_ENGINE_SPEC.md](docs/AI_GAME_TRANSLATION_ENGINE_SPEC.md) v2.0
**规范**: [docs/specs/](docs/specs/) · **工单**: [docs/tickets/](docs/tickets/) · **审计**: [docs/audits/](docs/audits/)

## 这是什么

不是传统静态汉化包。本项目是一个能理解 DF 动态文本、游戏上下文、人物关系,并利用
本地模型(ollama gemma-4b-trans)+ 云端大模型进行实时翻译的 AI 游戏本地化引擎。

```
Dwarf Fortress → DFHack → 文本捕获 → 解析/分段 → 上下文管理
  → 翻译编排器(缓存→字典→规则→本地LLM→云端Router) → 验证器 → 中文渲染 → DF
```

## 三层架构(工程书 §58)

| 层 | 职责 |
|---|---|
| 游戏层 | DFHack:游戏现在说了什么 |
| 翻译层 | Translation Engine:这句话什么意思、怎么翻 |
| 模型基础设施层 | Pi Model Router:现在该用哪个模型/Provider |

## 铁律(工程书 §2 / §56)

- ❌ API Key 永不进 Git(只走 `~/.config/df-fanyi/secrets.env`)
- ❌ 游戏主线程绝不等待 LLM(全部异步)
- ❌ 翻译系统崩溃 ≠ 游戏崩溃(引擎挂了显示原文)
- ❌ 不重复造轮子:先审计 DFI18n / dwarf-fortress-chinese / DFHack / Pi Router
- ❌ 第一版:不训练模型、不上 PostgreSQL、不上向量库、不 OCR、不阻塞

## 本机环境

- DF + DFHack: `~/Games/DwarfFortress`(版本审计见 docs/audits/)
- 本地模型: ollama `gemma-4b-trans`(5.3G, CPU ~9 tok/s)
- 云端: OpenRouter / 智谱 / 国家超算(经 Pi Model Router,第三阶段接入)
- 平台: Omarchy (Arch) / Ryzen 7 4800U / 16G RAM / AMD Vega

## 无人值守开发

```bash
bash scripts/install-unattended.sh   # 部署 driver + watchdog
tail -f .unattended/driver.log       # 观察进度
bash scripts/completion-check.sh     # 完工判据
```

工单在 `docs/tickets/`,状态行 `## 状态: pending|doing|done`。
