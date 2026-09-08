# IMPLEMENTATION_PLAN — 第三阶段工单草案（ticket-010 产出）

> **性质**: 草案目录。按无人值守驱动约定, 本阶段**不创建新的 `docs/tickets/ticket-*.md` 文件**
> （驱动以工单文件数判定完工）。下列工单在 MVP 验收（`MVP_ACCEPTANCE.md`）通过后,
> 由人工/新一轮规划复制为正式工单再执行。
>
> 依据: 工程书 §12-18(Model Router/Provider)、§28(云端并发)、§31-33(熔断/健康/成本路由)、
> §51 第三阶段、§52 第四阶段 + ticket-010 实测数据(60 真实句基准) + ADR「云端优先翻译」。

## 实测背景（来自 docs/audits/MVP_ACCEPTANCE.md）

- 本地 gemma-4b-trans CPU 实测 p50 58.5s / p95 77.2s（含 ~500 token thinking 阶段）,
  **不可用于交互路径** —— 云端优先（ADR-cloud-first-translation.md）。
- 真实流动句快路径命中 0%（词典/规则仅覆盖术语与模板）→ 主力流量必然走 LLM 层。
- 复杂度分数对真实 DF 文本的 LLM 难度区分力弱（0.01-0.12 一刀切）→ 云路由不能只靠 §34 分数。

## 草案 P3-1: OpenAI-Compatible Provider（工程书 §17/§18 + ADR）

**做什么:**
1. `df_fanyi/providers/openai_client.py`: `OpenAICompatibleProvider.chat(model, messages,
   temperature, max_tokens, timeout)`(§17 接口原样), 复用 `http_transport` 超时/错误语义。
2. 配置激活 `config/providers.yaml` 已有骨架: `api_key_env` 从环境变量读, 代码零密钥(§17);
   默认 `enabled: false`, 密钥未设时 provider 标记不可用(已有 ProviderConfig.available)。
3. 至少接入 Generic OpenAI-compatible 一家跑通(OpenRouter/智谱可后补, §18 允许增量)。

**验收标准:**
- [ ] 假 HTTP provider 单测: 成功/超时/5xx/限流(429)各一, 语义对齐 OllamaChatClient 现有契约
- [ ] 环境变量缺 key → provider 不可用, 引擎仍启动(离线降级不回归)
- [ ] orchestrator 在 provider 列表含云端时: 云优先、本地兜底、原文保底(§30 顺序)

## 草案 P3-2: Pi Model Router Adapter（工程书 §12/§13）

**做什么:**
1. `df_fanyi/router/pi_adapter.py`: 实现 `router.select(task, profile, complexity,
   latency_budget_ms, cost_budget)` —— 经 Pi Model Router 的 translation profile 选择
   provider/model/fallback 链(§12 返回形状原样)。
2. Profile 集(§13): `translation/fast|balanced|quality|long-context|offline`,
   映射规则按 text_type + 长度 + 验证反馈(不单看 §34 分数, 见实测结论)。
3. Router 不可用/未配置 → 直接落 `providers` 静态列表(现行为), 保证可独立运行。

**验收标准:**
- [ ] adapter 单测: profile 选择矩阵 + router 故障时静态回退
- [ ] 现有 fallback 顺序(云→本地→原文)在新旧两条路径下行为一致
- [ ] docs/decisions 补一条 ADR: router.select 签名/失败语义

## 草案 P3-3: Circuit Breaker + Provider Health（工程书 §31/§32）

**做什么:**
1. 每 provider 记录 success rate / latency p50 p95 / timeout rate / error rate(§32);
   连续 3 次失败 → COOLDOWN 60s 内不请求该 provider(§31)。
2. 健康数据持久化到 SQLite(复用 §8 库, 新表或 translation_jobs 聚合), 供 Router 重排序。
3. 熔断期间: 云 provider 跳过 → 本地 gemma → 原文; 恢复后半开探测。

**验收标准:**
- [ ] 假 provider 注入连续失败 → 断路打开, 流量切兜底, 冷却后半开恢复
- [ ] Test 7/8 既有验收回归全绿(断网/超时语义不变)

## 草案 P3-4: 成本路由与预算（工程书 §33）

**做什么:**
1. 每次云端调用记录 cost(token 数 × 单价表, 表放配置); 日预算 `daily_budget_usd` 默认 $0.50。
2. 阈值行为(§33): 0-50% 正常 / 50-80% 转便宜模型 / 80-100% 只处理复杂文本 /
   100% Cloud OFF(本地 gemma 接管, 实测慢但可用; 游戏主线程仍不阻塞)。
3. 预算状态在引擎重启后从 SQLite 恢复当日累计。

**验收标准:**
- [ ] 预算模拟测试: 四个阈值段行为各一例
- [ ] 预算耗尽 → 云端停用 → 本地兜底链回归(Test 7 变体)
- [ ] cost 记录不落任何密钥(§17/§56)

## 草案 P3-5(可选, 与 P3-2 解耦): 词典/规则层扩充

**做什么:** 实测快路径 0% 命中 → 从已积累的 translation_memory(§8.2)自动反哺词典
(高频已验证译文 → terminology/词典条目), 提高离线命中与云端成本下降。

**验收标准:**
- [ ] 高频句自动入层规则 + 命中率复测脚本(bench 复跑对比)

## 执行顺序建议

P3-1(独立可测) → P3-2(router 接线) → P3-3(熔断/健康) → P3-4(预算) → P3-5(反哺)。
P3-3/P3-4 依赖 P3-1 的 provider 抽象但不依赖 router; 可在 P3-2 之前并行。
