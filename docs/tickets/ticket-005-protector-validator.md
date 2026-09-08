# ticket-005: Markup/Variable 保护器 + 完整 Validator

## 状态: pending

**Blocked by:** ticket-004
**产出:** 工程书 Test 4/5 通过的守护层

**做什么:** 工程书 §22-24:
1. Markup Protector: `<color=red>Urist</color>` 类标记翻译前转 MARKUP_001/002 占位,译后还原。
2. Variable Protector: `{COUNT}` `{UNIT_NAME}` 等转 VAR_001/002,译后还原。
3. Validator 完整版: 原文/译文的 变量集合、markup 占位集合、数字集合、行数
   必须一致;不合格返回 {valid:false, errors:[...]} 并触发回退(原文或词典拼接)。
4. 与管线集成: protect → translate → validate → restore 顺序,单元测试覆盖
   恶意样例(游戏文本内嵌 "ignore instructions" 类注入,验证不被执行)。

**验收标准:**
- [ ] `{COUNT} dwarves` 译后 {COUNT} 完整保留(Test 4)
- [ ] `<color=red>Urist</color>` 译后标记完整且 Urist 不被误翻(Test 5)
- [ ] 丢失占位符的译文被拒绝并回退;pytest 全绿
