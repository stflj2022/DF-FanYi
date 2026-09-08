# ticket-006: SQLite 持久层(词典/TM/任务/反馈)

## 状态: done

**Blocked by:** ticket-004
**产出:** L2 持久缓存与四张表 + 崩溃恢复

**做什么:** 工程书 §8/§42:
1. 建库脚本按工程书 §8.1-8.4 四表(terminology / translation_memory /
   translation_jobs / feedback),WAL 模式。
2. DAO 层 + 内核集成: L2 查询(cache_hit 标记)、译文写回(含 model/provider/
   confidence/usage_count)、高频提升(usage_count>10 常驻,工程书 §38)。
3. 启动崩溃恢复: 存在 RUNNING 状态 job → 标 INTERRUPTED → 重排队。
4. 术语管理 CLI: `df-fanyi term add/list`(locked 默认 false)。
5. 测试用临时库,不落用户目录。

**验收标准:**
- [x] 同句第二次翻译 cache_hit=true 且不调 LLM(假 LLM 计数为证)
- [x] 杀进程模拟崩溃后重启,RUNNING job 被 INTERRUPted 并重试
- [x] pytest 全绿
