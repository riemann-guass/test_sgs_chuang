"""闸门 G1：Solver 在初等引理上的 solve_rate 分布（P1.3）。

G1 问的是整个项目最要命的一件事：**Solver 能不能解出一部分题**。
如果 solve_rate 几乎全是 0，SG-Lean 的"可证性"信号没有区分度，环就是空的。

用法：

    # ① 干跑（不联网、不花钱）：用引理自带的参考证明当"模型输出"，验证整条管线与统计
    set SGSLEAN_IMPORTS=none
    python tests/run_gate_g1.py --dry-run --limit 12

    # ② 真跑：起 service\\proxy.py 之后指向它
    python tests/run_gate_g1.py --endpoint http://127.0.0.1:8770/solve

参数（也读同名环境变量）：

    --k           每条引理取几篇候选证明（默认 3，env `G1_K`）
    --limit       只用前 N 条引理（默认全部 63 条，env `G1_LIMIT`）
    --chunk       每批交给 sgslean-server 的请求数（默认 30，env `G1_CHUNK`）
    --endpoint    `/solve` 的 URL（默认 `G1_ENDPOINT`，缺省用假服务 8765）
    --dry-run     不请求 HTTP，直接用 data/lemmas_g1.jsonl 里的参考证明 + 诱饵

判定（**跑之前就定死**，避免事后解释）：

* 非零解目标比例 < 20% → **G1 不过**，停机问（预案：换模型 / 退到 Nat 域 / 整篇生成降级为骨架 + 搜索）；
* 非零解目标比例 ≥ 20% 但平均 solve_rate > 0.9 → 记 **too_easy 警告**（区分度不足，题库太浅）；
* 其余 → **G1 通过**。

`--dry-run` 额外断言：每条引理的参考证明必须被判通过（否则 exit 1）——这条不变量同时
也是反向对照的抓手（改坏统计或批处理，它立刻失败）。
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SGSLEAN = ROOT / "sgslean"
DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
RUNS = ROOT / "experiments" / "runs"
LAKE = os.environ.get("LAKE", "lake")
DEFAULT_ENDPOINT = os.environ.get("G1_ENDPOINT", "http://127.0.0.1:8765/solve")

# 与假服务同款的两条诱饵（判负），保证 dry-run 也有非平凡失败面
DECOYS = ["exact ?_", "sorry"]


def http_post(url: str, payload: dict, timeout: float = 180.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def solve_http(endpoint: str, statement: str, k: int) -> tuple[list[str], dict]:
    payload = http_post(endpoint, {"statement": statement, "num_samples": k})
    proofs = [p["proof"] for p in payload.get("proofs", [])]
    return proofs, payload.get("meta", {})


def solve_dry(lemma: dict, k: int) -> tuple[list[str], dict]:
    """干跑：把参考证明摆在第一位，再用诱饵凑够 k 篇。"""
    return ([lemma["reference_proof"], *DECOYS][:k], {"backend": "dry-run", "latency_ms": 0})


def run_lean_batch(requests: list[dict], timeout: float = 3600.0) -> tuple[dict[str, dict], int]:
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
    responses: dict[str, dict] = {}
    frontend_ms = 0
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        item = json.loads(line)  # 协议纯度
        rid = str(item.get("id"))
        if rid == "__flush__":
            frontend_ms = (item.get("result") or {}).get("frontend_ms", 0)
        responses[rid] = item
    return responses, frontend_ms


def main() -> int:
    parser = argparse.ArgumentParser(description="闸门 G1：solve_rate 分布")
    parser.add_argument("--k", type=int, default=int(os.environ.get("G1_K", "3")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("G1_LIMIT", "0")))
    parser.add_argument("--chunk", type=int, default=int(os.environ.get("G1_CHUNK", "30")))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--imports", default=None, help="覆盖 SGSLEAN_IMPORTS")
    parser.add_argument("--file", default=str(DATA / "lemmas_g1.jsonl"),
                        help="引理集文件（默认 G1 初等引理集；miniF2F 用 data/miniF2F_*.jsonl）")
    args = parser.parse_args()

    if args.imports is not None:
        os.environ["SGSLEAN_IMPORTS"] = args.imports
    mode = "dry-run" if args.dry_run else f"http:{args.endpoint}"

    lemmas = [
        json.loads(line)
        for line in Path(args.file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        lemmas = lemmas[: args.limit]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / f"g1_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    # ① 取候选
    per_lemma: list[dict] = []
    for lemma in lemmas:
        t0 = time.perf_counter()
        if args.dry_run:
            proofs, meta = solve_dry(lemma, args.k)
        else:
            proofs, meta = solve_http(args.endpoint, lemma["statement"], args.k)
        per_lemma.append(
            {
                "id": lemma["id"],
                "domain": lemma.get("domain"),
                "statement": lemma["statement"],
                # miniF2F 一类没有参考证明的数据集也要能跑（真跑模式不用参考证明）
                "reference_proof": lemma.get("reference_proof", ""),
                "proofs": proofs,
                "solve_meta": meta,
                "solve_latency_s": round(time.perf_counter() - t0, 2),
                "verdicts": [],
            }
        )
    phase1_s = time.perf_counter() - started

    # ② 批处理验证（Mathlib 模式必须批量：一次导入成本几十秒到几分钟）
    frontend_total_ms = 0
    batches = 0
    pending: list[dict] = []
    for row in per_lemma:
        for idx, proof in enumerate(row["proofs"]):
            pending.append(
                {
                    "id": f"v:{row['id']}:{idx}",
                    "cmd": "verify",
                    "stmt": row["statement"],
                    "proof": proof,
                }
            )
    responses: dict[str, dict] = {}
    for start in range(0, len(pending), args.chunk):
        chunk = pending[start : start + args.chunk]
        chunk_responses, frontend_ms = run_lean_batch(chunk)
        responses.update(chunk_responses)
        frontend_total_ms += frontend_ms
        batches += 1
        print(
            f"[g1] 验证批 {batches}: {len(chunk)} 条（frontend {frontend_ms}ms，"
            f"累计 {time.perf_counter() - started:.0f}s）"
        )
    verify_s = time.perf_counter() - started - phase1_s

    # ③ 统计
    failures: list[str] = []
    reason_hist: collections.Counter = collections.Counter()
    for row in per_lemma:
        verified = 0
        for idx, _proof in enumerate(row["proofs"]):
            response = responses.get(f"v:{row['id']}:{idx}") or {}
            result = response.get("result") or {}
            ok = result.get("ok") is True
            verified += 1 if ok else 0
            if not ok:
                reason_hist[result.get("reason") or "missing"] += 1
            row["verdicts"].append(
                {"index": idx, "ok": ok, "reason": result.get("reason"),
                 "protocol_ok": response.get("ok") is True}
            )
        row["verified"] = verified
        row["num_samples"] = len(row["proofs"])
        row["solve_rate"] = round(verified / len(row["proofs"]), 4) if row["proofs"] else 0.0
        # 干跑不变量（整数比较，别用浮点）：候选是 [参考证明, 诱饵...]，
        # 所以必须**恰好**只有参考证明通过 —— 少一个说明参考证明或验证链路坏了，
        # 多一个说明判定读错了（把协议层的 ok 当成了判定结果）。
        if args.dry_run and row["verified"] != 1:
            failures.append(
                f"{row['id']}: 干跑应当恰好 1 篇通过（参考证明），实际 {row['verified']} 篇；"
                f"verdicts={[(v['ok'], v['reason']) for v in row['verdicts']]}"
            )

    rates = [row["solve_rate"] for row in per_lemma]
    nonzero = [row for row in per_lemma if row["solve_rate"] > 0]
    fully = [row for row in per_lemma if row["solve_rate"] == 1.0]
    nonzero_ratio = len(nonzero) / len(per_lemma) if per_lemma else 0.0
    mean_rate = sum(rates) / len(rates) if rates else 0.0
    histogram = collections.Counter(
        "0" if r == 0 else "1" if r == 1 else "部分" for r in rates
    )
    verdict = "pass" if nonzero_ratio >= 0.2 else "fail_no_signal"
    if verdict == "pass" and mean_rate > 0.9:
        verdict = "pass_but_too_easy"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "imports": os.environ.get("SGSLEAN_IMPORTS", "(server default)"),
        "k": args.k,
        "lemmas": len(per_lemma),
        "total_candidates": sum(row["num_samples"] for row in per_lemma),
        "total_verified": sum(row["verified"] for row in per_lemma),
        "nonzero_ratio": round(nonzero_ratio, 4),
        "mean_solve_rate": round(mean_rate, 4),
        "min_solve_rate": min(rates) if rates else None,
        "max_solve_rate": max(rates) if rates else None,
        "fully_solved": [row["id"] for row in fully],
        "zero_solved": [row["id"] for row in per_lemma if row["solve_rate"] == 0],
        "histogram": dict(histogram),
        "failure_reasons": dict(reason_hist),
        "timing": {
            "solve_phase_s": round(phase1_s, 1),
            "verify_phase_s": round(verify_s, 1),
            "frontend_total_ms": frontend_total_ms,
            "lean_batches": batches,
            "total_s": round(time.perf_counter() - started, 1),
        },
        "verdict": verdict,
        "criterion": "nonzero_ratio >= 0.2 视为通过；mean>0.9 记 too_easy 警告",
        "failures": failures,
        "per_lemma": [
            {k: row[k] for k in
             ("id", "domain", "statement", "num_samples", "verified", "solve_rate",
              "solve_latency_s", "verdicts", "proofs", "reference_proof")}
            for row in per_lemma
        ],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    # 报告名带 mode/规模后缀，避免小规模重跑覆盖正式报告（phase15 踩过这个坑）
    suffix = f"{'dryrun' if args.dry_run else 'real'}_k{args.k}_n{len(per_lemma)}"
    report_path = RESULTS / f"g1_solver_capability_{suffix}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # 只有"正式配置"（真跑 + 全量）才写规范名
    if not args.dry_run and args.limit == 0:
        (RESULTS / "g1_solver_capability.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    with (run_dir / "traces.jsonl").open("w", encoding="utf-8") as handle:
        for row in per_lemma:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"[g1] mode={mode} imports={report['imports']} k={args.k} 引理 {len(per_lemma)} 条 / "
        f"候选 {report['total_candidates']} 篇 / 通过 {report['total_verified']} 篇"
    )
    print(
        f"[g1] solve_rate: mean={report['mean_solve_rate']} "
        f"min={report['min_solve_rate']} max={report['max_solve_rate']} "
        f"非零解占比={report['nonzero_ratio']}"
    )
    print(f"[g1] 分布直方图={report['histogram']} 失败原因={report['failure_reasons']}")
    print(f"[g1] 判定：{verdict}")
    print(f"[g1] 轨迹：{run_dir / 'traces.jsonl'}；报告：{report_path}")
    if failures:
        print(f"[g1] FAIL，{len(failures)} 处：")
        for failure in failures:
            print("  -", failure)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
