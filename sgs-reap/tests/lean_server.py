"""`sgslean-server` 的常驻客户端（批处理 + 一次 frontend）。

为什么需要它：每次 `lake exe sgslean-server` 都等于**重新导入一次 Mathlib**
（本机实测 67 s（热）～493 s（冷））。现有 harness 是"一个 chunk 起一个进程"，
三个 chunk 就白付三次导入。本模块让一个进程处理任意多批：

    with LeanServer(imports="Mathlib", heartbeats=40_000_000) as server:
        responses = server.batch([...jobs...])
        responses2 = server.batch([...jobs...])   # 复用同一个进程

协议：stdin 一行一条 JSON 请求；`{"cmd":"flush"}` 触发本批执行，
随后 stdout 依次输出「每条请求一行响应」+「一行 flush 响应」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SGSLEAN = ROOT / "sgslean"
RUNS = ROOT / "experiments" / "runs"
LAKE = os.environ.get("LAKE", "lake")


class LeanServerError(RuntimeError):
    pass


class LeanServer:
    """一次性启动、多批复用的 `sgslean-server`。"""

    def __init__(
        self,
        imports: str = "Mathlib",
        heartbeats: int | None = None,
        trivial_heartbeats: int | None = None,
        tactic_timeout_ms: int | None = None,
        workdir: str | None = None,
        stderr_path: Path | None = None,
    ) -> None:
        self.imports = imports
        self.heartbeats = heartbeats
        self.trivial_heartbeats = trivial_heartbeats
        self.tactic_timeout_ms = tactic_timeout_ms
        self.workdir = workdir
        self.stderr_path = stderr_path
        self.proc: subprocess.Popen | None = None
        self._stderr_file = None
        self.frontend_ms_total = 0

    # ---- 生命周期 ----
    def start(self) -> "LeanServer":
        env = dict(os.environ)
        env["SGSLEAN_IMPORTS"] = self.imports
        if self.heartbeats is not None:
            env["SGSLEAN_HEARTBEATS"] = str(self.heartbeats)
        if self.trivial_heartbeats is not None:
            env["SGSLEAN_TRIVIAL_HEARTBEATS"] = str(self.trivial_heartbeats)
        if self.tactic_timeout_ms is not None:
            env["SGSLEAN_TACTIC_TIMEOUT_MS"] = str(self.tactic_timeout_ms)
        if self.workdir is not None:
            env["SGSLEAN_WORKDIR"] = self.workdir

        if self.stderr_path is not None:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_file = open(self.stderr_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [LAKE, "exe", "sgslean-server"],
            cwd=str(SGSLEAN),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file if self._stderr_file is not None else subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        return self

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=120)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            self.proc.kill()
        finally:
            if self._stderr_file is not None:
                self._stderr_file.close()
            self.proc = None

    def __enter__(self) -> "LeanServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 请求 ----
    def batch(self, jobs: list[dict], timeout: float = 7200.0) -> dict[str, dict]:
        """提交一批作业并等回响应。返回 `{id: 响应}`（含 flush 那条）。"""
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise LeanServerError("server 未启动")
        flush_id = f"__flush__{int(time.time() * 1000)}"
        lines = [json.dumps(job, ensure_ascii=False) for job in jobs]
        lines.append(json.dumps({"id": flush_id, "cmd": "flush"}))
        payload = "\n".join(lines) + "\n"
        started = time.perf_counter()
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise LeanServerError(f"写入 server 失败（子进程可能已退出）：{exc}") from exc

        responses: dict[str, dict] = {}
        expected = len(jobs) + 1
        while len(responses) < expected:
            if time.perf_counter() - started > timeout:
                raise LeanServerError(f"等待响应超时（{timeout}s，已收到 {len(responses)}/{expected}）")
            line = self.proc.stdout.readline()
            if line == "":
                raise LeanServerError(
                    f"server 在返回全部响应前关闭（收到 {len(responses)}/{expected}）；"
                    f"stderr 见 {self.stderr_path}"
                )
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)  # 协议纯度：每一行都必须是 JSON
            responses[str(item.get("id"))] = item
            if str(item.get("id")) == flush_id:
                self.frontend_ms_total += int((item.get("result") or {}).get("frontend_ms", 0) or 0)
        return responses

    def ping(self) -> dict:
        response = self.batch([{"id": "ping0", "cmd": "ping"}])
        return response["ping0"].get("result") or {}

    def verify_all(self, items: list[dict], chunk: int = 40) -> dict[str, dict]:
        """`items` = [{id, stmt, proof}, ...]；按 chunk 分批，复用同一进程。"""
        out: dict[str, dict] = {}
        for start in range(0, len(items), chunk):
            part = items[start : start + chunk]
            jobs = [
                {"id": it["id"], "cmd": "verify", "stmt": it["stmt"], "proof": it["proof"]}
                for it in part
            ]
            out.update(self.batch(jobs))
        return out


def main() -> int:
    """自测：起服务、ping、验证一条已知真与一条已知假。"""
    sys.stdout.reconfigure(encoding="utf-8")
    with LeanServer(imports=os.environ.get("SGSLEAN_IMPORTS", "Mathlib")) as server:
        info = server.ping()
        print("[lean-server] ping:", json.dumps(info, ensure_ascii=False))
        responses = server.verify_all(
            [
                {"id": "t", "stmt": "∀ (n : Nat), n + 0 = n", "proof": "intro n\nrfl"},
                {"id": "f", "stmt": "∀ (n : Nat), n + 0 = n", "proof": "sorry"},
            ]
        )
        for key in ("t", "f"):
            print(f"[lean-server] {key}:", json.dumps(responses[key]["result"], ensure_ascii=False))
        assert (responses["t"]["result"] or {}).get("ok") is True, "真证明应判通过"
        assert (responses["f"]["result"] or {}).get("ok") is False, "sorry 应判失败"
    print("[lean-server] PASS")
    return 0


def latest_traces(pattern: str = "g1_*/traces.jsonl") -> Path | None:
    """最近一次 g1 运行的轨迹文件（诊断脚本的默认输入）。"""
    found = sorted(RUNS.glob(pattern))
    return found[-1] if found else None


if __name__ == "__main__":
    raise SystemExit(main())
