# ticket-011: 云端批量预翻译 vanilla raws(离线翻译包)

## 状态: pending
**Blocked by:** ticket-010
**依据:** ADR-cloud-first-translation(docs/decisions/ADR-cloud-first-translation.md, 用户 2026-09-08 定: 大量翻译内容优先云端)+ 用户追加决策: 除运行时按需翻译外, 增加批量预翻译模式
**产出:** 可断点续跑的云端批量预翻译管线 + 离线翻译包(装入 L2 词典层)
**背景:** 游戏文本量 ≈17 万词(data/vanilla 196 个 raws 文件, 生物/物品/植物/材料描述与名称)。运行时按需翻译只覆盖公告/字幕, 不覆盖描述类文本; 批量预翻把全量文本离线译好, 游戏命中直接用(零延迟), 运行时只兜底动态句。

**做什么:** 工程书 §8.2(TM 表)/§12(cloud 接口)/§19(提示词版本化)/§24(验证)/§46(privacy):
1. **raws 扫描器** `df_fanyi/pretranslate/scanner.py`: 解析 data/vanilla/*.txt 的
   `[TAG:value]` 结构, 提取可翻译文本: `[DESCRIPTION:...]`、`[NAME:...]`、
   `[CASTE_NAME:...]`、`[CREATURE_TILE:...]` 之外的人类可读字段(名称/描述优先)。
   分句、去重、hash(source_hash 复用 core/parser)。产出 scan 报告:
   总句数/去重后句数/字符分布(写入 docs/audits/PRETRANSLATE_SCAN.md)。
   注意 raws 文本大量模板复用, 去重后预计 3-6 万句级别。
2. **批量翻译器** `df_fanyi/pretranslate/batch.py`: 复用 providers/router_client
   (RouterChatClient → model-router `router/L2`, 云端优先 ADR)。
   - 每请求打包 10-20 句(JSON 数组进出, 提示词 translation_system_v1 + 批量输出格式约束),
     content 解析容错(剥 ```json 围栏, reasoning 字段丢弃);
   - 节流: 请求间隔 ≥2s, 对 router 503/额度错误(智谱窗口耗尽等)指数退避并暂停,
     绝不绕过 model-router 直连上游;
   - **断点续跑**: 进度落 data/pretranslate_progress.json(已译 hash 集合), 重跑只补缺;
   - 复用 core/validator 逐句校验(长度比 0.3-3x/占位符守恒), 不合格丢弃并记录;
   - 假 LLM 录制回放测试(与 ollama_client 同约定, 不 mock 内部实现)。
3. **安装/集成** `df_fanyi/pretranslate/install.py`: 译文批量写入 L2
   translation_memory(provider="cloud-pretranslate", confidence=验证得分),
   terminology 层: 生物/物品**名称**类词条以 locked=true 写入 terminology
   (运行时名称翻译零 LLM)。引擎查询链不变(缓存→L2 TM 已天然覆盖)。
   CLI: `df-fanyi pretranslate scan|run --limit N|status|install`。
4. **试点先行**: 先跑 `--limit 50` 真实云端小批量(工程书 Test 精神: 数据说话),
   记录命中率/延迟/验证通过率进 PRETRANSLATE_SCAN.md, 再放开全量。
   全量预计 2-3 千请求(DeepSeek Flash 档), 数小时级, 可分多轮跑。
5. **文档**: docs/audits/PRETRANSLATE_GUIDE.md(操作手册: 扫描→试点→全量→安装)。

**验收标准:**
- [ ] scanner 在真实 raws 上产出 scan 报告(总句数/去重句数/抽样 10 句人工可读)
- [ ] batch 用假 LLM 回放测试全绿(含: 断点续跑、503 退避暂停、不合格句丢弃)
- [ ] `--limit 50` 真实云端试点完成, 统计表落 PRETRANSLATE_SCAN.md
- [ ] install 后: 引擎对包内句子的查询命中 pretranslate 且零 LLM 调用(测试为证)
- [ ] pytest 全绿; 全量运行命令在 GUIDE 中给出(允许跨多轮执行)
