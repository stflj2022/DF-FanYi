# PRETRANSLATE_SCAN — vanilla raws 扫描报告(ticket-011)

- 扫描根: `/home/wu/Games/DwarfFortress/data/vanilla`
- 扫描文件数: **88**
- 可翻译字段值(分句前): **12388**
- 分句后片段总数: **12706**
- 去重后片段数: **6964**(重复 5742 条)
- 名称类: **5705** / 描述类: **1259**

## 字符长度分布(去重后)

| 长度区间 | 句数 |
| --- | --- |
| 1-10 | 1830 |
| 11-20 | 3264 |
| 21-40 | 947 |
| 41-80 | 843 |
| 81-160 | 80 |
| 160+ | 0 |

## 按目录分布(去重后)

| 目录 | 句数 |
| --- | --- |
| vanilla_creatures | 4285 |
| vanilla_creatures_extinct | 990 |
| vanilla_plants | 899 |
| vanilla_materials | 314 |
| vanilla_items | 211 |
| vanilla_descriptors | 182 |
| vanilla_entities | 75 |
| vanilla_buildings | 4 |
| vanilla_environment | 2 |
| vanilla_interactions | 2 |

## 抽样(前 10 句人工抽查)

| # | 原文 | tag | 来源 |
| --- | --- | --- | --- |
| 1 | Vanilla Buildings | NAME | vanilla_buildings/info.txt:7 |
| 2 | These are the default Dwarf Fortress buildings. | DESCRIPTION | vanilla_buildings/info.txt:8 |
| 3 | Soap Maker's Workshop | NAME | vanilla_buildings/objects/building_custom.txt:6 |
| 4 | Screw Press | NAME | vanilla_buildings/objects/building_custom.txt:44 |
| 5 | Vanilla Creatures | NAME | vanilla_creatures/info.txt:7 |
| 6 | These are the default Dwarf Fortress creatures. | DESCRIPTION | vanilla_creatures/info.txt:8 |
| 7 | A squat amphibian with leathery skin, found in relatively dry areas. | DESCRIPTION | vanilla_creatures/objects/creature_amphibians.txt:6 |
| 8 | toad | NAME | vanilla_creatures/objects/creature_amphibians.txt:7 |
| 9 | toads | NAME | vanilla_creatures/objects/creature_amphibians.txt:7 |
| 10 | beauty | PREFSTRING | vanilla_creatures/objects/creature_amphibians.txt:17 |

## 真实云端试点(--limit 50, 2026-09-08)

命令: `python3 -m df_fanyi pretranslate run --limit 50`

| 指标 | 值 |
| --- | --- |
| 目标句数 | 50(全部 vanilla_buildings + vanilla_creatures 开头) |
| 翻译成功 | **50/50** |
| 验证通过率 | 100%(§24 校验丢弃 0) |
| 批次 | 5 批 × 12 句(batch_size=12) |
| 请求延迟 | p50=23.0s p95=29.4s(经 Pi Model Router → glm-5.3-flash) |
| 重试 | 2 次(同一批连续两次空 content, 指数退避 2s/4s 后成功; 疑为上游瞬时故障/思维链自适应占用) |
| 安装 | TM 行 55 | 锁定名称词条 42(provider=cloud-pretranslate) |
| 引擎命中 | `Soap Maker's Workshop → 制皂工坊`, provider=cloud-pretranslate, cache_hit=true, 零 LLM(实测) |
| 断点续跑 | 重跑 `--limit 5`: 跳过已有 50, 只补缺 5 ✓ |

**结论**: 试点数据支撑全量放开 —— 全量 6964 句 ≈ 581 请求(12 句/批)× ~23s ≈
3.7 小时(含 2s 节流间隔), 建议分多轮跑(见 PRETRANSLATE_GUIDE.md)。
全量命令:

```bash
nohup python3 -m df_fanyi pretranslate run >> logs/pretranslate.log 2>&1 &
# 中断后重跑同一命令即自动续传; 完成后:
python3 -m df_fanyi pretranslate install
```
