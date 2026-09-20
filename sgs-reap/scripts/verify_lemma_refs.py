"""用 `sgslean-server` 逐条验证 `data/lemmas_g1.jsonl` 里的**参考证明**。

规则（P1.3 的硬要求）：**参考证明必须自己先过 Lean 验证，才有资格当"标准答案"**。
没过的条目会被剔除并记录原因，绝不会留在 G1 引理集里当"其实可证"的证据。

用法：

    python scripts/verify_lemma_refs.py                # 只报告，不改文件
    python scripts/verify_lemma_refs.py --write        # 把未通过的条目剔除后写回
    python scripts/verify_lemma_refs.py --chunk 20     # 每批 20 条（Mathlib 模式批越小越安全）

输出：`experiments/results/lemma_refs_check.json`（逐条判定 + 耗时统计）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SGSLEAN = ROOT / "sgslean"
DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
LAKE = os.environ.get("LAKE", "lake")


def run_batch(requests: list[dict], timeout: float = 3600.0) -> tuple[dict[str, dict], int, str]:
    lines = [json.dumps(r, ensure_ascii=False) for r in requests]
    lines.append(json.dumps({"id": "__flush__", "cmd": "flush"}))
    payload = "\n".join(lines) + "\n"
    proc = subprocess.run(
        [LAKE, "exe", "sgslean-server"],
        cwd=str(SGSLEAN),
        input=payload.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    responses: dict[str, dict] = {}
    frontend_ms = 0
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        item = json.loads(line)  # 协议纯度：不是 JSON 就直接抛
        rid = str(item.get("id"))
        if rid == "__flush__":
            frontend_ms = (item.get("result") or {}).get("frontend_ms", 0)
        responses[rid] = item
    return responses, frontend_ms, proc.stderr.decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 G1 引理集的参考证明")
    parser.add_argument("--write", action="store_true", help="剔除未通过条目后写回 jsonl")
    parser.add_argument("--chunk", type=int, default=25, help="每批请求数（Mathlib 模式建议 ≤25）")
    parser.add_argument("--file", default=str(DATA / "lemmas_g1.jsonl"))
    args = parser.parse_args()

    path = Path(args.file)
    lemmas = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    os.environ.setdefault("SGSLEAN_IMPORTS", "Mathlib")
    started = time.perf_counter()
    checked: list[dict] = []
    for start in range(0, len(lemmas), args.chunk):
        chunk = lemmas[start : start + args.chunk]
        requests = [
            {"id": f"gate:{lemma['id']}", "cmd": "check", "stmt": lemma["statement"]} for lemma in chunk
        ] + [
            {
                "id": f"verify:{lemma['id']}",
                "cmd": "verify",
                "stmt": lemma["statement"],
                "proof": lemma["reference_proof"],
            }
            for lemma in chunk
        ]
        responses, frontend_ms, _ = run_batch(requests)
        for lemma in chunk:
            gate = responses.get(f"gate:{lemma['id']}") or {}
            verify = responses.get(f"verify:{lemma['id']}") or {}
            gate_result = gate.get("result") or {}
            verify_result = verify.get("result") or {}
            checked.append(
                {
                    "id": lemma["id"],
                    "domain": lemma.get("domain"),
                    "statement": lemma["statement"],
                    "reference_proof": lemma["reference_proof"],
                    "gate_ok": gate_result.get("ok") is True,
                    "gate_reason": gate_result.get("reason"),
                    "verified": verify_result.get("ok") is True,
                    "reason": verify_result.get("reason"),
                    "detail": str(verify_result.get("detail"))[:400],
                }
            )
        print(
            f"[lemma-refs] 批 {start // args.chunk + 1}: {len(chunk)} 条判定完"
            f"（本批 frontend {frontend_ms}ms，累计 {time.perf_counter() - started:.0f}s）"
        )

    passed = [row for row in checked if row["verified"]]
    failed = [row for row in checked if not row["verified"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "imports": os.environ.get("SGSLEAN_IMPORTS"),
        "total": len(checked),
        "passed": len(passed),
        "failed": len(failed),
        "elapsed_s": round(time.perf_counter() - started, 1),
        "failed_rows": [{k: row[k] for k in ("id", "statement", "reason", "detail")} for row in failed],
        "rows": checked,
    }
    (RESULTS / "lemma_refs_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[lemma-refs] 通过 {len(passed)}/{len(checked)}，耗时 {report['elapsed_s']}s")
    for row in failed:
        print(f"  - 未通过 {row['id']} ({row['reason']}): {row['statement']}")

    if args.write:
        keep = {row["id"] for row in passed}
        with path.open("w", encoding="utf-8") as handle:
            for lemma in lemmas:
                if lemma["id"] in keep:
                    handle.write(json.dumps(lemma, ensure_ascii=False) + "\n")
        print(f"[lemma-refs] 已写回 {path}：保留 {len(keep)} 条")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
