# ticket-007: 优先级队列 + 后台翻译 worker

## 状态: done

**Blocked by:** ticket-004
**产出:** 异步翻译基础设施,长句不阻塞(Test 6)

**做什么:** 工程书 §25-30:
1. 内存优先级队列 P0 realtime / P1 interactive / P2 important / P3 background /
   P4 prefetch;job 含 id/priority/text_hash/attempt/deadline_ms。
2. 本地 worker 数=1(可配),云端 worker 预留接口(第三阶段实现)。
3. 调度: 复杂度初版启发式(长度/变量数/是否模板句,0.0-1.0 归一),
   ≤词典/规则阈值走同步快路径,否则入队异步并返回原文占位。
4. 超时分级(工程书 §29): >10s 的 realtime 任务取消转 background;
   每任务 attempts 记录,失败重试上限 3 后落 FAILED。
5. 事件回调: 完成时触发 on_translation_done(为 DFHack 桥做准备)。
6. 集成测试: 提交 50 句混合复杂度,快路径全同步返回,慢路径均异步完成且主流程不等待。

**验收标准:**
- [x] 500+ 字符长句提交后立即返回(原文占位),后台完成后回调携带中文(Test 6)
- [x] P0 恒先于 P3 被处理(假 LLM 控制完成顺序验证)
- [x] ollama 超时场景: 任务重试 3 次后 FAILED,队列不死锁;pytest 全绿

## 完成记录 (2026-09-08)

交付 commit 后(见 git log), 新增 33 个测试, 全仓 237 passed。
- **df_fanyi/core/queue/**: `priority.py`(Priority P0-P4 枚举/job 字段/线程安全 heap
  PriorityQueue, max_size 满员拒绝+close+pop-timeout 不死锁)、`heuristic.py`
  (complexity_score: 长度+变量/标记惩罚−模板补偿, 0-1 归一, 阈值 0.25=规则档上界)、
  `worker.py`(LocalTranslationWorker: pop→translate→终态回调; 重试≤max_attempts 后
  FAILED+on_failed; §29 realtime 超 deadline_ms → was_demoted+降为 BACKGROUND)、
  `scheduler.py`(TranslationScheduler: submit 先算复杂度, ≤阈值走 fast_translate
  同步快路径(绝不调 LLM), 否则入队立即返回原文占位; subscribe_done/failed 事件回调;
  max_queue 满 → 同步回退不阻塞; 有 store 时任务生命周期写 translation_jobs).
- **编排器**: 提取公开 `fast_translate()`(L1→L2→词典→规则, 零 LLM)与私有
  `_fast_result()`, `translate()` 行为保持不变。
- **Store**: `job_enqueue` 新增可选 `job_id`(内存 job 与 DB 行同 id, 供 wait()/回调对齐)。
- **Config/pipeline**: `queue.async_threshold`/`queue.max_queue`(=local_llm.max_queue),
  `build_scheduler(cfg)`。
- **验收实现映射**: ①Test 6(长句立即返回+回调中文) ②P0 先于 P3(假 LLM 按调用顺序断言)
  ③LLM 持续失败 → attempt=3 FAILED+on_failed, 随后新任务正常完成(不死锁)。
- **测试文件**: test_queue_priority(7) / test_queue_heuristic(7) / test_fast_translate(6) /
  test_queue_scheduler(9) / test_queue_integration(50 句混合,1) / test_queue_store(3)。
- 坑: submit 内惰性 `_ensure_workers()` 使 autostart=False 失效(worker 提前启动吃掉 P0 顺序
  断言)→ 惰性启动从 submit 移除, 由构造 autostart 控制; 集成测试 40 异步若同文本会被 L1
  缓存去重 → 构造 40 句互不相同; 短句假 LLM 译文需与源文信息长度成比例(否则被 §24 长度比
  0.3-3 校验正确拒绝)。
