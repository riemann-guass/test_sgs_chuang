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

# 本文件在 `sgsr/verification/` 下，仓库根要往上三层：
# client.py → verification/ → sgsr/ → sgs-reap/（重构时这里踩过一次：parents[1] 会指到 sgsr/，
# 表现为 `NotADirectoryError: [WinError 267]`——因为 cwd 被指向一个不存在的目录）
ROOT = Path(__file__).resolve().parents[2]
SGSLEAN = ROOT / "sgslean"
RUNS = ROOT / "experiments" / "runs"
LAKE = os.environ.get("LAKE", "lake")


class LeanServerError(RuntimeError):
    pass


# 心跳预算的经验值与"批内累计"这个坑
# -----------------------------------
# Lean 的心跳计数器是**按 command 累计**的：一整批作业都跑在同一个
# `example : True := by run_tac ...` 里，所以计数器**不会**在作业之间复位。
# 症状极具迷惑性——phase19 用 9 条候选（`True` / `1 = 1` 这种）做离线漏斗，
# 27 条作业、预算 4,000,000，结果 8/9 条候选在**门检**就被判 `exception`：
# 不是这些命题难，而是前面几十条作业把累计预算用光了。
#
# 这也回头解释了 phase17：miniF2F 的 G1 每批 60 条作业、预算 4,000,000，
# 于是"每批后半段的作业集体报 exception"，被误读成"模型证不出"。
# phase18 把预算提到 40,000,000 后 164 条一批只剩 11 条 exception，
# 与"累计"这个模型一致（≈240k 心跳/作业）。
#
# 因此：**预算必须随批大小放大**，而不是固定值。
HEARTBEATS_BASE = 4_000_000
HEARTBEATS_PER_JOB = 200_000


def budget_for_jobs(n_jobs: int) -> int:
    """按批大小估一个够用的心跳预算。

    这是**经验估计**，不是定理。真实做法应该是每个作业前后复位计数器
    （若 Lean 暴露该 API）或改成每作业一个 command；在做到那一步之前，
    这个估计配合"报告里如实记录预算"足够可靠——而且它是可验证的：
    若某批里出现 `exception`，先看它是不是集中在批次尾部。
    """
    return HEARTBEATS_BASE + HEARTBEATS_PER_JOB * max(0, n_jobs)


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

    def verify_all(self, items: list[dict], chunk: int = 0) -> dict[str, dict]:
        """`items` = [{id, stmt, proof}, ...]；按 chunk 分批。

        **`chunk=0`（默认）表示"一次全喂"**。原因：服务端每处理一个 flush 都会
        **新起一个 `lean` 子进程**（`Server.runBatch`），而每个子进程都要重新导入一次
        Mathlib（本机实测 ≈2 min 热 / ≈8 min 冷）。分 5 批就白付 5 次导入——
        phase18 的 164 条候选跑了 2549 s，其中相当一部分是导入。

        安全性由"逐条落盘"保证（`Server.runJobs` 每处理完一条就写 `out.json`）：
        即使某个候选把子进程打崩，也只会丢它自己那一条，不会丢整批。

        真正彻底的修法是让子进程跨 flush 常驻（真正的 REPL），那是后续工作；
        在"请求可以一次算出来"的场景下，一次全喂已经等价。
        """
        if chunk <= 0:
            chunk = max(1, len(items))
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
