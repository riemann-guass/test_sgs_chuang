"""真实模型端到端测试的运行器（会消耗 API 额度）。

启动 service/proxy.py -> 跑 reap-fork/tests/ConjectureRealE2E.lean -> 汇总 JSONL 结果。

用法：
    C:\\Users\\gaosen\\anaconda3\\python.exe tests/run_real_e2e.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
FORK = REPO / "reap-fork"
EXPERIMENTS = REPO / "experiments"
sys.path.insert(0, str(SERVICE))
import config  # noqa: E402

PORT = 8770
RESULTS = FORK / "real_e2e_results.jsonl"
WALL_CLOCK = FORK / "real_e2e_wall_clock.jsonl"


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
    config.ensure_utf8_stdout()
    EXPERIMENTS.mkdir(exist_ok=True)
    for stale in (RESULTS, WALL_CLOCK):
        if stale.exists():
            stale.unlink()

    log_file = open(SERVICE / "proxy_real_e2e_stdout.log", "w", encoding="utf-8")
    proxy = subprocess.Popen(
        [sys.executable, "proxy.py", "--port", str(PORT)],
        cwd=SERVICE,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if not health_ok():
            print("[real-e2e] proxy 未就绪")
            log_file.flush()
            print((SERVICE / "proxy_real_e2e_stdout.log").read_text(encoding="utf-8")[-1500:])
            return 1
        print(f"[real-e2e] proxy ready on 127.0.0.1:{PORT}")

        proc = subprocess.run(
            ["lake", "env", "lean", "tests/ConjectureRealE2E.lean"],
            cwd=FORK,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print(f"[real-e2e] lean exit={proc.returncode}")
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
        print("[real-e2e] proxy stopped")

    records = [json.loads(line) for line in RESULTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = {"records": records}
    for record in records:
        if record.get("case") == "http_and_elaboration":
            total = len(record.get("candidates", []))
            accepted = record.get("accepted", 0)
            print(f"[real-e2e] 候选 {total} 条，Lean 接受 {accepted} 条（利用率 {accepted / total:.0%}）" if total else
                  "[real-e2e] 无候选")
            for candidate, review in zip(record.get("candidates", []), record.get("reviews", [])):
                print(f"           review={review:<4} {candidate}")
            for rejected in record.get("rejected", []):
                print(f"           拒绝: {rejected[:160]}")
        elif record.get("case") == "mcts_edge":
            print(f"[real-e2e] MCTS 根节点获得 {record.get('conjecture_edges')} 条猜想边")

    if WALL_CLOCK.exists():
        names = [json.loads(line)["name"] for line in WALL_CLOCK.read_text(encoding="utf-8").splitlines() if line.strip()]
        counts = {name: names.count(name) for name in set(names)}
        print(f"[real-e2e] wall-clock: {counts}")
        summary["wall_clock"] = counts

    (EXPERIMENTS / "real_e2e.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[real-e2e] 结果写入 {EXPERIMENTS / 'real_e2e.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
