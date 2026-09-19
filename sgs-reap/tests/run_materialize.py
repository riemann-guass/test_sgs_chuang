"""把已验证引理物化成有名常量，并**编译生成的 Lean 文件**（P3 收尾）。

流程（每一步都有真值依据，不靠信任）：

1. 从 `data/lemmas_g1.jsonl` 取前 N 条；
2. 逐条交给 `verify` —— **只有服务端判过 `ok=true` 的条目才允许物化**；
3. 调 `materialize` 写出 `sgslean/generated/Library.lean`；
4. 用 `lake env lean` **编译这个文件**：能编译才是"这批引理真的成立"的证据。

    python tests\\run_materialize.py --limit 3
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SGSLEAN = ROOT / "sgslean"
DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
OUT = SGSLEAN / "generated" / "Library.lean"
LAKE = os.environ.get("LAKE", "lake")


def run_server(requests: list[dict], timeout: float = 1800.0) -> dict[str, dict]:
    lines = [json.dumps(r, ensure_ascii=False) for r in requests]
    lines.append(json.dumps({"id": "__flush__", "cmd": "flush"}))
    proc = subprocess.run(
        [LAKE, "exe", "sgslean-server"],
        cwd=str(SGSLEAN),
        input=("\n".join(lines) + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    out: dict[str, dict] = {}
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        item = json.loads(line)
        out[str(item.get("id"))] = item
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="物化 + 编译校验")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--file", default=str(DATA / "lemmas_g1.jsonl"))
    args = parser.parse_args()

    lemmas = [
        json.loads(line)
        for line in Path(args.file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][: args.limit]

    # ① 逐条验证（只有通过的才允许物化）
    checks = run_server(
        [
            {"id": f"v:{lemma['id']}", "cmd": "verify",
             "stmt": lemma["statement"], "proof": lemma["reference_proof"]}
            for lemma in lemmas
        ]
    )
    entries, rejected = [], []
    for lemma in lemmas:
        result = (checks.get(f"v:{lemma['id']}") or {}).get("result") or {}
        if result.get("ok") is True:
            entries.append(
                {"stmt": lemma["statement"], "proof": lemma["reference_proof"],
                 "verified": True, "source": f"g1:{lemma['id']}"}
            )
        else:
            rejected.append({"id": lemma["id"], "reason": result.get("reason")})
    if rejected:
        print(f"[materialize] 拒绝未通过验证的条目：{rejected}")
    if not entries:
        print("[materialize] 没有可物化的条目")
        return 1

    # ② 物化
    response = run_server(
        [{"id": "m", "cmd": "materialize", "path": str(OUT), "entries": entries}]
    )
    result = (response.get("m") or {}).get("result") or {}
    if not result:
        print(f"[materialize] 物化失败，原始响应：{(response.get('m') or {})}")
        return 1
    print(f"[materialize] 写入 {result.get('written')} 条，路径 {result.get('path')}")

    # ③ 编译生成的 Lean 文件（这一条才是"真的成立"的证据）
    started = time.perf_counter()
    proc = subprocess.run(
        [LAKE, "env", "lean", str(OUT)],
        cwd=str(SGSLEAN),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=3600.0,
    )
    elapsed = time.perf_counter() - started
    compile_ok = proc.returncode == 0

    report = {
        "limit": args.limit,
        "verified_entries": len(entries),
        "rejected": rejected,
        "output": str(OUT.relative_to(ROOT)),
        "names": result.get("names", []),
        "compile_ok": compile_ok,
        "compile_seconds": round(elapsed, 1),
        "compile_head": proc.stdout.decode("utf-8", errors="replace")[:400],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "materialize_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[materialize] 编译生成文件：{'OK' if compile_ok else 'FAIL'}（{elapsed:.1f}s）")
    if not compile_ok:
        print(report["compile_head"])
        return 1
    print(f"[materialize] PASS；报告：{RESULTS / 'materialize_check.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
