"""ticket-001 验收测试: docs/audits/ENVIRONMENT_AUDIT.md 必须含全部环境事实。

只测外部行为(审计文档的内容可读性), 不 mock 任何内部实现。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUDIT = REPO / "docs" / "audits" / "ENVIRONMENT_AUDIT.md"


def _audit_text() -> str:
    assert AUDIT.exists(), f"缺少审计文档: {AUDIT}"
    return AUDIT.read_text(encoding="utf-8")


def test_audit_doc_exists() -> None:
    assert AUDIT.exists()


def test_contains_df_version() -> None:
    text = _audit_text()
    assert "53.16" in text, "缺少 DF 版本号 53.16"


def test_contains_dfhack_version() -> None:
    text = _audit_text()
    assert "53.16-r1.1" in text, "缺少 DFHack 版本号 53.16-r1.1"


def test_contains_classic_support_conclusion() -> None:
    text = _audit_text()
    assert "classic" in text.lower(), "缺少 classic(非 Steam) 支持结论"


def test_contains_ollama_chat_invocation_sample() -> None:
    text = _audit_text()
    assert "/api/chat" in text, "缺少 /api/chat 调用样例"
    assert "gemma-4b-trans" in text, "缺少模型名"


def test_contains_measured_latency_data() -> None:
    text = _audit_text()
    assert "tok/s" in text, "缺少生成速度(tok/s)"
    assert "首载" in text, "缺少首载延迟"


def test_contains_df_startup_and_dfhack_liveness_commands() -> None:
    text = _audit_text()
    assert "./dfhack" in text, "缺少 DF 启动命令"
    assert "dfhack-run" in text, "缺少 dfhack-run 存活确认命令"
    assert "eventful" in text and "overlay" in text, "缺少关键插件确认(eventful/overlay)"
