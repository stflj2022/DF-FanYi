# ticket-010: MVP 验收(工程书 Test 1-8)与阈值初调

## 状态: pending

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
- [ ] pytest 全绿且 Test 1-8 每项在报告中有一行结论(通过/数据)
- [ ] 真实样本 ≥50 句的命中/延迟统计表,阈值有实测依据
- [ ] MVP_ACCEPTANCE.md + 第三阶段工单草案完成
