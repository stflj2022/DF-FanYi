# ticket-015 — 长段落 addst 段落缓存（优化层）

> 父 spec：SPEC-012 · 状态：pending · 优先级：P2（优化，可延后）
> 依赖：013, 014（依附其 inline 路径）· 被依赖：016

## 目标
长段落（v53 按词逐次 addst 渲染）的翻译结果做段落级缓存，避免同一段文本每次重渲染都重复翻译。

## 背景
- dfint 渲染钩子按 addst(string) 调用，逐词返回中文（已删虚词防词沙拉）
- textviewer/announcement 区域已被 013/014 覆盖，本层是**性能优化**而非必要功能
- 段落缓存命中率 ≥ 30% 时可节省 30% LLM 调用

## 范围

### 1. 段落聚合窗口（DFHack Lua 层）
- fanyi.lua 启动时拉一次 inline_cache（key: `paragraph:{text_hash}`）→ 写入 fanyi_state.paragraph_cache
- 段落缓存命中：render_inline_payload() 直接返回缓存 → 不调引擎
- 未命中：调 `bridge.paragraph_translate(text)` → 引擎 LLM 翻译 → 写回缓存

### 2. 翻译引擎段落缓存（Python 层）
- `df_fanyi/bridge/inline_cache.py`：SQLite 表 `paragraph_cache`
  - key: `sha256(text_normalized)` （normalize: 小写 + 去标点 + collapse whitespace）
  - value: 译文 + ts + context
  - 索引: ts DESC（清理过期用）
- 启动时加载最近 1000 条到内存（LRU）
- 清理：> 7 天未访问 → 标记可清理

### 3. JSON-RPC 协议扩展
- 新方法：`bridge.paragraph_translate(text, context)` 
- 返回 `{translated: str, cache_hit: bool}`
- 引擎内部流程：缓存命中 → 直接返回；未命中 → LLM → 写入缓存 → 返回

### 4. 与 013/014 共用
- 013 (textviewer) + 014 (announcement) 翻译时优先查段落缓存
- textviewer 长段落（如教程页 1000+ 字）→ 命中段落缓存 → 0 LLM 调用
- announcement 短文本（<100 字）→ 段落缓存命中率较低（短文本直接走 inline_translate）

## 验收
- [ ] `df_fanyi/bridge/inline_cache.py` 实现段落缓存表 + LRU
- [ ] JSON-RPC `bridge.paragraph_translate` 可用
- [ ] 单测：缓存 get/set/LRU 淘汰测试通过
- [ ] 集成：同一段文本 2 次调用，第二次 `cache_hit=true`
- [ ] 段落实时聚合窗口（DFHack Lua 层）：连续 addst 在 50ms 内合并
- [ ] 提交 + 推送

## 风险
- 段落缓存体积膨胀：LRU 1000 条上限 + 7 天清理 → 内存占用 < 10MB
- 段落 hash 冲突概率：sha256 极低，可接受
- 段落聚合窗口时间参数：50ms 是经验值，需 E2E 调优

## 估计
- Python 引擎：~120 行（inline_cache.py + JSON-RPC 扩展）
- DFHack Lua：~40 行（聚合窗口 + cache 读）
- 单测：~60 行
- 提交：1 commit + push

## 备注
**不强依赖 013/014 完成**：013/014 已用了 inline_translate；015 在它们之上加段落缓存层。先合 013/014，再合 015（性能优化），最后 016 E2E。