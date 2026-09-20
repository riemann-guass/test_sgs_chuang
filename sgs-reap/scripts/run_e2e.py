"""阶段 1 端到端测试的运行器。

1. 启动确定性假服务（默认 127.0.0.1:8765），并把候选命题池设为测试所需的 `P`；
2. 用 `lake env lean` 跑 `reap-fork/tests/ConjectureE2E.lean`（真实 HTTP 往返）；
3. 跑 `reap-fork/tests/ConjectureDegrade.lean`（服务不可达时的降级行为）；
4. 关闭假服务并汇报结果。

用法：
    C:\\Users\\gaosen\\anaconda3\\python.exe scripts/run_e2e.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORK = REPO / "reap-fork"
MODELS = REPO / "sgsr" / "models"
PORT = 8765
WALL_CLOCK = FORK / "e2e_wall_clock.jsonl"
RAW_TREE = FORK / "e2e_raw_tree.json"


def wait_for_health(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{PORT}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8")).get("status") == "ok"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(0.3)
    return False


def run_lean(lean_file: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["lake", "env", "lean", lean_file],
        cwd=FORK,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    mock_env = dict(os.environ)
    # 假服务返回的候选命题，供 ConjectureE2E.lean 中的目标 `Q`（有 h : P, himp : P → Q）使用
    mock_env["MOCK_CANDIDATES"] = json.dumps(["P"])
    mock_env["MOCK_RELEVANT"] = json.dumps(["P"])

    mock = subprocess.Popen(
        [sys.executable, "mock_server.py", "--port", str(PORT)],
        cwd=MODELS,
        env=mock_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    failures: list[str] = []
    try:
        for artefact in (WALL_CLOCK, RAW_TREE):
            if artefact.exists():
                artefact.unlink()

        if not wait_for_health():
            print("[run_e2e] mock server failed to become healthy")
            if mock.stdout is not None:
                print(mock.stdout.read())
            return 1
        print(f"[run_e2e] mock server healthy on 127.0.0.1:{PORT}")

        for lean_file in ("tests/ConjectureE2E.lean", "tests/ConjectureDegrade.lean"):
            code, out = run_lean(lean_file)
            if code == 0:
                print(f"[run_e2e] PASS  {lean_file}")
            else:
                print(f"[run_e2e] FAIL  {lean_file} (exit {code})")
                print(out)
                failures.append(lean_file)

        failures.extend(check_artefacts())
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()
        print("[run_e2e] mock server stopped")

    if failures:
        print(f"[run_e2e] FAILED: {', '.join(failures)}")
        return 1
    print("[run_e2e] ALL PASS")
    return 0


def check_artefacts() -> list[str]:
    """验证可观测性产物：wall-clock 记录里必须出现 conjecture / guide 两类调用，
    raw tree 必须是可解析的 JSON。这些文件同时是阶段 4 的成本核算依据。"""
    failures: list[str] = []
    if not WALL_CLOCK.exists():
        failures.append("e2e_wall_clock.jsonl 未生成")
        return failures
    names = [json.loads(line)["name"] for line in WALL_CLOCK.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = {name: names.count(name) for name in set(names)}
    print(f"[run_e2e] wall-clock records: {counts}")
    for required in ("conjecture", "guide"):
        if counts.get(required, 0) < 1:
            failures.append(f"wall-clock 缺少 {required} 记录")
    if not RAW_TREE.exists():
        failures.append("e2e_raw_tree.json 未生成")
    else:
        try:
            json.loads(RAW_TREE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"raw tree 不是合法 JSON: {exc}")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
