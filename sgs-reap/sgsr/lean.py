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

# 本文件在 `sgsr/` 下，仓库根（sgs-reap/）往上两层：
# lean.py → sgsr/ → sgs-reap/。
# 这个数字随文件位置改过两次（每次移动都要改），表现都是"服务起不来、报告为空"：
# 指错一层会让 `lake exe` 在一个不存在的 cwd 里跑，子进程立刻 exit=1。
ROOT = Path(__file__).resolve().parents[1]
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
# 需要更紧/更松的预算时用 `SGSLEAN_HEARTBEATS` 显式指定（把"预算掐死"与"真判定"
# 分开做诊断时用；注意常驻循环跑在一个 command 里，这个额度是整个会话的）。
#: **0 = 不限制**。子进程现在跨批常驻（见 `SgsLean/Server.lean` 的 `serveLoop`），
#: 整条服务循环跑在**一个 command** 里，按 command 累计的额度会把后面的批次掐死。
#: 于是回到上游 SGS 的配置：`maxHeartbeats 0` + 每条 tactic 的墙钟 `reap.timeout`
#: （默认 200 s）作为真正的上界。需要显式封顶时用 `SGSLEAN_HEARTBEATS` 指定。
HEARTBEATS_PER_JOB = 0


def budget_for_jobs(n_jobs: int = 0) -> int:  # noqa: ARG001 - 参数保留兼容旧调用点
    """心跳预算（0 = 不限制，靠墙钟兜底）。参数 `n_jobs` 保留只为兼容旧调用点。"""
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
        #: 常驻协议里的批号（单调递增，唯一）；每个批次的响应写进 `out.<n>.json`。
        self._batch_seq = 0
        self._workdir_path = Path(workdir)

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
        # 复用同一个工作目录时要清掉上一轮的协议残留：`served.txt` 会让子进程认为
        # 第 1..k 批已经处理过（**这正是 phase26 的第三个坑**）。
        self._workdir_path.mkdir(parents=True, exist_ok=True)
        for name in ("request.json", "served.txt", "stop.flag"):
            try:
                (self._workdir_path / name).unlink()
            except OSError:
                pass
        for stale in self._workdir_path.glob("out.*.json"):
            try:
                stale.unlink()
            except OSError:
                pass
        self._batch_seq = 0
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
            # 先请子进程自己退出（`stop.flag`），再等它；超时才强杀。
            try:
                (self._workdir_path / "stop.flag").write_text("stop", encoding="utf-8")
            except OSError:
                pass
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=60)
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
        """提交一批作业并等回响应 → `{id: 响应}`。**同一进程内跨批复用**（见 `serveLoop`）。

        协议三件套：

        1. 父进程把 `{"batch": n, "jobs": [...]}` **原子替换**进 `request.json`（n 单调递增）；
        2. 子进程把第 n 批的响应**逐条增量**写进 `out.<n>.json`；
        3. 完成判据 = 该文件的响应条数 ≥ 作业数 —— 不看退出码、不看"标记文件"
           （phase26 的坑：父等退出码、子等标记 → 死锁；空 out 文件被当成结果；
           残留标记被当成"本批已完成"；子进程比 `jobs.json` 还快 → 读到旧内容。
           **批号唯一 ⟹ 文件名唯一**，这四条一次消掉）。

        实测收益：Mathlib 导入从"每批一次（75–150 s）"变成"每个会话一次"，
        20 题批量评测的固定成本从约 1 小时降到约 2 分钟。
        """
        if self.proc is None:
            raise LeanServerError("server 未启动")
        if self.proc.poll() is not None:
            raise LeanServerError(
                f"server 已退出（exit={self.proc.returncode}）；stderr 见 {self.stderr_path}"
            )
        self._batch_seq += 1
        seq = self._batch_seq
        tmp_path = self._workdir_path / "request.tmp"
        tmp_path.write_text(
            json.dumps({"batch": seq, "jobs": jobs}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self._workdir_path / "request.json")
        out_path = self._workdir_path / f"out.{seq}.json"
        started = time.perf_counter()
        payload: list | None = None
        while payload is None:
            if time.perf_counter() - started > timeout:
                raise LeanServerError(
                    f"等待第 {seq} 批响应超时（{timeout}s，作业 {len(jobs)} 条）；"
                    f"工作目录 {self._workdir_path}"
                )
            if self.proc.poll() is not None:
                raise LeanServerError(
                    f"server 在处理第 {seq} 批时退出（exit={self.proc.returncode}）；"
                    f"stderr 见 {self.stderr_path}，child.log 见 {self._workdir_path}"
                )
            try:
                data = json.loads(out_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, list) and len(data) >= len(jobs):
                payload = data[: len(jobs)]
            else:
                time.sleep(0.05)
        # 前端耗时口径：父进程等待每批响应的墙钟之和（首批含 Mathlib 导入）。
        self.frontend_ms_total += int((time.perf_counter() - started) * 1000)
        responses: dict[str, dict] = {}
        for item in payload:
            rid = str(item.get("id"))
            if rid in responses:
                raise LeanServerError(f"server 重复回了 id={rid} 的响应（协议违规）")
            responses[rid] = item
        missing = [str(job.get("id")) for job in jobs if str(job.get("id")) not in responses]
        if missing:
            # 少回一条 = 协议错误，必须当场炸：静默让调用方把"响应缺失"当成
            # "这条候选不成立"，正好是审计最反对的那类失真。
            raise LeanServerError(f"server 少回了 {len(missing)} 条响应（例如 {missing[:3]}）")
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
            return preflight_in_session(server, probe_stmt)
    except (LeanServerError, OSError) as exc:
        return False, f"预检进程失败：{type(exc).__name__}: {exc}"


def preflight_in_session(server: "LeanServer", probe_stmt: str = "True") -> tuple[bool, str]:
    """在**已经开好的会话**里做环境预检（省掉一次 Mathlib 导入）。

    子进程常驻之后，一次会话可以服务整批实验，所以预检也该在同一会话里做——
    否则"预检 1 次导入 + 实验 N 次导入"里那次预检显得很贵。
    """
    try:
        response = server.batch([{"id": "pf", "cmd": "check", "stmt": probe_stmt}])
    except (LeanServerError, OSError) as exc:
        return False, f"预检失败：{type(exc).__name__}: {exc}"
    entry = response.get("pf") or {}
    if entry.get("error"):
        return False, f"预检协议错误：{json.dumps(entry['error'], ensure_ascii=False)[:300]}"
    result = entry.get("result") or {}
    if result.get("ok") is not True:
        return False, f"预检判定未通过：{json.dumps(result, ensure_ascii=False)[:300]}"
    return True, "ok"


if __name__ == "__main__":
    raise SystemExit(main())
