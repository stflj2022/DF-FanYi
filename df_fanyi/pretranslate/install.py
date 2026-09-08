"""预翻译包安装器(ticket-011): 进度文件 → L2 translation_memory + terminology。

工单约定:
- 译文批量写入 L2 translation_memory(provider="cloud-pretranslate",
  confidence=验证得分) —— 引擎查询链不变(缓存→L2 TM 已天然覆盖);
- 名称类词条(kind="name")以 locked=true 写入 terminology(运行时名称翻译
  零 LLM); 描述类只进 TM;
- 幂等: tm_upsert/term_add 都是 upsert 语义, 重复安装不产生重复行;
- §46 privacy: store_source_text=false 的进度包没有原文, 名称词条无法
  安装进 terminology(term_add 需要非空 source), 跳过并计数 —— TM 行仍可装。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from df_fanyi.database.store import SQLiteStore
from df_fanyi.pretranslate.batch import PROVIDER_NAME, PROGRESS_VERSION

logger = logging.getLogger("df_fanyi.pretranslate")


@dataclass
class InstallStats:
    """安装结果统计(CLI 打印/验收)。"""

    tm_rows: int = 0  # 写入/更新 TM 行数
    terms_locked: int = 0  # 锁定名称词条数
    skipped_no_source: int = 0  # 隐私模式缺原文而跳过的名称词条
    dropped_records: int = 0  # 进度包中记录的不合格句数(仅报告)

    def summary(self) -> str:
        return (
            f"TM 行 {self.tm_rows} | 锁定名称词条 {self.terms_locked} | "
            f"跳过(缺原文) {self.skipped_no_source} | 包内不合格记录 {self.dropped_records}"
        )


def load_progress_file(path: str | Path) -> dict:
    """读取进度/翻译包文件(独立于 BatchTranslator 的纯读取)。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != PROGRESS_VERSION:
        raise ValueError(f"翻译包格式不识别或版本不符: {path}")
    return data


def install_from_progress(progress: dict, store: SQLiteStore) -> InstallStats:
    """把翻译包装入 L2 TM + 术语层(幂等)。返回统计。"""
    stats = InstallStats()
    translations = progress.get("translations", {})
    stats.dropped_records = len(progress.get("dropped", {}))

    for digest, entry in translations.items():
        translation = (entry.get("translation") or "").strip()
        if not translation:
            continue
        score = float(entry.get("score") or 0.0)
        source = (entry.get("source") or "").strip()
        store.tm_upsert(
            digest,
            source,
            translation,
            model=entry.get("model"),
            provider=PROVIDER_NAME,
            quality_score=score,
            confidence=score,
        )
        stats.tm_rows += 1

        if entry.get("kind") == "name":
            if not source:
                stats.skipped_no_source += 1  # §46: 包内无原文, 名称词条装不了
                continue
            store.term_add(
                source,
                translation,
                category=(entry.get("tag") or "name").lower(),
                source_type=PROVIDER_NAME,
                locked=True,  # 工单: 名称类词条 locked=true, 运行时零 LLM
            )
            stats.terms_locked += 1

    logger.info(
        "预翻译包安装完成: %s", stats.summary()
    )
    return stats
