#!/usr/bin/env python3
"""真实样本验收基准(ticket-010, 工程书 §54 + §34 阈值实测)。

对 collect_acceptance_samples.py 采集的真实游戏文本逐句:
1. 复杂度评分(§34 启发式);
2. 快路径 fast_translate(L1→L2→词典→规则): 命中层 + 延迟, 绝不调 LLM;
3. 快路径未命中的前 N 条(--llm-limit, --max-chars 内)走真 ollama gemma:
   记录延迟、验证结果(§24) —— LLM 层验证通过率的实测依据;
4. 阈值分析: 快路径可解句的最大复杂度 vs 需 LLM 句的最小复杂度,
   对照 config queue.async_threshold(0.25) 给出实测调优依据(§34: 不写死理论值)。

输出(到 --out-dir, 默认 data/samples/, gitignored):
- bench_results.json   逐句明细 + 聚合
- bench_summary.md     报告用 markdown 片段

用法: python3 scripts/acceptance_bench.py [--samples PATH] [--llm-limit N]
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from df_fanyi.config import load_config  # noqa: E402
from df_fanyi.core.queue.heuristic import complexity_score  # noqa: E402
from df_fanyi.pipeline import build_orchestrator  # noqa: E402

DEFAULT_SAMPLES = REPO / "data" / "samples" / "acceptance_samples.jsonl"
DEFAULT_OUT_DIR = REPO / "data" / "samples"


def pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, round(p / 100 * (len(vals) - 1))))
    return vals[idx]


def ollama_alive(host: str) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(host.rstrip("/") + "/api/tags", timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--llm-limit", type=int, default=12,
                    help="走真 ollama 的未命中句上限(控制耗时)")
    ap.add_argument("--max-chars", type=int, default=300,
                    help="LLM 实测句的长度上限(更长句属异步队列职责, 见 §29)")
    ap.add_argument("--llm-timeout", type=float, default=120.0)
    args = ap.parse_args()

    rows = [json.loads(line) for line in args.samples.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        print(f"错误: 样本为空: {args.samples}", file=sys.stderr)
        return 1

    cfg = load_config()  # 真实配置(含用户覆盖); L2 指向临时库, 不污染 data/engine.db
    tmpdir = tempfile.mkdtemp(prefix="fanyi-bench-")
    cfg.cache["l2_path"] = str(Path(tmpdir) / "bench.db")
    llm_base = str(cfg.local_llm.get("base_url", "http://127.0.0.1:11434"))
    llm_alive = ollama_alive(llm_base)
    orch = build_orchestrator(cfg)

    bench_rows: list[dict] = []
    llm_used = 0
    try:
        for row in rows:
            text = row["text"]
            score = complexity_score(text)
            t0 = time.perf_counter()
            fast = orch.fast_translate(text)
            fast_ms = (time.perf_counter() - t0) * 1000
            entry = {
                **row,
                "complexity": score,
                "chars": len(text),
                "fast_hit": fast is not None,
                "layer": fast.model if fast is not None else "",
                "fast_ms": round(fast_ms, 2),
            }
            if fast is None and llm_alive and llm_used < args.llm_limit and len(text) <= args.max_chars:
                llm_used += 1
                t0 = time.perf_counter()
                result = orch.translate(text)
                llm_ms = (time.perf_counter() - t0) * 1000
                entry.update(
                    llm_called=True,
                    llm_ms=round(llm_ms, 2),
                    llm_pass=result.error is None and result.confidence > 0,
                    llm_confidence=result.confidence,
                    llm_error=result.error,
                    translated=result.text if result.error is None else "",
                )
            bench_rows.append(entry)
            tag = entry.get("layer") or ("llm" if entry.get("llm_called") else "miss")
            print(f"[{len(bench_rows):3d}/{len(rows)}] {row['id']:18s} score={score:.2f} "
                  f"{tag:10s} {entry['fast_ms']:7.2f}ms"
                  + (f" llm={entry['llm_ms']:.0f}ms pass={entry['llm_pass']}"
                     if entry.get("llm_called") else ""))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 聚合 ------------------------------------------------------------
    fast_rows = [r for r in bench_rows if r["fast_hit"]]
    miss_rows = [r for r in bench_rows if not r["fast_hit"]]
    llm_rows = [r for r in bench_rows if r.get("llm_called")]
    layer_counts: dict[str, int] = {}
    for r in fast_rows:
        layer_counts[r["layer"]] = layer_counts.get(r["layer"], 0) + 1
    layer_counts["llm_measured"] = len(llm_rows)
    layer_counts["llm_not_measured(async_queue)"] = len(miss_rows) - len(llm_rows)

    fast_ms_all = [r["fast_ms"] for r in bench_rows]
    llm_ms = [r["llm_ms"] for r in llm_rows]
    llm_pass_rows = [r for r in llm_rows if r["llm_pass"]]

    fast_scores = [r["complexity"] for r in fast_rows]
    miss_scores = [r["complexity"] for r in miss_rows]
    summary = {
        "n_samples": len(bench_rows),
        "categories": {
            c: sum(1 for r in bench_rows if r["category"] == c)
            for c in {r["category"] for r in bench_rows}
        },
        "llm_provider_alive": llm_alive,
        "layer_counts": layer_counts,
        "fast_hit_rate": round(len(fast_rows) / len(bench_rows), 3),
        "fast_ms": {"p50": round(pctl(fast_ms_all, 50), 2), "p95": round(pctl(fast_ms_all, 95), 2),
                    "max": round(max(fast_ms_all, default=0), 2)},
        "llm": {
            "measured": len(llm_rows),
            "pass": len(llm_pass_rows),
            "pass_rate": round(len(llm_pass_rows) / len(llm_rows), 3) if llm_rows else None,
            "ms": {"p50": round(pctl(llm_ms, 50), 0), "p95": round(pctl(llm_ms, 95), 0),
                   "max": round(max(llm_ms, default=0), 0)} if llm_ms else None,
        },
        "threshold_analysis": {
            "max_score_fast_solvable": max(fast_scores, default=None),
            "min_score_needs_llm": min(miss_scores, default=None),
            "current_async_threshold": 0.25,
            "note": "async_threshold 只影响调度预判; 快路径未命中一律入队(§34/§29)",
        },
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_json = args.out_dir / "bench_results.json"
    out_md = args.out_dir / "bench_summary.md"
    out_json.write_text(json.dumps({"summary": summary, "rows": bench_rows},
                                   ensure_ascii=False, indent=1), encoding="utf-8")

    def table_md() -> str:
        lines = [
            "| 样本 | 类别 | 字符 | 复杂度 | 命中层 | 快路径ms | LLM ms | 验证 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in bench_rows:
            lines.append(
                f"| {r['id']} | {r['category']} | {r['chars']} | {r['complexity']:.2f} "
                f"| {r['layer'] or ('LLM' if r.get('llm_called') else '—(异步队列)')} "
                f"| {r['fast_ms']:.2f} "
                f"| {('%.0f' % r['llm_ms']) if r.get('llm_called') else '—'} "
                f"| {'✓' if r.get('llm_pass') else ('✗ ' + (r.get('llm_error') or '')[:30]) if r.get('llm_called') else '—'} |"
            )
        return "\n".join(lines)

    llm_p50 = f"{pctl(llm_ms, 50):.0f}" if llm_ms else "—"
    llm_p95 = f"{pctl(llm_ms, 95):.0f}" if llm_ms else "—"
    llm_rate = f"{(summary['llm']['pass_rate'] or 0):.0%}"
    out_md.write_text(
        f"""# 真实样本基准摘要(ticket-010 自动生成)

- 样本: {summary['n_samples']} 句 {summary['categories']}(来源: 本机 Dwarf Fortress 53.16 真实文本)
- ollama 可用: {llm_alive}  |  快路径命中率: {summary['fast_hit_rate']:.1%}
- 命中层分布: {json.dumps(layer_counts, ensure_ascii=False)}
- 快路径延迟 ms: p50={summary['fast_ms']['p50']} p95={summary['fast_ms']['p95']} max={summary['fast_ms']['max']}
- LLM 实测({len(llm_rows)} 句): 验证通过 {len(llm_pass_rows)}/{len(llm_rows)}({llm_rate}),
  延迟 ms p50={llm_p50} p95={llm_p95}
- 阈值分析: 快路径可解句最大复杂度={summary['threshold_analysis']['max_score_fast_solvable']},
  需 LLM 句最小复杂度={summary['threshold_analysis']['min_score_needs_llm']}, 现行 async_threshold=0.25

{table_md()}
""",
        encoding="utf-8",
    )
    print(f"\n明细 → {out_json}\n摘要 → {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
