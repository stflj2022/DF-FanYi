# ticket-004: 翻译内核 tracer bullet(缓存→词典→规则→本地LLM→验证)

## 状态: pending

**Blocked by:** ticket-001, ticket-003
**产出:** CLI 端到端真实翻译,工程书 Test 1/2/3 通过

**做什么:** 打通内核全链路(第一版无云端):
1. normalize + source_hash(SHA256; 上下文敏感文本 hash 含 relevant context, 工程书 §37)。
2. L1 内存 LRU 缓存(容量可配)。
3. 术语词典: database/ 内置 seed 词典(YAML/CSV 起步, sqlite 在 ticket-006),
   精确匹配直接返回(locked=true 优先);seed 至少含: dwarf=矮人, wooden barrel=木桶,
   carpenter=木匠, cancel=取消, fortress=要塞, migrate=迁徙 等 50+ 常用 DF 术语。
4. 规则引擎: 模板句(如 "{name} cancels {job}.")→ 中文模板,置信度阈值 0.9。
5. 本地 LLM 适配器: ollama /api/chat 调 gemma-4b-trans(调用方式以 ticket-001
   审计结论为准),超时 8s,系统提示词版本化到 prompts/translation_system_v1.txt
   (内容按工程书 §19.1 十二条),游戏文本标注为不可信内容(§21)。
6. Validator 基础版: 数字集合守恒、非空、长度比异常(>3x 或 <0.3x)拒绝。
7. 失败链路: LLM 失败/超时/验证不过 → 返回原文并标 confidence=0(绝不抛异常阻塞)。

**验收标准:**
- [ ] `echo "Dwarf" | df-fanyi translate` → 矮人;`wooden barrel` → 木桶(Test 1/2)
- [ ] `echo "Urist cancels Make Wooden Barrel." | df-fanyi translate` 输出合理中文(Test 3)
- [ ] 停掉 ollama 再跑:输出原文、进程退出码 0、日志记 fallback
- [ ] pytest 全绿,LLM 用录制回放假实现,不依赖真实 ollama
