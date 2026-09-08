# ticket-010: MVP 验收(工程书 Test 1-8)与阈值初调

## 状态: done

**Blocked by:** ticket-009
**产出:** 验收报告 + 复杂度阈值实测初值 + 第三阶段计划

**做什么:** 工程书 §54 全量验收:
1. Test 1-6 内核自动化测试补齐并全绿(矮人/木桶/动态句/变量/markup/长句异步)。
2. Test 7 断网: 全链路无网压测(云端未接入,模拟 provider 关闭)。Test 8 API 超时:
   用慢速假 provider 验证 fallback 链(本地 E4B → 规则 → 原文)。
3. 收集真实游戏文本样本(≥50 句,含公告/描述/历史),记录: 命中层、延迟、验证通过率。
4. 据实测调复杂度阈值(工程书 §34: 阈值不写死),写回 config/default.yaml 并注释依据。
5. 产出 docs/audits/MVP_ACCEPTANCE.md(数据表+结论),更新 IMPLEMENTATION_PLAN:
   第三阶段 Pi Router translation profile 接入(接口按工程书 §12)、云 Provider
   OpenAI-compatible 适配(§17-18)、预算/熔断(§31-33)的工单草案。

**验收标准:**
- [x] pytest 全绿且 Test 1-8 每项在报告中有一行结论(通过/数据) → tests/test_acceptance_spec54.py 15 项全绿, 见 MVP_ACCEPTANCE.md §一
- [x] 真实样本 ≥50 句的命中/延迟统计表, 阈值有实测依据 → 60 句(公告10/描述25/历史25), LLM 12/12 验证通过, p50 58.5s; async_threshold 0.25 实测保持, 依据写入 config/default.yaml 注释
- [x] MVP_ACCEPTANCE.md + 第三阶段工单草案完成 → docs/audits/MVP_ACCEPTANCE.md + docs/audits/IMPLEMENTATION_PLAN.md(P3-1~P3-5, 未建新工单文件, 遵守驱动完工判定)

**验收实测发现(2026-09-08):**
- F1(已修): gemma-4b-trans 含 ~500 token thinking 阶段, 旧 num_predict=256 被思考耗尽致正文空/超时空转(修复前 9/12 句失败)→ OllamaChatClient 默认 256→1024 + thinking 回归测试; 修复后 12/12 全部通过。
- 真实流动句快路径命中 0%(词典/规则仅覆盖术语与模板) → 主力流量走 LLM, 印证 ADR 云端优先。
- 复杂度分数对真实文本 LLM 难度区分力弱 → 第三阶段云路由不单靠 §34 分数(已写入 IMPLEMENTATION_PLAN)。
