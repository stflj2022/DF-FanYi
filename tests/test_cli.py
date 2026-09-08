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
    # 不用 st_size 切片对比: 本机其它进程(驱动/看门狗/进度报告/平行开发)可能并发
    # 追加 engine.log, 字节偏移会被插入的内容推向错误位置(偶发误报)。
    # 改为断言本次运行新增了一条回退记录(计数增加), 同样精确且抗并发。
    before = log.read_text(encoding="utf-8").count("回退原文") if log.exists() else 0
    p = _run(
        "translate", "--config", str(cfg_path), stdin="Stray dog has given birth to puppies.\n"
    )
    assert p.returncode == 0, p.stderr
    after = log.read_text(encoding="utf-8").count("回退原文")
    assert after > before, "本次运行未新增 回退原文 日志记录"
    assert "fallback" in log.read_text(encoding="utf-8").lower(), "日志未记录 fallback"


# --- ticket-006: 术语管理 CLI(§8.1) ----------------------------------------

def _term_config(tmp_path: Path) -> tuple[Path, Path]:
    """写一份显式配置: 库文件指向临时目录(测试不落用户目录/仓库默认库)。"""
    db = tmp_path / "terms.db"
    cfg = tmp_path / "terms.yaml"
    cfg.write_text(f"cache:\n  l2_path: {db}\n", encoding="utf-8")
    return cfg, db


def test_term_help_lists_add_and_list() -> None:
    p = _run("term", "--help")
    assert p.returncode == 0, p.stderr
    assert "add" in p.stdout and "list" in p.stdout


def test_term_add_then_list(tmp_path: Path) -> None:
    """验收: `df-fanyi term add` 后 `term list` 可见(locked 默认 false)。"""
    cfg, db = _term_config(tmp_path)
    p = _run("term", "add", "--config", str(cfg), "Goblin", "哥布林")
    assert p.returncode == 0, p.stderr
    assert db.exists(), "term add 应创建库文件"
    p2 = _run("term", "list", "--config", str(cfg))
    assert p2.returncode == 0, p2.stderr
    assert "Goblin" in p2.stdout and "哥布林" in p2.stdout
    assert "locked\t0" in p2.stdout.replace(" ", "").replace("\t\t", "\t") or \
        _locked_col(p2.stdout) == "0", "locked 默认应为 false(0)"


def _locked_col(stdout: str) -> str:
    """从 term list 制表输出取第 6 列(locked)。"""
    for line in stdout.strip().splitlines()[1:]:  # 跳过表头
        cols = line.split("\t")
        return cols[5] if len(cols) > 5 else "?"
    return "?"


def test_term_add_locked_and_list_locked_only(tmp_path: Path) -> None:
    cfg, _ = _term_config(tmp_path)
    p = _run("term", "add", "--config", str(cfg), "Goblin", "哥布林", "--locked", "--category", "creature")
    assert p.returncode == 0, p.stderr
    p = _run("term", "add", "--config", str(cfg), "Apple", "苹果")
    assert p.returncode == 0, p.stderr
    p2 = _run("term", "list", "--config", str(cfg))
    assert _locked_col(p2.stdout) in ("0", "1")
    p3 = _run("term", "list", "--locked-only", "--config", str(cfg))
    assert p3.returncode == 0, p3.stderr
    assert "Goblin" in p3.stdout and "Apple" not in p3.stdout, "locked-only 应只含锁定词条"


def test_term_add_updates_existing(tmp_path: Path) -> None:
    """重复 add 同源词条 = 更新不重复插入。"""
    cfg, _ = _term_config(tmp_path)
    assert _run("term", "add", "--config", str(cfg), "Goblin", "哥布林").returncode == 0
    assert _run("term", "add", "--config", str(cfg), "Goblin", "地精").returncode == 0
    out = _run("term", "list", "--config", str(cfg)).stdout
    assert out.count("Goblin") == 1, "同源词条重复 add 不应产生重复行"


def test_term_add_affects_translation(tmp_path: Path) -> None:
    """验收: CLI 添加的词条立即参与翻译管线(整句精确匹配, 词典命中)。"""
    cfg, _ = _term_config(tmp_path)
    p = _run("term", "add", "--config", str(cfg), "Goblin", "哥布林", "--locked")
    assert p.returncode == 0, p.stderr
    p2 = _run(
        "translate",
        "--config",
        str(cfg),
        "--json",
        stdin="Goblin\n",
    )
    assert p2.returncode == 0, p2.stderr
    data = json.loads(p2.stdout)
    assert data["text"] == "哥布林"
    assert data["model"] == "dictionary"
    assert data["confidence"] == 1.0
