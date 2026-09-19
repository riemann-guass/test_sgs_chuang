"""闸门 G2：重复子目标是否常见、能否与死路伪影区分（P2 / N1）。

G2 决定 N1 成立与否：如果"反复出现的子目标状态"很罕见，N1（需求驱动的条件化）就没有原料，
主线应降级为只做 N2 + N3。

用法：

    # 干跑（不联网）：用引理自带的参考证明当候选，逐条 `trace` 出子目标流
    python tests\\run_gate_g2.py --dry-run --limit 38

    # 真跑：先起 service\\proxy.py，再用 /solve 拿候选（要花钱）
    python tests\\run_gate_g2.py --endpoint http://127.0.0.1:8770/solve --k 3

判定准则（**跑之前定死**）：

* `bucket == demand`（跨 ≥ `--min-targets` 个不同目标出现，且至少一次出现在成功轨迹里）的签名
  **≥ 3 条** → G2 通过，N1 成立；
* 否则 → G2 不过，N1 降级为 N2 + N3 主线；报告里给出"局部需求 / 伪影"构成的诊断。

干跑不变量：参考证明都是验证通过的，所以每条轨迹必须 `verified=true`——统计或轨迹链路一旦坏掉，
这条断言立刻失败（同时也是反向对照的抓手）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graph.demand import mine  # noqa: E402
from graph.schema import load_jsonl, trace_from_job, validate_trace  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SGSLEAN = ROOT / "sgslean"
DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
RUNS = ROOT / "experiments" / "runs"
LAKE = os.environ.get("LAKE", "lake")
DECOYS = ["exact ?_"]  # 真跑时给每条目标补一条诱饵，制造失败轨迹


def self_test() -> int:
    """不依赖 Lean 的过滤器自测：手工造三条轨迹，检查分桶是否符合定义。

    为什么需要它：G2 的"死路伪影"是 **MCTS 搜索**的产物，而当前整篇生成的流水线里
    "失败"表现为"整篇不过"，中间步骤照样是正常的子目标。所以伪影这一支在真跑里
    基本不会自然出现（见 docs/phase9-log.md），必须用手工样例把这条逻辑钉住。
    """
    traces = [
        # 目标 t1：成功轨迹，两步，第二步产生 sig X
        {"id": "t1", "verified": True, "steps": [
            {"signature": "A", "goalsLeft": 1}, {"signature": "X", "goalsLeft": 1}]},
        # 目标 t2：成功轨迹，也产生 sig X → X 应升为 demand（跨 2 个目标 + 出现在成功轨迹）
        {"id": "t2", "verified": True, "steps": [
            {"signature": "B", "goalsLeft": 1}, {"signature": "X", "goalsLeft": 2}]},
        # 目标 t3：失败轨迹，产生只有它自己出现过的 sig Z（失败-only、单目标）→ artifact
        {"id": "t3", "verified": False, "steps": [
            {"signature": "C", "goalsLeft": 1}, {"signature": "Z", "goalsLeft": 1}]},
    ]
    report = mine(traces, min_targets=2)
    bucket = {entry["sig"]: entry["bucket"] for entry in report["entries"]}
    # A/B 有成功证据但只出现 1 个目标 → local；C/Z 只出现在失败轨迹里 → artifact；
    # X 跨 2 个目标且有成功证据 → demand
    expected = {"A": "local", "B": "local", "C": "artifact", "X": "demand", "Z": "artifact"}
    print(f"[g2-self-test] 分桶={bucket}")
    if bucket != expected:
        print(f"[g2-self-test] FAIL：期望 {expected}")
        return 1
    print("[g2-self-test] PASS（demand / artifact / local 三分支都被覆盖）")
    return 0


def http_post(url: str, payload: dict, timeout: float = 300.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
        item = json.loads(line)
        rid = str(item.get("id"))
        if rid == "__flush__":
            frontend_ms = (item.get("result") or {}).get("frontend_ms", 0)
        responses[rid] = item
    return responses, frontend_ms


def main() -> int:
    parser = argparse.ArgumentParser(description="闸门 G2：重复子目标与伪影过滤")
    parser.add_argument("--self-test", action="store_true", help="只跑过滤器自测，不碰 Lean")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--endpoint", default=os.environ.get("G2_ENDPOINT", "http://127.0.0.1:8770/solve"))
    parser.add_argument("--k", type=int, default=int(os.environ.get("G2_K", "3")))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--chunk", type=int, default=15)
    parser.add_argument("--min-targets", type=int, default=2)
    parser.add_argument("--file", default=str(DATA / "lemmas_g1.jsonl"))
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    lemmas = load_jsonl(args.file)
    if args.limit:
        lemmas = lemmas[: args.limit]

    # ① 准备候选：干跑用参考证明；真跑用 /solve
    jobs: list[dict] = []
    for lemma in lemmas:
        if args.dry_run:
            proofs = [lemma["reference_proof"]]
        else:
            payload = http_post(
                args.endpoint,
                {"statement": lemma["statement"], "num_samples": max(1, args.k - len(DECOYS))},
            )
            proofs = [p["proof"] for p in payload.get("proofs", [])] + DECOYS
        for idx, proof in enumerate(proofs):
            jobs.append(
                {
                    "id": f"{lemma['id']}#{idx}",
                    "target": lemma["id"],
                    "domain": lemma.get("domain"),
                    "statement": lemma["statement"],
                    "proof": proof,
                }
            )

    # ② 逐条 trace（每批摊薄一次 Mathlib 导入）
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / f"g2_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    frontend_total_ms = 0
    traces: list[dict] = []
    for start in range(0, len(jobs), args.chunk):
        chunk = jobs[start : start + args.chunk]
        requests = [
            {"id": job["id"], "cmd": "trace", "stmt": job["statement"], "proof": job["proof"]}
            for job in chunk
        ]
        responses, frontend_ms = run_lean_batch(requests)
        frontend_total_ms += frontend_ms
        for job in chunk:
            response = responses.get(job["id"]) or {}
            result = response.get("result") or {}
            if response.get("ok") is not True:
                traces.append(
                    {
                        "id": job["target"],
                        "domain": job.get("domain"),
                        "statement": job["statement"],
                        "proof": job["proof"],
                        "verified": False,
                        "reason": "protocol_error",
                        "steps": [],
                    }
                )
                continue
            traces.append(trace_from_job(job | {"id": job["target"]}, result))
        print(
            f"[g2] trace 批 {start // args.chunk + 1}: {len(chunk)} 条（frontend {frontend_ms}ms，"
            f"累计 {time.perf_counter() - started:.0f}s）"
        )

    dirty = [problem for trace in traces for problem in validate_trace(trace)]
    report_mine = mine(traces, min_targets=args.min_targets)

    failures: list[str] = []
    if args.dry_run:
        bad = [t["id"] for t in traces if not t["verified"]]
        if bad:
            failures.append(f"干跑里参考证明必须全部 verified，实际未通过：{bad}")
    if dirty:
        failures.append(f"轨迹校验不通过 {len(dirty)} 处，前 3 条：{dirty[:3]}")
    verdict = "pass" if report_mine["demand_count"] >= 3 else "fail_no_signal"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run" if args.dry_run else f"http:{args.endpoint}",
        "imports": os.environ.get("SGSLEAN_IMPORTS", "(server default)"),
        "lemmas": len(lemmas),
        "traces": len(traces),
        "timing": {
            "frontend_total_ms": frontend_total_ms,
            "total_s": round(time.perf_counter() - started, 1),
        },
        "mining": {k: v for k, v in report_mine.items() if k != "entries"},
        "demand_entries": [e for e in report_mine["entries"] if e["bucket"] == "demand"],
        "artifact_entries": [e for e in report_mine["entries"] if e["bucket"] == "artifact"][:20],
        "verdict": verdict,
        "criterion": "bucket=demand 的签名数 ≥ 3 视为通过（跨 ≥ min_targets 个目标且至少一次出现在成功轨迹里）",
        "failures": failures,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "g2_demand.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (run_dir / "traces.jsonl").open("w", encoding="utf-8") as handle:
        for trace in traces:
            handle.write(json.dumps(trace, ensure_ascii=False) + "\n")

    print(
        f"[g2] mode={report['mode']} 引理 {len(lemmas)} / 轨迹 {len(traces)} / "
        f"签名 {report_mine['signatures']} / 分桶 {report_mine['buckets']}"
    )
    print(f"[g2] 需求签名 {report_mine['demand_count']} 条（门槛 ≥3）；判定：{verdict}")
    for entry in report["demand_entries"][:8]:
        print(
            f"    {entry['sig'][:60]:<60} freq={entry['freq_total']} "
            f"(成功{entry['freq_success']}/失败{entry['freq_failure']}) "
            f"目标={entry['num_targets']} score={entry['score']}"
        )
    print(f"[g2] 轨迹：{run_dir / 'traces.jsonl'}；报告：{RESULTS / 'g2_demand.json'}")
    if failures:
        print(f"[g2] FAIL，{len(failures)} 处：")
        for failure in failures:
            print("  -", failure)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
