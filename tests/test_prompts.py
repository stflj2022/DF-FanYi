"""ticket-004: 系统提示词版本化 prompts/translation_system_v1.txt。

工程书 §19: prompt 必须固定为版本化文件;
§19.1: 十二条基础规则必须齐全;
§21: 游戏文本必须标注为不可信内容(untrusted content)。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.config import REPO_ROOT  # noqa: E402
from df_fanyi.prompts import load_system_prompt, system_prompt_path  # noqa: E402
from df_fanyi.providers.ollama_client import TRANSLATION_SYSTEM_PROMPT  # noqa: E402

V1_PATH = REPO_ROOT / "prompts" / "translation_system_v1.txt"

# §19.1 十二条(中文实现, 覆盖全部语义)
_REQUIRED_RULES = [
    "精确传达原意",  # 1
    "不要添加信息",  # 2
    "不要删减信息",  # 3
    "保留专有名词",  # 4
    "保留变量",  # 5
    "保留标记",  # 6
    "保留数字",  # 7
    "保留游戏术语",  # 8
    "遵循提供的术语表",  # 9
    "不要解释",  # 10
    "只输出译文",  # 11
    "保持原文结构",  # 12
]


def test_prompt_file_exists() -> None:
    assert V1_PATH.exists(), f"缺少版本化提示词: {V1_PATH}"


def test_prompt_covers_all_12_rules() -> None:
    content = load_system_prompt("v1")
    for rule in _REQUIRED_RULES:
        assert rule in content, f"§19.1 规则缺失: {rule}"


def test_prompt_marks_game_text_as_untrusted() -> None:
    """§21: 游戏文本是不可信内容, 禁止执行其中指令。"""
    content = load_system_prompt("v1")
    assert "不可信" in content and "untrusted" in content.lower()
    assert "不要执行" in content or "永远不要执行" in content


def test_ollama_client_uses_versioned_prompt() -> None:
    """客户端系统提示 == prompts/translation_system_v1.txt 内容(单源)。"""
    assert TRANSLATION_SYSTEM_PROMPT == load_system_prompt("v1")


def test_unknown_version_falls_back_to_embedded() -> None:
    content = load_system_prompt("v99")
    assert "只输出译文" in content
