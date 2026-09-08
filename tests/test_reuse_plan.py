"""ticket-002 验收测试: docs/audits/REUSE_PLAN.md 交付物结构校验。

验收标准(来自 docs/tickets/ticket-002-reuse-audit.md):
- [ ] docs/audits/REUSE_PLAN.md 存在,含上述 4 问的明确答案
- [ ] 有结论表(复用/改造/自研),并据此更新 specs 中第一版范围
- [ ] 引用的仓库/文档有 URL,关键结论有代码/文档佐证
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REUSE_PLAN = REPO / "docs" / "audits" / "REUSE_PLAN.md"
SPEC_001 = REPO / "docs" / "specs" / "SPEC-001-mvp-translation-engine.md"

# 四个必答问题,REUSE_PLAN.md 中必须各有一段落
REQUIRED_QUESTIONS = [
    "捕获",  # Q1: 如何捕获文本/渲染中文 + DFHack API
    "classic",  # Q2: classic 版支持?字符集/字体方案
    "复用",  # Q3: 可直接复用哪些组件
    "结论",  # Q4: 结论表
]

# 必须被调研的仓库/项目,文档中需出现其 URL 或仓库路径
REQUIRED_PROJECTS = [
    "dfi18n",  # DFI18n 本体(大小写不敏感)
    "dfi18n-data-zh-hans",  # DFI18n Data - Simplified Chinese
    "dwarf-fortress-chinese",  # wodzys/dwarf-fortress-chinese
]

# DFHack 官方能力(ticket-002 第三项调研对象)
REQUIRED_DFHACK_APIS = [
    "eventful",
    "overlay",
    "paintString",  # Lua screen API
    "dfhack-run",
]


@pytest.fixture(scope="module")
def plan_text() -> str:
    if not REUSE_PLAN.exists():
        pytest.fail(f"交付物缺失: {REUSE_PLAN.relative_to(REPO)} (ticket-002 未产出)")
    return REUSE_PLAN.read_text(encoding="utf-8")


def test_plan_exists():
    assert REUSE_PLAN.exists(), "docs/audits/REUSE_PLAN.md 必须存在"


def test_four_questions_answered(plan_text):
    missing = [q for q in REQUIRED_QUESTIONS if q not in plan_text]
    assert not missing, f"REUSE_PLAN.md 缺少必答问题段落: {missing}"


def test_projects_researched(plan_text):
    missing = [p for p in REQUIRED_PROJECTS if p.lower() not in plan_text.lower()]
    assert not missing, f"REUSE_PLAN.md 未覆盖调研对象: {missing}"


def test_dfhack_apis_covered(plan_text):
    missing = [a for a in REQUIRED_DFHACK_APIS if a.lower() not in plan_text.lower()]
    assert not missing, f"REUSE_PLAN.md 未覆盖 DFHack 官方能力: {missing}"


def test_urls_present(plan_text):
    assert "http" in plan_text, "REUSE_PLAN.md 必须引用仓库/文档 URL"
    # 至少 3 个独立 URL(DFI18n、数据仓库、dfzh)
    import re

    urls = set(re.findall(r"https?://[^\s)>\"'，。、]+", plan_text))
    assert len(urls) >= 3, f"REUSE_PLAN.md 引用 URL 过少: {len(urls)} 个"


def test_conclusion_table_with_verdicts(plan_text):
    # 结论表必须覆盖 复用/改造/自研 三档判断
    for verdict in ("复用", "改造", "自研"):
        assert verdict in plan_text, f"结论表缺少判断档位: {verdict}"


def test_spec_scope_updated():
    """验收标准第二条: 结论须回写 specs 第一版范围。"""
    assert SPEC_001.exists(), "SPEC-001 缺失"
    text = SPEC_001.read_text(encoding="utf-8")
    assert "ticket-002" in text, "SPEC-001 未记录 ticket-002 复用结论"
