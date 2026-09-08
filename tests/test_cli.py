"""ticket-003 验收: CLI 可运行 + translate 走假 LLM 占位译文 + 日志落盘。

验收标准:
- [ ] `python -m df_fanyi --help` 与 `df-fanyi translate` 可运行
- [ ] translate 子命令走假 LLM 返回占位译文
- [ ] 日志 logs/engine.log 默认 INFO(工程书 §45)

用 subprocess 走真实 CLI 进程(外部行为), 假 LLM = 骨架内置占位翻译器(零网络)。
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


def test_translate_stdin_returns_placeholder_translation() -> None:
    p = _run("translate", stdin="Dwarf\n")
    assert p.returncode == 0, p.stderr
    assert "Dwarf" in p.stdout.strip()


def test_translate_json_has_contract_fields() -> None:
    p = _run("translate", "--json", stdin="Urist cancels Make Wooden Barrel.\n")
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert set(data) >= {"text", "source_text", "model", "provider", "confidence", "cache_hit"}
    assert data["source_text"] == "Urist cancels Make Wooden Barrel."
    assert data["confidence"] == 0.0


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
