"""术语词典(ticket-004): database/ 内置 seed 词典, 精确匹配直返。

工程书 §11: exact = terminology.lookup(normalized) → 命中直接返回;
§34: 词典 ≈0.1 复杂度档位, 命中即高置信(confidence=1.0)。

数据来源: database/seed_terms.yaml(CC BY-NC 4.0, 矮人要塞中文维基翻译组,
见 docs/audits/REUSE_PLAN.md §6 —— 文件内必须保留来源与许可署名)。
SQLite 持久层(terminology 表)由 ticket-006 落地。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from df_fanyi.config import REPO_ROOT

logger = logging.getLogger("df_fanyi.terminology")

DEFAULT_SEED_PATH = REPO_ROOT / "database" / "seed_terms.yaml"


@dataclass(frozen=True)
class Term:
    source: str
    target: str
    locked: bool = False


class Terminology:
    """内存术语表: 整句精确匹配(大小写不敏感) + 短语替换(规则引擎用)。"""

    def __init__(self, terms: Iterable[Term] | None = None) -> None:
        # key: source.lower() → 词条列表(lookup 时 locked 优先)
        self._exact: dict[str, list[Term]] = {}
        self._phrases: list[tuple[re.Pattern[str], str]] = []
        for term in terms or []:
            self.add(term)

    @classmethod
    def load_seed(cls, path: str | Path | None = None) -> "Terminology":
        """从 seed YAML 加载: [{'source': ..., 'target': ..., 'locked': bool}]。"""
        seed_path = Path(path) if path else DEFAULT_SEED_PATH
        if not seed_path.exists():
            raise FileNotFoundError(f"seed 术语词典不存在: {seed_path}")
        data = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
        terms = data.get("terms", [])
        if not isinstance(terms, list):
            raise ValueError(f"seed 词典 terms 必须是列表: {seed_path}")
        return cls.from_terms(terms)

    @classmethod
    def from_terms(cls, raw_terms: Iterable[dict[str, Any]]) -> "Terminology":
        t = cls()
        for item in raw_terms:
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            if not source or not target:
                continue
            t.add(Term(source=source, target=target, locked=bool(item.get("locked", False))))
        return t

    def add(self, term: Term) -> None:
        key = term.source.strip().lower()
        if not key:
            return
        self._exact.setdefault(key, []).append(term)
        # 短语替换表: 多词优先(长词条先匹配), 整词边界, 大小写不敏感
        pattern = re.compile(r"(?<![A-Za-z])" + re.escape(key) + r"(?![A-Za-z])", re.IGNORECASE)
        self._phrases.append((pattern, term.target))
        self._phrases.sort(key=lambda p: len(p[0].pattern), reverse=True)

    def lookup(self, text: str) -> Term | None:
        """整句精确匹配(§11 terminology.lookup)。大小写/首尾空白不敏感。"""
        key = " ".join((text or "").split()).lower()
        if not key:
            return None
        candidates = self._exact.get(key)
        if not candidates:
            return None
        for term in candidates:
            if term.locked:  # locked=true 优先(工单要求)
                return term
        return candidates[0]

    def replace_phrases(self, text: str) -> str:
        """短语级替换(规则引擎翻译 job/profession 用): 长词条优先, 不碰中文。"""
        out = text
        for pattern, target in self._phrases:
            out = pattern.sub(target, out)
        return out

    def __len__(self) -> int:
        return len(self._exact)
