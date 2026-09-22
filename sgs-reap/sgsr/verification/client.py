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
from itertools import count
from pathlib import Path

# 本文件在 `sgsr/verification/` 下，仓库根要往上三层：
# client.py → verification/ → sgsr/ → sgs-reap/（重构时这里踩过一次：parents[1] 会指到 sgsr/，
# 表现为 `NotADirectoryError: [WinError 267]`——因为 cwd 被指向一个不存在的目录）
ROOT = Path(__file__).resolve().parents[2]
SGSLEAN = ROOT / "sgslean"
RUNS = ROOT / "experiments" / "runs"
LAKE = os.environ.get("LAKE", "lake")

#: 每个客户端实例一个**独立工作目录**。
#:
#: 服务端默认用固定的 `.lake/sgslean-server-work`，多实例同时跑会互相覆盖
#: `jobs.json` / `out.json`（审计"并发与稳定性"一节：响应串扰或文件覆盖）。
#: 这里按 pid + 序号分配子目录；同一实例内复用同一个目录，因为服务端每批都要
#: 把 `sgslean_snippet.lean` / `run_child.cmd` 重新写一遍，换目录会白付一次文件写入
#: （不影响正确性，只影响几十毫秒）。
#:
#: 目录必须**留在 `.lake/` 里面**：子进程是 `lean sgslean_snippet.lean` 驱动的，
#: 它靠工作目录在项目内才能拿到 lake 给的搜索路径（`LEAN_PATH`）。
#: 把它挪到系统临时目录会退化成 `unknown module prefix 'SgsLean'`（实测踩过）。
_WORKDIR_SEQ = count(1)


class LeanServerError(RuntimeError):
    pass


# 心跳预算：**单条作业一份，与批大小无关**
# ----------------------------------------
# 历史（phase17–20 的坑）：Lean 的心跳计数器**按 command 累计**，而当时一整批作业
# 跑在同一个 `example : True := by run_tac ...` 里，于是批次尾部的作业被前面的作业
# 拖死，集体报 `maximum number of heartbeats` —— 看起来像"模型证不出"。
# 当时的缓解办法是把预算按批大小放大（`4M + 200k × 作业数`）。
#
# 2026-09-22 之后，服务端改成**一个作业一个 command**（见 `SgsLean/Server.lean` 的
# `runJob`/`snippetSource`）：计数器在每个 command 开头复位，累计伪影从根上消失。
# 于是"按批大小放大"不再有意义（它现在只是给**每一条**作业都发一份放大后的预算），
# 这里改成固定值，并按实测标定：
#
# * `aime_1984_p15` 的题面在 52.8M 心跳下 `whnf` 超时、在 400M 下通过
#   （miniF2F 里最重的题面之一，主要开销是 Mathlib 解释执行下的 `whnf`/`isDefEq`）；
# * 真正的上界是**墙钟**：每条约 tactic 受 `reap.timeout`（默认 200 s）约束，
#   整批受 `batch(timeout=...)` 约束。
#
# 需要更紧/更松的预算时用 `SGSLEAN_HEARTBEATS` 显式指定（`diagnose_exceptions.py`
# 就是这么把"预算掐死"与"真判定"分开的）。
HEARTBEATS_PER_JOB = 400_000_000


def budget_for_jobs(n_jobs: int = 0) -> int:  # noqa: ARG001 - 参数保留兼容旧调用点
    """单条作业的心跳预算。

    参数 `n_jobs` **不再影响结果**：每个作业跑在自己的 command 里，预算逐条独立，
    按批大小放大只会让"报告里的预算"失去意义。保留参数是为了不动各脚本的调用点。
    """
    return HEARTBEATS_PER_JOB


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
        # 默认给每个实例一个独立工作目录（见 `_WORKDIR_SEQ` 的说明）。
        # 放在 `.lake/` 下：既在项目内（子进程需要），又在 `.gitignore` 里（不入库）。
        if workdir is None:
            base = os.environ.get("SGSLEAN_WORKDIR_ROOT") or str(
                SGSLEAN / ".lake" / "sgslean-server-work"
            )
            workdir = os.path.join(base, f"{os.getpid()}-{next(_WORKDIR_SEQ)}")
        self.workdir = workdir
        self.stderr_path = stderr_path
        self.proc: subprocess.Popen | None = None
        self._stderr_file = None
        self.frontend_ms_total = 0
        self.batch_count = 0

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
        # **按收到的行数**判断收齐，而不是按响应字典的条数：字典长度在"服务端把同一个 id
        # 回了两次"时会永远到不了 expected，于是这里会一直阻塞到 `timeout`（默认 2 小时）
        # 才报超时——现象是"批处理卡死"，而不是"丢了一条响应"。phase26 记的
        # "没有解释的丢响应"很可能就是这个形态。
        lines_read = 0
        while lines_read < expected:
            if time.perf_counter() - started > timeout:
                raise LeanServerError(
                    f"等待响应超时（{timeout}s，已收到 {lines_read}/{expected} 行）"
                )
            line = self.proc.stdout.readline()
            if line == "":
                raise LeanServerError(
                    f"server 在返回全部响应前关闭（收到 {lines_read}/{expected} 行）；"
                    f"stderr 见 {self.stderr_path}"
                )
            line = line.strip()
            if not line:
                continue
            lines_read += 1
            try:
                item = json.loads(line)  # 协议纯度：每一行都必须是 JSON
            except ValueError as exc:
                raise LeanServerError(f"server 回了非 JSON 行：{line[:200]!r}") from exc
            rid = str(item.get("id"))
            if rid in responses:
                raise LeanServerError(
                    f"server 重复回了 id={rid} 的响应（协议违规）；已收到 {lines_read}/{expected} 行"
                )
            responses[rid] = item
            if rid == flush_id:
                self.frontend_ms_total += int((item.get("result") or {}).get("frontend_ms", 0) or 0)
        missing = [str(job.get("id")) for job in jobs if str(job.get("id")) not in responses]
        if missing:
            # 少回一条 = 协议错误，必须当场炸：静默让调用方把"响应缺失"当成
            # "这条候选不成立"，正好是审计最反对的那类失真。
            raise LeanServerError(
                f"server 少回了 {len(missing)} 条响应（例如 {missing[:3]}）"
            )
        if flush_id not in responses:
            raise LeanServerError("server 没有回 flush 响应：无法确认本批已结束")
        self.batch_count += 1
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


def preflight_imports(imports: str, probe_stmt: str = "True",
                      stderr_path: Path | None = None) -> tuple[bool, str]:
    """**环境预检**：这套 import（尤其是 `SgsLean.GeneratedLibrary`）真的能用吗？

    phase22 踩过一次：`lake build SgsLean`（库目标）不会编
    `SgsLean.GeneratedLibrary`，于是处理臂的片段以 "object file ... does not exist"
    整批崩掉，所有判定为空——却表现为"给库之后成绩退化"的**假结论**。
    所以每次动到库的实验都必须先跑这个预检，失败就直接退出，不让假数据流进报告。

    返回 `(是否可用, 说明)`。预检本身只花一次导入。
    """
    try:
        with LeanServer(imports=imports, heartbeats=budget_for_jobs(1),
                        stderr_path=stderr_path) as server:
            response = server.batch([{"id": "pf", "cmd": "check", "stmt": probe_stmt}])
    except (LeanServerError, OSError) as exc:
        return False, f"预检进程失败：{type(exc).__name__}: {exc}"
    entry = response.get("pf") or {}
    if entry.get("error"):
        return False, f"预检协议错误：{json.dumps(entry['error'], ensure_ascii=False)[:300]}"
    result = entry.get("result") or {}
    if result.get("ok") is not True:
        return False, f"预检判定未通过：{json.dumps(result, ensure_ascii=False)[:300]}"
    return True, "ok"


if __name__ == "__main__":
    raise SystemExit(main())
