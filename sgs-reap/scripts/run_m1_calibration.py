"""闸门 M1 标定运行器（会消耗 API 额度）。

启动 proxy（独立日志）→ 跑 reap-fork/tests/Calibrate.lean（20 个目标）→ 汇总：
候选利用率、拒绝原因分布、非平凡率、每类调用的延迟分位与 token。

用法：
    C:\\Users\\gaosen\\anaconda3\\python.exe scripts/run_m1_calibration.py
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODELS = REPO / "sgsr" / "models"
FORK = REPO / "reap-fork"
EXPERIMENTS = REPO / "experiments"
sys.path.insert(0, str(REPO))
from sgsr.models import config  # noqa: E402
from sgsr.utils.service import dump_log, parse_port  # noqa: E402

PORT = 8770
RESULTS = FORK / "calibrate_results.jsonl"
WALL_CLOCK = FORK / "calibrate_wall_clock.jsonl"
PROXY_LOG = MODELS / "m1_proxy_log.jsonl"


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def health_ok(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as resp:
                if json.loads(resp.read().decode("utf-8")).get("status") == "ok":
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    global PORT
    PORT = parse_port("M1 标定：测候选利用率/延迟/token（消耗 API 额度）", PORT)
    config.ensure_utf8_stdout()
    EXPERIMENTS.mkdir(exist_ok=True)
    for stale in (RESULTS, WALL_CLOCK, PROXY_LOG):
        if stale.exists():
            stale.unlink()

    env = dict(os.environ)
    env["PROXY_LOG_PATH"] = str(PROXY_LOG)
    log_file = open(MODELS / "proxy_calibration_stdout.log", "w", encoding="utf-8")
    proxy = subprocess.Popen(
        [sys.executable, "proxy.py", "--port", str(PORT)],
        cwd=MODELS,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    started = time.time()
    try:
        if not health_ok():
            print("[m1] proxy 未就绪（真代理需要网络权限与 sgsr/models/.env 里的 API key）")
            log_file.flush()
            dump_log(MODELS / "proxy_calibration_stdout.log", label="m1")
            return 1
        print(f"[m1] proxy ready on 127.0.0.1:{PORT}")
        proc = subprocess.run(
            ["lake", "env", "lean", "tests/Calibrate.lean"],
            cwd=FORK,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print(f"[m1] lean exit={proc.returncode}  用时 {time.time() - started:.0f}s")
        if proc.returncode != 0:
            print((proc.stdout or "") + (proc.stderr or ""))
            return 1
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
        log_file.close()

    records = [json.loads(line) for line in RESULTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    total = sum(r.get("total", 0) for r in records)
    accepted = sum(r.get("accepted", 0) for r in records)
    non_trivial = sum(r.get("non_trivial", 0) for r in records)
    reasons: dict[str, int] = {}
    for record in records:
        for rejection in record.get("rejections", []):
            reasons[rejection["reason"]] = reasons.get(rejection["reason"], 0) + 1

    print(f"\n[m1] ===== Lean 侧 =====")
    print(f"[m1] 目标数 {len(records)}  候选 {total}  接受 {accepted}  利用率 {accepted / max(total, 1):.1%}")
    print(f"[m1] 非平凡候选 {non_trivial}  非平凡率 {non_trivial / max(total, 1):.1%}")
    print(f"[m1] 拒绝原因分布 {reasons}")
    for record in records:
        reviews = record.get("reviews", [])
        print(
            f"      {record['case']:26s} 候选={record.get('total')} 接受={record.get('accepted')} "
            f"非平凡={record.get('non_trivial')} reviews={reviews}"
        )

    proxy_records = (
        [json.loads(line) for line in PROXY_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
        if PROXY_LOG.exists()
        else []
    )
    per_endpoint: dict[str, dict] = {}
    for endpoint in ("conjecture", "guide"):
        subset = [r for r in proxy_records if r.get("endpoint") == endpoint]
        latencies = [float(r.get("latency_ms", 0.0) or 0.0) for r in subset]
        prompt_tokens = sum(int((r.get("usage") or {}).get("prompt_tokens", 0) or 0) for r in subset)
        completion_tokens = sum(int((r.get("usage") or {}).get("completion_tokens", 0) or 0) for r in subset)
        per_endpoint[endpoint] = {
            "calls": len(subset),
            "latency_p50_ms": round(percentile(latencies, 0.5), 1),
            "latency_p95_ms": round(percentile(latencies, 0.95), 1),
            "latency_mean_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "errors": sum(1 for r in subset if r.get("error")),
        }
    total_calls = sum(v["calls"] for v in per_endpoint.values())
    total_prompt = sum(v["prompt_tokens"] for v in per_endpoint.values())
    total_completion = sum(v["completion_tokens"] for v in per_endpoint.values())
    print(f"\n[m1] ===== 服务侧 =====")
    for endpoint, stats in per_endpoint.items():
        print(f"[m1] {endpoint:10s} {json.dumps(stats, ensure_ascii=False)}")
    print(
        f"[m1] 合计 {total_calls} 次调用  prompt={total_prompt}  completion={total_completion}  "
        f"平均每次目标 {(total_prompt + total_completion) / max(len(records), 1):.0f} token"
    )

    wall_clock: dict[str, int] = {}
    if WALL_CLOCK.exists():
        names = [json.loads(line)["name"] for line in WALL_CLOCK.read_text(encoding="utf-8").splitlines() if line.strip()]
        wall_clock = {name: names.count(name) for name in set(names)}
        print(f"[m1] wall-clock: {wall_clock}")

    summary = {
        "goals": len(records),
        "candidates": total,
        "accepted": accepted,
        "utilization": accepted / max(total, 1),
        "non_trivial": non_trivial,
        "non_trivial_rate": non_trivial / max(total, 1),
        "rejection_reasons": reasons,
        "MODELS": per_endpoint,
        "totals": {
            "calls": total_calls,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
        },
        "wall_clock": wall_clock,
        "per_case": records,
    }
    (EXPERIMENTS / "m1_calibration.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[m1] 结果写入 {EXPERIMENTS / 'm1_calibration.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
