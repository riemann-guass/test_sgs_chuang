"""离线打通「statement → k 篇候选证明 → Lean 验证 → 轨迹落盘」（不联网、不花一分钱）。

用法：

    C:\\Users\\gaosen\\anaconda3\\python.exe tests\\run_solve_mock.py

链路：

    data/workload.jsonl  ──▶  mock_server 的 POST /solve （确定性"解题器"）
                         ──▶  一次批处理交给 `lake exe sgslean-server` 做 check + verify
                         ──▶  experiments/runs/<ts>/traces.jsonl（每条目标一行，带逐条判定码）
                         ──▶  experiments/results/solve_smoke.json（汇总，入库）

关键断言（对应反向对照，见 docs/phase5-log.md）。**期望值写在测试里**，
不从假服务的答案表推导——否则把假服务改坏反而不会报错（第一版就是这么漏的）：

* `EXPECT_NONZERO` 里的目标：solve_rate 恰为 1/k（一条正确证明 + k-1 条诱饵）；
* `EXPECT_ZERO` 里的目标（`0 + n = n`、`P → Q`）：solve_rate 恰为 0；
* 每条目标的 `check` 必须通过 —— 否则说明数据文件里的语句本身有问题。
"""

from __future__ import annotations

import json
import os
import socket
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
SERVICE = ROOT / "service"
DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
RUNS = ROOT / "experiments" / "runs"
LAKE = os.environ.get("LAKE", "lake")
SAMPLES_PER_STATEMENT = int(os.environ.get("SOLVE_SAMPLES", "3"))

# 与假服务实现无关的显式期望：w01–w08 的正确答案在假服务的表里，w09/w10 故意不可解
EXPECT_NONZERO = {f"w{i:02d}" for i in range(1, 9)}
EXPECT_ZERO = {"w09", "w10"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_post(url: str, payload: dict, timeout: float = 60.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_health(url: str, timeout: float = 30.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if json.loads(resp.read().decode("utf-8")).get("status") == "ok":
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪：{url}")


def run_lean_server(requests: list[dict], timeout: float = 3600.0) -> tuple[dict[str, dict], int, str]:
    """一次批处理跑完全部请求；返回（按 id 索引的响应, frontend 毫秒, stderr）。"""
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
    out = proc.stdout.decode("utf-8", errors="replace").splitlines()
    responses: dict[str, dict] = {}
    frontend_ms = 0
    for line in out:
        item = json.loads(line)  # 协议纯度：每一行都必须是 JSON，否则这里直接抛
        rid = str(item.get("id"))
        if rid == "__flush__":
            frontend_ms = (item.get("result") or {}).get("frontend_ms", 0)
        responses[rid] = item
    return responses, frontend_ms, proc.stderr.decode("utf-8", errors="replace")


def main() -> int:
    work = [
        json.loads(line)
        for line in (DATA / "workload.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    port = free_port()
    mock_log = (run_dir / "mock_server.stderr.log").open("w", encoding="utf-8")
    mock = subprocess.Popen(
        [sys.executable, str(SERVICE / "mock_server.py"), "--port", str(port)],
        cwd=str(SERVICE),
        stdout=subprocess.DEVNULL,
        stderr=mock_log,
    )
    failures: list[str] = []
    started = time.perf_counter()
    traces: list[dict] = []
    try:
        wait_health(f"http://127.0.0.1:{port}/health")
        base = f"http://127.0.0.1:{port}"

        requests: list[dict] = []
        for item in work:
            resp = http_post(
                f"{base}/solve",
                {"statement": item["statement"], "num_samples": SAMPLES_PER_STATEMENT},
            )
            proofs = resp.get("proofs", [])
            requests.append({"id": f"check:{item['id']}", "cmd": "check", "stmt": item["statement"]})
            for proof in proofs:
                requests.append(
                    {
                        "id": f"verify:{item['id']}:{proof['index']}",
                        "cmd": "verify",
                        "stmt": item["statement"],
                        "proof": proof["proof"],
                    }
                )
            traces.append(
                {
                    "workload_id": item["id"],
                    "domain": item.get("domain"),
                    "statement": item["statement"],
                    "num_samples": SAMPLES_PER_STATEMENT,
                    "proofs": [
                        {"index": p["index"], "proof": p["proof"]} for p in proofs
                    ],
                    "mock_meta": resp.get("meta"),
                    "verdicts": [],
                }
            )

        responses, frontend_ms, stderr_text = run_lean_server(requests)

        # 汇总：把 verify 判定填回轨迹，算每条目标的 solve_rate
        by_id = {t["workload_id"]: t for t in traces}
        per_statement: list[dict] = []
        for item in work:
            check = responses.get(f"check:{item['id']}")
            if check is None or (check.get("result") or {}).get("ok") is not True:
                failures.append(f"{item['id']}: 语句没过门检/缺响应：{check}")
            trace = by_id[item["id"]]
            verified = 0
            for proof in trace["proofs"]:
                key = f"verify:{item['id']}:{proof['index']}"
                response = responses.get(key) or {}
                result = response.get("result") or {}
                ok = result.get("ok") is True
                verified += 1 if ok else 0
                trace["verdicts"].append(
                    {"index": proof["index"], "ok": ok, "reason": result.get("reason"),
                     "protocol_ok": response.get("ok") is True}
                )
            total = len(trace["proofs"])
            rate = (verified / total) if total else 0.0
            per_statement.append(
                {"id": item["id"], "statement": item["statement"], "proofs": total,
                 "verified": verified, "solve_rate": round(rate, 4)}
            )
            if item["id"] in EXPECT_NONZERO:
                expected = (1 / total) if total else 0.0
            else:
                expected = 0.0
            if abs(rate - expected) > 1e-9:
                failures.append(
                    f"{item['id']}: solve_rate 应为 {expected:.4f}（共 {total} 篇候选），"
                    f"实际 {rate:.4f}"
                )
        # 分布形状：既要有人解得出，也要有人解不出（否则指标没有区分度）
        if not any(row["solve_rate"] > 0 for row in per_statement):
            failures.append("所有目标的 solve_rate 都是 0：生成/验证链路可能整条断掉")
        if not any(row["solve_rate"] == 0.0 for row in per_statement):
            failures.append("所有目标的 solve_rate 都非 0：不可解目标未被如实判负")
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()
        mock_log.close()

    elapsed = time.perf_counter() - started
    traces_path = run_dir / "traces.jsonl"
    with traces_path.open("w", encoding="utf-8") as handle:
        for trace in traces:
            handle.write(json.dumps(trace, ensure_ascii=False) + "\n")

    rates = [row["solve_rate"] for row in per_statement]
    report = {
        "run_dir": str(run_dir.relative_to(ROOT)),
        "elapsed_s": round(elapsed, 1),
        "frontend_ms": frontend_ms,
        "samples_per_statement": SAMPLES_PER_STATEMENT,
        "statements": len(per_statement),
        "total_proofs": sum(row["proofs"] for row in per_statement),
        "total_verified": sum(row["verified"] for row in per_statement),
        "solve_rate_mean": round(sum(rates) / len(rates), 4) if rates else None,
        "solve_rate_min": min(rates) if rates else None,
        "solve_rate_max": max(rates) if rates else None,
        "zero_solve_rate_targets": [row["id"] for row in per_statement if row["solve_rate"] == 0.0],
        "per_statement": per_statement,
        "failures": failures,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "solve_smoke.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "run.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[solve-mock] {report['statements']} 条目标 × {SAMPLES_PER_STATEMENT} 篇 = "
          f"{report['total_proofs']} 篇候选，验证通过 {report['total_verified']} 篇，"
          f"{report['elapsed_s']}s（frontend {frontend_ms}ms）")
    print(f"[solve-mock] solve_rate: mean={report['solve_rate_mean']} "
          f"min={report['solve_rate_min']} max={report['solve_rate_max']} "
          f"零解目标={report['zero_solve_rate_targets']}")
    print(f"[solve-mock] 轨迹：{traces_path}")
    if failures:
        print(f"[solve-mock] FAIL，{len(failures)} 处：")
        for failure in failures:
            print("  -", failure)
        return 1
    print(f"[solve-mock] PASS（汇总写入 {RESULTS / 'solve_smoke.json'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
