# MVP 验收报告（ticket-010 · 工程书 §54）

- **日期**: 2026-09-08
- **结论**: **第一阶段 MVP 验收通过** —— §54 Test 1-8 全部自动化测试通过；60 条真实游戏文本基准实测完成；实测中发现的 1 个生产级缺陷（thinking 预算耗尽）已按 TDD 修复并纳入回归。
- **测试证据**: `pytest tests/ -q` 全绿（ticket-010 前基线 292 passed / 1 skipped + 本次新增 15 项验收 + 3 项 provider 回归）。

## 一、§54 Test 1-8 逐条结论（自动化, 真实 TCP socket 桥）

| # | 工程书要求 | 测试(`tests/test_acceptance_spec54.py`) | 结论 |
|---|---|---|---|
| 1 | `Dwarf` → `矮人` | `Test1Dwarf`（含大小写不敏感） | ✅ 词典层同步返回 |
| 2 | `wooden barrel` → `木桶` | `Test2WoodenBarrel` | ✅ 词典层同步返回 |
| 3 | 动态句 `Urist cancels Make Wooden Barrel.` | `Test3DynamicSentence` | ✅ 规则层模板翻译 |
| 4 | `{COUNT} dwarves` 变量完整保留 | `Test4VariablePreserved`（含 LLM 丢变量→验证拒绝→原文兜底） | ✅ §24 多集守恒 |
| 5 | `<color=red>Urist</color>` markup 保留 | `Test5MarkupPreserved`（含 LLM 丢标记→拒绝→兜底） | ✅ §24 多集守恒 |
| 6 | >500 字符长句异步翻译 | `Test6LongSentenceAsync`（queued 占位→fetch_done 取回, 主线程不阻塞） | ✅ §29 |
| 7 | 断网(云不可用)游戏仍正常运行 | `Test7NetworkDown`（provider 全灭 20 句压力 + 完全离线降级） | ✅ 原文兜底, 桥不倒 |
| 8 | API 超时自动 fallback | `Test8ApiTimeout`（挂起 provider → 超时回退原文; 桥级降级链; 慢 provider 不卡死队列） | ✅ |

## 二、真实文本基准（§54「收集 ≥50 条真实游戏句子」）

- **采集**: `scripts/collect_acceptance_samples.py`（确定性, 同机可复现）→ `data/samples/acceptance_samples.jsonl`
- **来源**: 本机 Dwarf Fortress 53.16 真实文本 —— 公告 `gamelog.txt`（开局公告逐句）、描述 `data/vanilla/*/objects/*.txt` 的 `[DESCRIPTION:]`、历史 `vanilla_text/objects/text_*.txt`
- **规模**: **60 句** = 公告 10 + 描述 25 + 历史 25（`data/` 已 gitignore, 不入库）
- **执行**: `scripts/acceptance_bench.py`（真 ollama gemma-4b-trans）→ `data/samples/bench_results.json` / `bench_summary.md`

| 指标 | 实测值 |
|---|---|
| 快路径(L1→L2→词典→规则)命中 | 0/60（真实流动句均为全文新句; 快路径延迟 p50 0.03ms, 命中时 <0.1ms） |
| LLM 实测子集（限 12 句）验证通过 | **12/12 = 100%**（§24 验证层零误杀零漏放） |
| LLM 延迟（CPU ~9.5 tok/s, 含 thinking） | p50 **58.5s** / p95 **77.2s** / max 79.3s |
| 复杂度分布（真实句） | 0.01–0.12（短句）; ~0.5（250+ 字符描述） |

**质量抽查**（详见 bench_results.json 全部译文）:
- "A Dwarven Outpost: You have arrived." → 「一个矮人前哨站：你已经到达了。」
- "Strike the earth!" → 「向大地进军吧！」
- 专有名词（Kasbenurvad / Oslanbakust / Mountainhomes）按 §24 要求保留。

## 三、实测发现与处置

**F1（已修复·生产级缺陷）**: gemma-4b-trans 每次翻译前有 ~500+ token 的 **thinking 阶段**（ollama 归入 `message.thinking`）。旧默认 `num_predict=256` 被思考阶段耗尽 → 正文为空 + `done_reason=length` → 30s 超时空转 → 全部回退原文（修复前实测 9/12 句失败）。
- 修复: `OllamaChatClient.build_payload` 默认 `num_predict` 256→**1024**（预算覆盖 thinking + 译文）; 回归测试 `test_thinking_with_empty_content_raises`（thinking 吞预算必须显式报错回退, 不能把 thinking 当译文）、`test_content_preferred_over_thinking`。
- 复测: 12/12 全部通过, 验证层零失败。

## 四、§34 阈值实测调整（config/default.yaml）

- **`queue.async_threshold` 保持 0.25**, 依据:
  - 词典/规则可解句（Test 1-3）实测复杂度 ≤0.12, 全部 < 0.25 → 同步快路径;
  - 真实自然句复杂度 0.01–0.12（短）至 ~0.5（长描述）, 但快路径均未命中 → 正确入队异步, 无一误分档;
  - 结论: 复杂度分数对真实 DF 文本的 LLM 难度**区分力弱**（§34 的 0.45/0.65/0.8 档只由长度分量触发）。已在 config 注释与第三阶段计划中声明: 云路由不可只依赖此分数, 需叠加 text_type / 长度 / 验证反馈。
- **`local_llm.timeout_s` 保持 120**: 实测 p95 77.2s < 120s（含 thinking）, 覆盖充足; 若换 thinking 关闭的模型可显著降低。

## 五、MVP 验收判定

| 验收维度 | 判定 |
|---|---|
| §54 Test 1-8 自动化 | ✅ 全部通过（真实 TCP 桥端到端, 非 mock 管道） |
| ≥50 真实句子 + 命中层/延迟/验证率记录 | ✅ 60 句, 全指标落盘 |
| §34 阈值有测量依据 | ✅ 0.25 实测保持, 数据与结论入库 |
| 主线程不等待铁律（§8/§29） | ✅ Test 6/7/8 复验 |
| 验证失败绝不污染上屏（§24） | ✅ Test 4/5 + LLM 12/12 |

**遗留（不阻塞 MVP）**: 真实句快路径命中 0% → 词典/规则层收益目前限于 UI 术语与模板句; 流动句收益依赖第三阶段云端低延迟（见 `IMPLEMENTATION_PLAN.md`）。本地 gemma CPU 58s/句不可交互, 再次印证 ADR「云端优先」。

**游戏内人工项**: 见 `docs/audits/E2E_CHECKLIST.md` B1-B6（需真机 Dwarf Fortress 会话, 与本自动化验收互补）。
