"""ticket-011 验收: CLI `df-fanyi pretranslate scan|run|status|install` 接线。

run 子命令走真实云端 router, 不在单测里执行(试点由验收流程人工触发);
此处验证 scan/status 的子进程外部行为与 parser 接线。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
RAWS = REPO / "tests" / "fixtures" / "raws"


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "df_fanyi", *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
    )


def test_help_lists_subcommands() -> None:
    p = _run("--help")
    assert p.returncode == 0, p.stderr
    assert "pretranslate" in p.stdout


def test_pretranslate_help_lists_all_subcommands() -> None:
    p = _run("pretranslate", "--help")
    assert p.returncode == 0, p.stderr
    for cmd in ("scan", "run", "status", "install"):
        assert cmd in p.stdout


def test_scan_on_fixture_raws_writes_report(tmp_path: Path) -> None:
    report = tmp_path / "SCAN.md"
    p = _run("pretranslate", "scan", "--vanilla-dir", str(RAWS), "--report", str(report))
    assert p.returncode == 0, p.stderr
    assert "去重后 23" in p.stdout
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "PRETRANSLATE_SCAN" in text
    assert "blue jay" in text  # 抽样人工可读


def test_status_before_run_is_friendly(tmp_path: Path) -> None:
    p = _run("pretranslate", "status", "--progress-path", str(tmp_path / "none.json"))
    assert p.returncode == 0, p.stderr
    assert "未开始" in p.stdout


def test_status_after_run_counts(tmp_path: Path) -> None:
    """进程内跑一个小包, 再走 CLI status(跨进程读同一进度文件)。"""
    sys.path.insert(0, str(REPO))
    from df_fanyi.core.parser import normalize_text, source_hash
    from df_fanyi.core.protector import Protector
    from df_fanyi.pretranslate.batch import BatchTranslator
    from df_fanyi.pretranslate.scanner import Segment
    from df_fanyi.providers.router_client import RouterChatClient

    text = normalize_text("blue jay")
    protected, _ = Protector().protect(text)
    segment = Segment(
        hash=source_hash(protected), text=text, kind="name", tag="NAME",
        rel_path="f.txt", line=1,
    )

    def transport(endpoint: str, payload: dict) -> dict:
        return {
            "model": "glm-5.3-flash",
            "choices": [{"message": {"role": "assistant", "content": '["冠蓝鸦"]'}}],
        }

    progress = tmp_path / "progress.json"
    translator = BatchTranslator(
        RouterChatClient(transport=transport),
        progress_path=progress,
        sleep_fn=lambda _s: None,
    )
    assert translator.run([segment]).ok

    p = _run("pretranslate", "status", "--progress-path", str(progress))
    assert p.returncode == 0, p.stderr
    assert "已译 1" in p.stdout
    assert "cloud-pretranslate" in p.stdout


def test_install_missing_progress_fails_cleanly(tmp_path: Path) -> None:
    p = _run("pretranslate", "install", "--progress-path", str(tmp_path / "no.json"))
    assert p.returncode != 0
    assert "翻译包" in (p.stderr + p.stdout) or "No such file" in (p.stderr + p.stdout)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
