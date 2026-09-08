# ticket-003: Python 翻译内核项目骨架

## 状态: pending

**Blocked by:** 无(可立即开始)
**产出:** 可安装、可测试、可运行 CLI 的内核骨架

**做什么:** 按 specs 目录结构搭出翻译层骨架(tracer bullet 之基座):
1. pyproject.toml(项目名 df-fanyi,Python ≥3.11,依赖: pyyaml, requests;
   开发: pytest, pytest-asyncio)。包名 df_fanyi,含 core/{orchestrator,queue,validator,
   context,capture,parser,segmenter}、local/gemma、database、router、providers 空模块。
2. config/default.yaml(照工程书 §47: 缓存、context 上限、队列、local_llm.workers=1、
   provider 结构 base_url/api_key_env/model);config 加载器(含
   ~/.config/df-fanyi/ 用户覆盖 + secrets.env 只从 env 读)。
3. CLI 入口 `df-fanyi`: 子命令 translate(读 stdin 单句出译文)、selftest。
4. tests/ 冒烟: 配置加载、CLI --help、translate 子命令走假 LLM 返回占位译文。
5. 日志: logs/engine.log,默认 INFO(工程书 §45)。

**验收标准:**
- [ ] `python -m df_fanyi --help` 与 `df-fanyi translate` 可运行
- [ ] `pytest tests/ -q` 全绿(测试命令即无人值守 driver 的 TEST_COMMAND)
- [ ] config/default.yaml 结构齐全且可被用户目录覆盖;任何代码文件不含密钥
