"""真实模型代理的端到端冒烟测试（会消耗 API 额度，默认只跑 1 个目标）。

流程：启动 proxy → /health → /conjecture（真实模型）→ /guide（真实模型）→ /stats → 关闭。
同时把结果落盘到 experiments/proxy_smoke.json，作为闸门 M1 的第一份数据点。

用法：
    C:\\Users\\gaosen\\anaconda3\\python.exe scripts/run_proxy_smoke.py
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
MODELS = REPO / "sgsr" / "models"
EXPERIMENTS = REPO / "experiments"
sys.path.insert(0, str(REPO))
from sgsr.models import config  # noqa: E402  (需要先把 MODELS 目录加入 sys.path)
from sgsr.utils.service import dump_log, parse_port  # noqa: E402

PORT = 8770
BASE = f"http://127.0.0.1:{PORT}"

GOAL_STATE = """n : ℕ
⊢ 2 ∣ n ^ 2 + n"""


def http_json(path: str, payload: dict | None = None, timeout: int = 600) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method="GET" if payload is None else "POST"
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:400]}


def wait_health(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, body = http_json("/health", timeout=3)
            if status == 200 and body.get("status") == "ok":
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    global PORT, BASE
    PORT = parse_port("真实模型代理的端到端冒烟（消耗 API 额度）", PORT)
    BASE = f"http://127.0.0.1:{PORT}"
    config.ensure_utf8_stdout()
    EXPERIMENTS.mkdir(exist_ok=True)
    log_file = open(MODELS / "proxy_smoke_stdout.log", "w", encoding="utf-8")
    proxy = subprocess.Popen(
        [sys.executable, "proxy.py", "--port", str(PORT)],
        cwd=MODELS,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    result: dict = {"goal_state": GOAL_STATE}
    try:
        if not wait_health():
            print("[smoke] proxy 未就绪（真代理需要网络权限与 sgsr/models/.env 里的 API key）")
            log_file.flush()
            dump_log(MODELS / "proxy_smoke_stdout.log", label="smoke")
            return 1
        status, health = http_json("/health")
        print(f"[smoke] /health -> {status} {health}")
        result["health"] = health

        status, conj = http_json(
            "/conjecture", {"goal_state": GOAL_STATE, "num_samples": 3, "depth": 0}
        )
        print(f"[smoke] /conjecture -> HTTP {status}")
        if status != 200:
            print(json.dumps(conj, ensure_ascii=False)[:600])
            return 1
        result["conjecture"] = conj
        for cand in conj.get("candidates", []):
            print(f"          [{cand['index']}] {cand['type']}")
        if not conj.get("candidates"):
            print("[smoke] 未解析出任何候选命题")
            return 1

        status, guide = http_json(
            "/guide",
            {
                "target": GOAL_STATE,
                "candidates": [{"index": c["index"], "type": c["type"]} for c in conj["candidates"]],
            },
        )
        print(f"[smoke] /guide -> HTTP {status}")
        result["guide"] = guide
        for score in guide.get("scores", []):
            print(
                f"          [{score['index']}] relevance={score['relevance']} "
                f"redundancy={score['redundancy']} complexity={score['complexity']} "
                f"review={score['review']}"
            )

        status, stats = http_json("/stats")
        print(f"[smoke] /stats -> {json.dumps(stats, ensure_ascii=False)}")
        result["stats"] = stats
        return 0
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
        log_file.close()
        (EXPERIMENTS / "proxy_smoke.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[smoke] 结果已写入 {EXPERIMENTS / 'proxy_smoke.json'}")


if __name__ == "__main__":
    raise SystemExit(main())
