"""工程书 §2.4/§56 强制禁止事项: API Key 永不进入代码与 Git。

扫描源码与配置文件, 禁止出现密钥形态(sk-/ghp_/AIza/xox)。
模式由片段拼装, 避免本文件自身被误扫。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_PATTERNS = [
    re.compile(r"sk-" + r"[A-Za-z0-9]{16,}"),           # OpenAI 系
    re.compile(r"ghp_" + r"[A-Za-z0-9]{20,}"),          # GitHub PAT
    re.compile(r"AIza" + r"[0-9A-Za-z_-]{20,}"),        # Google API
    re.compile(r"xox[baprs]-" + r"[A-Za-z0-9-]{10,}"),  # Slack
]


def _source_files():
    roots = [REPO / "df_fanyi", REPO / "config", REPO / "scripts", REPO / "tests"]
    for root in roots:
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file():
                continue
            if f.name == "test_no_secrets.py":
                continue
            if f.suffix in (".py", ".yaml", ".yml", ".toml", ".sh"):
                yield f
    yield REPO / "pyproject.toml"


def test_no_api_key_shaped_secret_in_source() -> None:
    hits = []
    for f in _source_files():
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if any(p.search(line) for p in _PATTERNS):
                hits.append(f"{f.relative_to(REPO)}:{i}")
    assert not hits, f"源码/配置中发现疑似密钥: {hits}"


def test_default_config_never_assigns_api_key_value() -> None:
    text = (REPO / "config" / "default.yaml").read_text(encoding="utf-8")
    assert "api_key:" not in text, "default.yaml 只允许 api_key_env(环境变量名), 禁止直接写密钥"


def test_code_never_reads_secrets_file() -> None:
    """代码只从环境变量读密钥, 禁止直接读取 secrets.env 文件(§2.4)。"""
    for f in _source_files():
        if not f.exists() or f.suffix != ".py":
            continue
        text = f.read_text(encoding="utf-8")
        assert "secrets.env" not in text, f"{f} 疑似直接读取 secrets.env 文件"
        assert "open(" not in text or "secrets" not in text, f"{f} 疑似读取密钥文件"
