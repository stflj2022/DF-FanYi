"""云端批量翻译器(ticket-011): JSON 数组进出 + 断点续跑 + 退避暂停 + 逐句验证。

工程书依据:
- §12/§17-18: 云端走 Pi Model Router(OpenAI-compatible), 绝不绕过 router 直连上游;
- ADR-cloud-first-translation: 大量翻译内容优先云端(批量预翻是极端形态);
- §24(验证): 每句过 Validator, 不合格丢弃并记录, 绝不进 TM 污染运行时;
- §19(提示词版本化): 系统提示 = translation_system_v1 + pretranslate_batch_v1;
- §46(privacy): privacy.store_source_text=false 时进度文件只存 hash 不存原文。

断点续跑: 进度落 data/pretranslate_progress.json(已译 hash → 译文, 已丢 hash →
原因), 重跑只补缺; 该文件即「离线翻译包」, install 阶段装入 L2/术语层。

节流与退避: 请求间隔 ≥ request_interval_s; router 不可达/503/额度耗尽/
输出不可解析 → 指数退避重试同一批, 重试耗尽 → 停止本轮(进度已保存,
下轮续跑), 绝不绕过或并发轰炸 router。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from df_fanyi.core.validator import Validator
from df_fanyi.prompts import load_prompt, load_system_prompt
from df_fanyi.providers.router_client import RouterChatClient, RouterError

logger = logging.getLogger("df_fanyi.pretranslate")

PROGRESS_VERSION = 1
PROVIDER_NAME = "cloud-pretranslate"

_EMBEDDED_BATCH_PROMPT = """批量模式补充规则(在上述翻译规则基础上生效):
- 用户消息是一个 JSON 字符串数组, 每个元素是独立的英文游戏文本片段。
- 把每个元素分别翻译为简体中文。
- 只输出一个 JSON 字符串数组: 顺序与输入一一对应, 元素数量与输入相同。
- 不要输出任何解释、注释、编号或 Markdown 代码围栏。
- 某个元素无法翻译时, 对应位置原样输出该元素。
"""


def batch_system_prompt() -> str:
    """系统提示 = v1 十二条 + 批量输出格式约束(双双版本化, §19)。"""
    return (
        load_system_prompt("v1")
        + "\n\n"
        + load_prompt("pretranslate_batch_v1", fallback=_EMBEDDED_BATCH_PROMPT)
    )


def parse_batch_content(content: str, expected: int) -> list[str]:
    """解析批量译文: 剥 ```json 围栏 → 提取 JSON 数组 → 校验类型与对齐。

    容错约定(工单): reasoning 字段在客户端已丢弃; content 可能带围栏/前后杂文,
    取第一个 '[' 到最后一个 ']' 之间的 JSON。解析失败/非字符串数组/数量不齐
    → ValueError(由调用方按可重试失败处理)。
    """
    if not content:
        raise ValueError("空内容")
    text = content.strip()
    if text.startswith("```"):
        # 剥第一行 ```json / ``` 围栏与结尾 ```
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("未找到 JSON 数组")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("translations")  # 容错: {"translations": [...]}
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError("批量输出不是字符串数组")
    if len(data) != expected:
        raise ValueError(f"数量不齐: 期望 {expected}, 实际 {len(data)}")
    return data


@dataclass
class RunStats:
    """一轮 run 的可观测统计(CLI 打印 / 报告用)。"""

    total: int = 0  # 本轮目标句数(过滤+limit 后)
    translated: int = 0
    dropped: int = 0
    skipped_done: int = 0  # 进度中已有, 跳过
    batches: int = 0
    requests: int = 0
    retries: int = 0
    failed_batches: int = 0
    ok: bool = True
    error: str | None = None
    backoff_sleeps_s: list[float] = field(default_factory=list)
    request_latencies_ms: list[int] = field(default_factory=list)

    def summary(self) -> str:
        lat = ""
        if self.request_latencies_ms:
            xs = sorted(self.request_latencies_ms)
            p50 = xs[len(xs) // 2]
            p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
            lat = f" 请求延迟 p50={p50}ms p95={p95}ms"
        msg = f"error={self.error} " if self.error else ""
        return (
            f"目标 {self.total} | 已译 {self.translated} | 丢弃 {self.dropped} | "
            f"跳过(进度已有) {self.skipped_done} | 批次 {self.batches} "
            f"(请求 {self.requests}, 重试 {self.retries}, 失败批 {self.failed_batches})"
            f"{lat} {msg}".rstrip()
        )


class BatchTranslator:
    """批量云端翻译: 进度文件即离线翻译包, 断点续跑, 永不并发轰炸 router。"""

    def __init__(
        self,
        client: RouterChatClient,
        *,
        progress_path: str | Path,
        batch_size: int = 12,
        request_interval_s: float = 2.0,
        max_retries: int = 5,
        backoff_initial_s: float = 2.0,
        backoff_max_s: float = 120.0,
        max_tokens: int = 4096,
        sleep_fn: Callable[[float], None] = time.sleep,
        store_source_text: bool = True,
        validator: Validator | None = None,
    ) -> None:
        self._client = client
        self._progress_path = Path(progress_path)
        self._batch_size = max(1, int(batch_size))
        self._interval = float(request_interval_s)
        self._max_retries = int(max_retries)
        self._backoff_initial = float(backoff_initial_s)
        self._backoff_max = float(backoff_max_s)
        # 批量输出 10-20 句 + 可能的思维链(模型自适应, ticket-010 gemma 同款教训):
        # 2048 会被 thinking 吃完导致正文空; 批量模式默认 4096。
        self._max_tokens = int(max_tokens)
        self._sleep = sleep_fn
        self._store_source_text = bool(store_source_text)
        self._validator = validator or Validator()
        self._requests_made = 0  # 本实例累计(节流依据: 非首个请求必须间隔)

    # ---- 进度文件(= 离线翻译包) -------------------------------------------

    def load_progress(self) -> dict:
        if self._progress_path.exists():
            try:
                data = json.loads(self._progress_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") == PROGRESS_VERSION:
                    return data
                logger.warning("进度文件版本不识别, 忽略: %s", self._progress_path)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("进度文件不可读, 从零开始: %s (%s)", self._progress_path, exc)
        return {
            "version": PROGRESS_VERSION,
            "provider": PROVIDER_NAME,
            "model": self._client.model,
            "store_source_text": self._store_source_text,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "translations": {},
            "dropped": {},
            "stats": {"requests": 0, "retries": 0, "latencies_ms": []},
        }

    def save_progress(self, progress: dict) -> None:
        """原子落盘(断点续跑的崩溃安全: 临时文件 + os.replace)。"""
        progress["updated_at"] = int(time.time())
        progress["store_source_text"] = self._store_source_text
        progress["model"] = progress.get("model") or self._client.model
        self._progress_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._progress_path.with_suffix(self._progress_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(progress, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self._progress_path)

    def status(self) -> dict:
        """status 子命令数据: 已译/已弃/元信息(不扫描 raws, 瞬时返回)。"""
        progress = self.load_progress()
        return {
            "exists": self._progress_path.exists(),
            "progress_path": str(self._progress_path),
            "model": progress.get("model"),
            "provider": progress.get("provider", PROVIDER_NAME),
            "translated": len(progress.get("translations", {})),
            "dropped": len(progress.get("dropped", {})),
            "requests": int(progress.get("stats", {}).get("requests", 0)),
            "updated_at": progress.get("updated_at"),
        }

    # ---- 批量运行 ------------------------------------------------------------

    def run(self, segments: Sequence, *, limit: int | None = None) -> RunStats:
        """翻译未完成片段: 过滤进度 → limit 截断 → 分批 → 请求/退避/验证/落盘。

        每批成功即原子落盘进度; 重试耗尽 → 停止本轮(返回 ok=False, error),
        已完成部分已在进度中, 下轮 run 直接续跑。
        """
        stats = RunStats()
        progress = self.load_progress()
        translations: dict = progress.setdefault("translations", {})
        dropped: dict = progress.setdefault("dropped", {})
        pstats = progress.setdefault("stats", {"requests": 0, "retries": 0, "latencies_ms": []})

        todo = [
            s for s in segments if s.hash not in translations and s.hash not in dropped
        ]
        stats.skipped_done = len(segments) - len(todo)
        if limit is not None:
            todo = todo[: max(0, int(limit))]
        stats.total = len(todo)

        for batch in _chunks(todo, self._batch_size):
            stats.batches += 1
            if not self._run_batch(batch, progress, translations, dropped, stats, pstats):
                # 重试耗尽: 保存进度并停止本轮(续跑留给下一轮)
                pstats["requests"] = pstats.get("requests", 0) + stats.requests
                pstats["retries"] = pstats.get("retries", 0) + stats.retries
                self.save_progress(progress)
                stats.ok = False
                stats.error = (
                    f"批次重试 {self._max_retries} 次后仍失败, 已停止本轮; "
                    f"进度已保存, 可重跑续传"
                )
                return stats
            self.save_progress(progress)

        pstats["requests"] = pstats.get("requests", 0) + stats.requests
        pstats["retries"] = pstats.get("retries", 0) + stats.retries
        self.save_progress(progress)
        return stats

    def _run_batch(
        self,
        batch: list,
        progress: dict,
        translations: dict,
        dropped: dict,
        stats: RunStats,
        pstats: dict,
    ) -> bool:
        """跑一个批次: 请求 → 解析 → 逐句验证 → 写进度。成功 True; 重试耗尽 False。"""
        sentences = [s.text for s in batch]
        payload_text = json.dumps(sentences, ensure_ascii=False)
        system = batch_system_prompt()
        attempt = 0
        while True:
            if self._requests_made > 0 and attempt == 0:
                # 节流: 批次间的首请求与上一请求间隔 ≥ interval;
                # 批内重试已有指数退避间隔, 不叠加(避免双倍空等)。
                self._sleep(self._interval)
            try:
                started = time.perf_counter()
                result = self._client.chat(
                    payload_text, system=system, max_tokens=self._max_tokens
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                self._requests_made += 1
                stats.requests += 1
                stats.request_latencies_ms.append(latency_ms)
                pstats.setdefault("latencies_ms", []).append(latency_ms)
                outputs = parse_batch_content(result.content, len(batch))
            except (RouterError, ValueError) as exc:
                attempt += 1
                stats.retries += 1
                if attempt > self._max_retries:
                    stats.failed_batches += 1
                    logger.error("批次失败(重试耗尽): %s", exc)
                    return False
                delay = min(
                    self._backoff_initial * (2 ** (attempt - 1)), self._backoff_max
                )
                logger.warning(
                    "批次失败(%s), %.1fs 后指数退避重试(%d/%d)",
                    exc, delay, attempt, self._max_retries,
                )
                self._sleep(delay)
                stats.backoff_sleeps_s.append(delay)
                continue

            model = result.model or self._client.model
            kept = dropped_n = 0
            for seg, trans in zip(batch, outputs):
                trans = (trans or "").strip()
                validation = self._validator.validate(seg.text, trans)
                if validation.valid and trans:
                    translations[seg.hash] = {
                        **({"source": seg.text} if self._store_source_text else {}),
                        "translation": trans,
                        "score": round(validation.score, 3),
                        "kind": seg.kind,
                        "tag": seg.tag,
                        "model": model,
                    }
                    kept += 1
                else:
                    reason = "; ".join(validation.errors) or "空译文"
                    dropped[seg.hash] = {
                        **({"source": seg.text} if self._store_source_text else {}),
                        "reason": reason,
                    }
                    dropped_n += 1
                    logger.info("不合格丢弃(%s): %s… ← %s", seg.hash[:8], seg.text[:30], reason)
            stats.translated += kept
            stats.dropped += dropped_n
            logger.info("批次完成: %d 句, 保留 %d, 丢弃 %d", len(batch), kept, dropped_n)
            return True


def _chunks(items: Sequence, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])
