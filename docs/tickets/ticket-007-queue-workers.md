# ticket-007: 优先级队列 + 后台翻译 worker

## 状态: pending

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
- [ ] 500+ 字符长句提交后立即返回(原文占位),后台完成后回调携带中文(Test 6)
- [ ] P0 恒先于 P3 被处理(假 LLM 控制完成顺序验证)
- [ ] ollama 超时场景: 任务重试 3 次后 FAILED,队列不死锁;pytest 全绿
