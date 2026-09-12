"""ticket-003 验收: 配置加载(分层 + 密钥只从 env 读)。

工程书依据:
- §47 配置文件分层: config/default.yaml 默认值, ~/.config/df-fanyi/config.yaml 用户覆盖
- §2.4 API Key 永不进入 Git: 密钥只从环境变量读取(api_key_env 指定变量名)
- §27 本地 gemma 并发 = 1(local_llm.workers)
- §36 context 不允许无限增长(max_tokens/recent_events/recent_texts 上限)

只测外部行为(加载结果), 不 mock。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.config import Config, ConfigError, load_config  # noqa: E402


def test_default_config_loads() -> None:
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert cfg.config_path.name == "default.yaml"


def test_default_config_has_all_required_sections() -> None:
    cfg = load_config()
    for key in ("cache", "context", "queue", "local_llm", "providers", "logging", "privacy"):
        assert key in cfg.raw, f"default.yaml 缺少 §47 必需节: {key}"


def test_local_llm_workers_is_one() -> None:
    """工程书 §27: 本地 gemma 并发恒 1(CPU 单实例)。"""
    assert cfg().local_llm["workers"] == 1


def test_context_limits_present() -> None:
    """工程书 §36: 上下文不允许无限增长。"""
    c = cfg().context
    assert c["max_tokens"] == 3500
    assert c["recent_events"] == 10
    assert c["recent_texts"] == 10


def test_provider_structure_has_base_url_api_key_env_model() -> None:
    """工程书 §12/§17: provider = base_url + api_key_env + model。"""
    providers = cfg().providers
    assert len(providers) >= 1
    for p in providers:
        assert p.name
        assert p.base_url
        assert p.model
        assert isinstance(p.api_key_env, str)


def test_user_config_overrides_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """用户目录 ~/.config/df-fanyi/config.yaml 整树覆盖, 未覆盖键保留(深合并)。"""
    home = tmp_path / "home"
    user_cfg = home / ".config" / "df-fanyi" / "config.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text("local_llm:\n  workers: 2\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    cfg = load_config()
    assert cfg.local_llm["workers"] == 2, "用户覆盖未生效"
    assert cfg.context["max_tokens"] == 3500, "深合并丢失未覆盖的默认值"


def test_api_key_read_only_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """密钥只从环境变量读取: 未设 env → None+不可用; 设 env → 可用; raw 永不含密钥。"""
    cfg_path = tmp_path / "cloud.yaml"
    cfg_path.write_text(
        "providers:\n"
        "  - name: testcloud\n"
        "    base_url: https://example.invalid/v1\n"
        "    api_key_env: DF_FANYI_TEST_KEY\n"
        "    model: test-model\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DF_FANYI_TEST_KEY", raising=False)

    cfg = load_config(cfg_path)
    p = cfg.providers[0]
    assert p.api_key is None
    assert p.available is False, "缺 key 的云端 provider 必须标记不可用"

    monkeypatch.setenv("DF_FANYI_TEST_KEY", "sk-test-1234567890abcdef")
    cfg2 = load_config(cfg_path)
    assert cfg2.providers[0].api_key == "sk-test-1234567890abcdef"
    assert cfg2.providers[0].available is True
    # 密钥绝不回写 raw 配置, 防止 dump/日志带出(§2.4)
    assert "sk-test-1234567890abcdef" not in str(cfg2.raw)


def test_local_provider_available_without_key() -> None:
    """本地 ollama 无 key 语义: 2026-09-10 起用户决策停用本地兜底,
    default.yaml 里 enabled: false → available 必须为 False(云端唯一)。
    """
    ollama = [p for p in cfg().providers if p.name == "ollama"]
    assert ollama, "default.yaml 必须含本地 ollama provider"
    assert ollama[0].api_key_env == ""
    assert ollama[0].enabled is False, "ollama 已退役(云端唯一决策)"
    assert ollama[0].available is False


def test_disabled_provider_not_available(tmp_path: Path) -> None:
    cfg_path = tmp_path / "disabled.yaml"
    cfg_path.write_text(
        "providers:\n"
        "  - name: off\n"
        "    base_url: http://x\n"
        "    api_key_env: ''\n"
        "    model: m\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    assert load_config(cfg_path).providers[0].available is False


def test_invalid_workers_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("local_llm:\n  workers: 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_invalid_logging_level_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad-level.yaml"
    bad.write_text("logging:\n  level: LOUD\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_missing_explicit_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def cfg() -> Config:
    return load_config()
