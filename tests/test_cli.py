"""ticket-004 验收: CLI 端到端真实翻译(工程书 Test 1/2/3) + 日志 + 断网回退。

验收标准:
- [x] `echo "Dwarf" | df-fanyi translate` → 矮人(Test 1)
- [x] `echo "wooden barrel" | df-fanyi translate` → 木桶(Test 2)
- [x] `echo "Urist cancels Make Wooden Barrel." | df-fanyi translate` → 合理中文(Test 3)
- [x] LLM 不可用(坏 base_url 等价停掉 ollama) → 输出原文、退出码 0、日志记 fallback
- [x] 日志 logs/engine.log 默认 INFO(工程书 §45)

用 subprocess 走真实 CLI 进程(外部行为); 词典/规则路径零网络,
LLM 路径用坏 base_url 模拟 ollama 停机(连接立即被拒)。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "df_fanyi", *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
    )


def test_cli_help_lists_subcommands() -> None:
    p = _run("--help")
    assert p.returncode == 0, p.stderr
    assert "translate" in p.stdout
    assert "selftest" in p.stdout


def test_translate_dwarf_returns_dictionary_hit() -> None:
    """Test 1: Dwarf → 矮人(词典精确命中, 零网络)。"""
    p = _run("translate", stdin="Dwarf\n")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "矮人"


def test_translate_wooden_barrel_returns_dictionary_hit() -> None:
    """Test 2: wooden barrel → 木桶。"""
    p = _run("translate", stdin="wooden barrel\n")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "木桶"


def test_translate_dynamic_sentence_returns_chinese() -> None:
    """Test 3: 动态句子 → 合理中文(规则模板: 取消 + 木桶)。"""
    p = _run("translate", stdin="Urist cancels Make Wooden Barrel.\n")
    assert p.returncode == 0, p.stderr
    out = p.stdout.strip()
    assert "取消" in out and "木桶" in out
    assert out.startswith("Urist"), "人名应保留"


def test_translate_json_has_contract_fields() -> None:
    p = _run("translate", "--json", stdin="Urist cancels Make Wooden Barrel.\n")
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert set(data) >= {"text", "source_text", "model", "provider", "confidence", "cache_hit"}
    assert data["source_text"] == "Urist cancels Make Wooden Barrel."
    assert data["model"] == "rule"
    assert data["confidence"] >= 0.9
    assert data["error"] is None


def test_selftest_exits_zero() -> None:
    p = _run("selftest")
    assert p.returncode == 0, p.stderr
    assert "配置" in p.stdout
    assert "日志" in p.stdout


def test_engine_log_written() -> None:
    """工程书 §45: logs/engine.log, 默认 INFO。"""
    log = REPO / "logs" / "engine.log"
    before = log.stat().st_size if log.exists() else 0
    p = _run("translate", stdin="Dwarf\n")
    assert p.returncode == 0, p.stderr
    assert log.exists(), "logs/engine.log 未创建"
    assert log.stat().st_size > before, "engine.log 未写入 INFO 记录"


def test_translate_empty_stdin_fails_cleanly() -> None:
    p = _run("translate", stdin="\n\n")
    assert p.returncode == 2
    assert p.stderr.strip() != ""


def test_ollama_down_falls_back_to_original(tmp_path: Path) -> None:
    """验收: 停掉 ollama(模拟: base_url 指向不可达端口) → 输出原文、退出码 0。"""
    cfg_path = tmp_path / "broken.yaml"
    cfg_path.write_text(
        "local_llm:\n  base_url: http://127.0.0.1:1\n  model: gemma-4b-trans\n  timeout_s: 5\n",
        encoding="utf-8",
    )
    src = "Stray dog has given birth to puppies."
    p = _run("translate", "--config", str(cfg_path), stdin=src + "\n")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == src, "LLM 不可用时应输出原文"


def test_ollama_down_logs_fallback(tmp_path: Path) -> None:
    """验收: 日志记录 fallback(engine.log 出现回退原文)。"""
    cfg_path = tmp_path / "broken.yaml"
    cfg_path.write_text(
        "local_llm:\n  base_url: http://127.0.0.1:1\n  model: gemma-4b-trans\n  timeout_s: 5\n",
        encoding="utf-8",
    )
    log = REPO / "logs" / "engine.log"
    before = log.stat().st_size if log.exists() else 0
    p = _run(
        "translate", "--config", str(cfg_path), stdin="Stray dog has given birth to puppies.\n"
    )
    assert p.returncode == 0, p.stderr
    after = log.read_text(encoding="utf-8")[before:]
    assert "回退原文" in after or "fallback" in after.lower(), "日志未记录 fallback"
