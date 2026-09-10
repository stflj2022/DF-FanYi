#!/usr/bin/env python3
"""translate-exe-strings.py — exe 句串批量云端翻译(router/L2), 断点续跑

输入: extract-exe-strings.py 的 JSONL({"key": ...})
输出: JSONL({"key","zh"}), 增量写 checkpoint, 重跑自动跳过已译

质量规则:
- 系统提示要求保留方括号标记([C:7:0:1]/[B]/[VAR:...])与 %s/%d 原样
- 校验: 每批全部 id 有非空译文 + 标记 token 多重集一致 + %s/%d 计数一致
- 失败重试 2 次后降级单条翻译; 单条也失败则跳过并在 stderr 记录
用法:
  python3 scripts/translate-exe-strings.py \
    --in dfint-data/exe-missing-20260910.jsonl \
    --out dfint-data/exe-translated-20260910.jsonl
"""
import argparse
import json
import re
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROUTER = "http://127.0.0.1:8010/v1/chat/completions"
MODEL = "router/L2"
BATCH = 15
WORKERS = 6
MAX_TOKENS = 8000

MARKUP_RE = re.compile(r"\[[^\[\]]{1,48}\]")
FMT_RE = re.compile(r"%[sdf]")

SYSTEM = (
    "你是《矮人要塞》(Dwarf Fortress)游戏的文本译者。把用户 JSON 对象里的每个英文游戏文本"
    "翻译成简体中文，返回一个 JSON 对象：键与输入完全相同，值是译文。规则：\n"
    "1) 方括号标记(如 [C:7:0:1]、[B]、[C:VAR:MEETING:ACTOR]、[VAR:NATIVENAME:MEETING:ACTOR])"
    "原样保留、位置贴合语义，只翻译标记外的文字；\n"
    "2) %s %d 等格式占位符原样保留；\n"
    "3) 游戏术语用社区通用译名(矮人/要塞/政务厅/酒馆等)，专有名词可意译；\n"
    "4) 界面提示要简短自然；句子碎片按上下文语气译；\n"
    "5) 只输出 JSON 对象本身，不要 markdown 代码块，不要解释。"
)

_lock = threading.Lock()
_stats = {"batches": 0, "ok": 0, "retry": 0, "single_fail": 0}


def chat(batch: dict[str, str]) -> dict[str, str]:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "disabled"},
    }
    req = urllib.request.Request(
        ROUTER, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"]["content"] or ""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", content).strip()
    obj = json.loads(content)
    if not isinstance(obj, dict):
        raise ValueError("response is not a JSON object")
    return obj


def validate(src: dict[str, str], dst: dict[str, str]) -> str | None:
    """返回错误原因, None=通过"""
    for k, v in src.items():
        t = dst.get(k)
        if not isinstance(t, str) or not t.strip():
            return f"missing/empty id={k}"
        if sorted(MARKUP_RE.findall(v)) != sorted(MARKUP_RE.findall(t)):
            return f"markup mismatch id={k}"
        if Counter(FMT_RE.findall(v)) != Counter(FMT_RE.findall(t)):
            return f"format-token mismatch id={k}"
    return None


def translate_keys(keys: list[str]) -> tuple[list[dict], list[str]]:
    """批量→重试→单条降级。返回 (结果dict列表, 失败key列表)"""
    batch = {str(i + 1): k for i, k in enumerate(keys)}
    for attempt in range(3):  # 2 次批量重试
        try:
            obj = chat(batch)
            err = validate(batch, obj)
            if err is None:
                with _lock:
                    _stats["batches"] += 1
                    _stats["ok"] += 1
                return [{"key": k, "zh": obj[i].strip()} for i, k in batch.items()], []
            reason = err
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
        with _lock:
            _stats["retry"] += 1
        if attempt == 1:  # 第三次改为单条
            break
        time.sleep(1.5 * (attempt + 1))
    # 单条降级
    results, failed = [], []
    for k in keys:
        try:
            obj = chat({"1": k})
            if validate({"1": k}, obj) is None:
                results.append({"key": k, "zh": obj["1"].strip()})
            else:
                failed.append(k)
        except Exception as e:  # noqa: BLE001
            print(f"[single-fail] {k[:50]!r}: {e}", file=sys.stderr)
            failed.append(k)
    with _lock:
        _stats["batches"] += len(keys)
        _stats["single_fail"] += len(failed)
    return results, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args()

    todo = [json.loads(l)["key"] for l in Path(args.inp).read_text(encoding="utf-8").splitlines() if l.strip()]
    done: dict[str, str] = {}
    outp = Path(args.out)
    if outp.exists():
        for l in outp.read_text(encoding="utf-8").splitlines():
            if l.strip():
                r = json.loads(l)
                done[r["key"]] = r["zh"]
    todo = [k for k in todo if k not in done]
    print(f"待译 {len(todo)} (已跳过 {len(done)})")

    t0 = time.time()
    with outp.open("a", encoding="utf-8") as fout, ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(translate_keys, todo[i : i + BATCH]) for i in range(0, len(todo), BATCH)]
        n = 0
        for fut in as_completed(futures):
            results, failed = fut.result()
            with _lock:
                for r in results:
                    if r["key"] not in done:
                        done[r["key"]] = r["zh"]
                        fout.write(json.dumps(r, ensure_ascii=False) + "\n")
                fout.flush()
                n += len(results) + len(failed)
                elapsed = time.time() - t0
                print(
                    f"[{n}/{len(todo)}] ok={len(done)} fail={_stats['single_fail']} "
                    f"{elapsed:.0f}s ({n / max(elapsed, 1):.1f}/s)",
                    file=sys.stderr,
                )
    print(f"完成: 共 {len(done)} 条译文 → {outp}; 单条失败 {_stats['single_fail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
