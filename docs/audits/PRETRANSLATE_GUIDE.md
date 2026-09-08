# PRETRANSLATE_GUIDE — 云端批量预翻译操作手册(ticket-011)

目的: 把 vanilla raws 的名称/描述**离线批量译好**装入 L2 词典层, 游戏命中
直接用(零延迟), 运行时 LLM 只兜底动态句(ADR-cloud-first-translation)。

## 0. 前置

- Pi Model Router 在线: `curl -s http://127.0.0.1:8010/v1/models`(云端 key 由
  router 侧持有, DF-FanYi 侧零密钥, 工程书 §12/§2.4)。
- DF raws 存在: `~/Games/DwarfFortress/data/vanilla`(或仓库 `data/vanilla`,
  或 config `pretranslate.vanilla_dir` / CLI `--vanilla-dir` 显式指定)。

## 1. 扫描(只读, 随时可跑)

```bash
python3 -m df_fanyi pretranslate scan
```

- 产出 `docs/audits/PRETRANSLATE_SCAN.md`: 文件数/字段值/分句后/去重后句数、
  字符分布、按目录分布、抽样 10 句。
- 2026-09-08 基线: 88 文件 → 12388 字段值 → 去重后 **6964** 句
  (名称 5705 / 描述 1259)。

## 2. 试点(数据说话, 先小后大)

```bash
python3 -m df_fanyi pretranslate run --limit 50
python3 -m df_fanyi pretranslate status
```

- 50 句 ≈ 5 请求 × ~23s ≈ 2 分钟; 看丢弃数是否为 0、有无重试。
- 试点结论见 PRETRANSLATE_SCAN.md「真实云端试点」节。

## 3. 全量(可分多轮, 断点续跑)

```bash
nohup python3 -m df_fanyi pretranslate run >> logs/pretranslate.log 2>&1 &
tail -f logs/pretranslate.log
```

- **断点续跑**: 进度=翻译包 `data/pretranslate_progress.json`(已译/已弃 hash),
  中断(Ctrl-C/断电/router 长时间不可用)后**重跑同一命令只补缺**。
- 节流与退避: 请求间隔 ≥2s; 503/额度耗尽指数退避(2s→4s→…→120s), 重试 5 次
  耗尽自动停本轮, 进度已落盘。
- 全量 ≈ 581 请求 ≈ 3.7 小时(单轮), 也可 `--limit N` 分夜跑。

## 4. 安装(幂等, 可重复执行)

```bash
python3 -m df_fanyi pretranslate install
```

- 译文 → L2 `translation_memory`(provider=`cloud-pretranslate`,
  confidence=§24 验证得分); 引擎查询链不变, 运行时对包内句**零 LLM**。
- 名称类词条(kind=name)→ `terminology` **locked=true**(运行时名称零 LLM 且
  不被后续翻译覆盖)。
- privacy `store_source_text: false` 的包: TM 照常装, 名称词条因缺原文跳过。

## 5. 验证

```bash
echo "Soap Maker's Workshop" | python3 -m df_fanyi translate --json
# → text=制皂工坊 provider=cloud-pretranslate cache_hit=true
python3 -m df_fanyi pretranslate status   # 已译/丢弃/请求数
```

## 6. 故障与维护

| 现象 | 处置 |
| --- | --- |
| run 停轮 `router 持续不可用` | 查 model-router/上游额度, 恢复后重跑(自动续传) |
| 丢弃数增长 | 看 progress.json `dropped` 里的 reason(§24 长度比/占位符) |
| 想重译某句 | 从 `translations`/`dropped` 删该 hash 条目后重跑即补 |
| DF 版本升级 | 重跑 scan → run(新文本自动补译)→ install(§43) |

## 7. 实现索引

- 扫描器: `df_fanyi/pretranslate/scanner.py`(tag 白名单/分句/引擎同款 hash)
- 批量器: `df_fanyi/pretranslate/batch.py`(JSON 数组进出/围栏容错/退避/续跑)
- 安装器: `df_fanyi/pretranslate/install.py`(TM + locked 术语, 幂等)
- 提示词: `prompts/translation_system_v1.txt` + `prompts/pretranslate_batch_v1.txt`(§19 版本化)
- 测试: `tests/test_pretranslate_scanner.py` / `test_pretranslate_batch.py`
  / `test_pretranslate_install.py` / `test_pretranslate_cli.py`
