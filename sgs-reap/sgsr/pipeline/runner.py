"""SG-Lean 的闭环编排（一轮 = 图 A 的完整流程）。

这是整个项目**唯一**把各零件串成环的地方。在此之前，每一件都有实现与测试，
但没有任何代码把它们按顺序连起来（审计结论）：门检/硬门/软分/物化/库各自独立，
"环"只存在于文档里。本模块补上这一环：

    ① 采轨迹（在 C 上跑 Solver + 验证 + 记录子目标签名）
    ② 需求挖掘（N1：分层统计 + 跨目标一致性过滤）
    ③ 猜想（条件化在 [未解目标 + 需求 + 库范例]）
    ④ 判据层 G：门检 → 硬门（非平凡 ∧ 新颖）
    ⑤ 求解（k 篇证明）→ ⑥ 验证（可证硬门）
    ⑦ 软分（依赖抽取；压缩收益在能配对时才算）
    ⑧ 选择（N2：按"父目标覆盖"做子模贪心，受库容 B 约束）
    ⑨ 物化 + 入库 + 记忆注入（下一轮的提示词自动带上库）
    ─────────────────────────────────────────────
    ⑩ 一轮结束 → 回到 ①（此时提示词里已经有库了）

## 数据角色（见 `docs/data-protocol.md`，这里用代码强制）

* `curriculum`（C）：**唯一**允许进库的数据。采轨迹、挖需求、猜想都只在这里做；
* `dev`（D）：只用于调参与确认装置，**不进库、不进需求**；
* `test`（T）：只在最终评测时读一次。本模块默认**拒绝**读它（`--allow-test` 才放行，
  且放行时不给建库）。

## 为什么"选择"用的是覆盖而不是分数

N2 的目标是"给定工作负载，选哪一小组引理入库"——这是数据库里的**物化视图选择**问题，
`cover(S) = |{w ∈ W : w 能被 S 帮助证明}|` 是并集形式，天然单调子模，贪心有 (1−1/e) 界。

但**真 cover 需要两臂测量**（有库/无库各跑一遍），代价高。所以分两层：

* **选择时**用廉价代理 `parent_cover`：一条候选"覆盖"的是它**为之生成的那个目标**
  （父目标关系来自生成过程，不来自测量，因此不循环）。贪心就是"在库容 B 内覆盖尽量多不同目标"。
  这与"视图是为某些查询建的"结构完全一致。
* **评测时**用 `proved_cover`（`scripts/run_gate_g3_real.py`）验证代理是否忠实——
  这正是最初计划里闸门 G3 要回答的问题。
"""

from __future__ import annotations

import collections
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sgsr.pipeline.conjecture import generate as conjecture_generate
from sgsr.pipeline.conjecture import load_demand, load_seeds
from sgsr.pipeline.coverage import greedy as coverage_greedy
from sgsr.pipeline.demand import mine as mine_demand
from sgsr.pipeline.library import add_many, load as load_library
from sgsr.data.schema import normalize_sig, trace_from_job


# ─────────────────────────── 数据角色 ───────────────────────────


ROLE_CURRICULUM = "curriculum"
ROLE_DEV = "dev"
ROLE_TEST = "test"


class DataRoleError(RuntimeError):
    """把 D/T 的数据带进建库流程时报错（见 docs/data-protocol.md）。"""


@dataclass
class TargetSet:
    """一组目标 + 它的数据角色。角色决定它可以被哪些环节读取。"""

    role: str
    path: Path
    rows: list[dict] = field(default_factory=list)

    @staticmethod
    def load(role: str, path: str | Path) -> "TargetSet":
        p = Path(path)
        rows = [
            json.loads(line)
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for row in rows:
            row.setdefault("statement", row.get("stmt", ""))
        return TargetSet(role=role, path=p, rows=rows)

    def assert_buildable(self) -> None:
        """只有 C 允许进库/挖需求。"""
        if self.role != ROLE_CURRICULUM:
            raise DataRoleError(
                f"{self.path.name} 的角色是 {self.role}；"
                "按 docs/data-protocol.md，只有 curriculum 可以进库/挖需求"
            )

    def batch(self, limit: int = 0) -> list[dict]:
        return self.rows[:limit] if limit else list(self.rows)


# ─────────────────────────── 配置与记录 ───────────────────────────


@dataclass
class RoundConfig:
    round_index: int = 0
    solve_endpoint: str = "http://127.0.0.1:8765/solve"
    conjecture_endpoint: str = "http://127.0.0.1:8765/conjecture"
    imports: str = "Mathlib"
    k_solve: int = 3
    n_conjecture: int = 3
    demand_limit: int = 8
    seeds_limit: int = 4
    library_budget: int = 30          # B：库容
    target_limit: int = 0             # 本轮用多少条 C 目标（0 = 全部）
    min_proof_steps: int = 2          # 软分：证明过短（≤ 该步数）的引理记 warning


@dataclass
class RoundReport:
    round_index: int
    started_at: str
    funnel: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)
    demand: dict = field(default_factory=dict)
    library_before: list[str] = field(default_factory=list)
    library_after: list[str] = field(default_factory=list)
    solved_targets: list[str] = field(default_factory=list)
    cover_delta: int | None = None
    selected: list[dict] = field(default_factory=list)
    timing_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "round": self.round_index,
            "started_at": self.started_at,
            "funnel": self.funnel,
            "reasons": self.reasons,
            "demand": self.demand,
            "library_before": self.library_before,
            "library_after": self.library_after,
            "solved_targets": self.solved_targets,
            "cover_delta": self.cover_delta,
            "selected": self.selected,
            "timing_s": round(self.timing_s, 1),
        }


# ─────────────────────────── HTTP 小工具 ───────────────────────────


def _post(url: str, payload: dict, timeout: float = 300.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": {"code": f"http_{exc.code}",
                          "message": exc.read().decode("utf-8", "replace")[:300]}}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"error": {"code": "network", "message": str(exc)}}


def solve(statement: str, k: int, endpoint: str, library: list[dict] | None = None) -> list[str]:
    payload: dict = {"statement": statement, "num_samples": k}
    if library:
        payload["library"] = library
    response = _post(endpoint, payload)
    return [p.get("proof", "") for p in (response.get("proofs") or []) if p.get("proof", "").strip()]


# ─────────────────────────── 闭环主体 ───────────────────────────


class RoundRunner:
    """跑一轮（或连续多轮）SG-Lean 闭环。"""

    def __init__(
        self,
        curriculum: TargetSet,
        config: RoundConfig,
        workdir: Path,
        library_path: Path,
        generated_path: Path,
        lean_server_factory,
        log=print,
    ) -> None:
        self.curriculum = curriculum
        self.config = config
        self.workdir = workdir
        self.library_path = library_path
        self.generated_path = generated_path
        self.lean_server_factory = lean_server_factory
        self.log = log
        self._solved_prev: set[str] | None = None
        # 每轮会起几次 `lean` 子进程（= 几次 Mathlib 导入）。导入是本项目最贵的固定成本，
        # 必须把它记录进报告，否则"一轮要多久"无法解释也无法优化。
        self._batches = 0

    # ---- ① 采轨迹 ----
    def collect(self, targets: list[dict], library: list[dict]) -> tuple[list[dict], list[dict]]:
        """在目标集上跑 Solver → 记录每条候选证明的轨迹（含子目标签名）。

        返回 `(traces, attempts)`：traces 供需求挖掘；attempts 供后续验证。
        """
        attempts: list[dict] = []
        for target in targets:
            proofs = solve(target["statement"], self.config.k_solve,
                           self.config.solve_endpoint, library=library)
            for idx, proof in enumerate(proofs):
                attempts.append({
                    "id": f"c:{target['id']}:{idx}",
                    "target": target["id"],
                    "statement": target["statement"],
                    "proof": proof,
                })
        traces: list[dict] = []
        if attempts:
            with self.lean_server_factory(len(attempts)) as server:
                jobs = [{"id": a["id"], "cmd": "trace", "stmt": a["statement"], "proof": a["proof"]}
                        for a in attempts]
                responses = server.batch(jobs)
                self._batches += 1
            for attempt in attempts:
                result = (responses.get(attempt["id"]) or {}).get("result") or {}
                traces.append(trace_from_job(attempt, result))
        return traces, attempts

    # ---- ② 需求 ----
    def demand(self, traces: list[dict]) -> dict:
        return mine_demand(traces)

    # ---- ③ 猜想 ----
    def conjecture(self, targets: list[dict], demand_sigs: list[str], seeds: list[str]) -> list[dict]:
        out: list[dict] = []
        for target in targets:
            result = conjecture_generate(
                self.config.conjecture_endpoint, target, demand_sigs, seeds,
                self.config.n_conjecture,
            )
            for candidate in result.candidates:
                out.append({
                    "key": f"{target['id']}#{candidate['index']}",
                    "target": target["id"],                        # 父目标（供 parent_cover 用）
                    "target_statement": target["statement"],
                    "stmt": candidate["type"],
                })
        return out

    # ---- ④ 判据层：门检 + 硬门 ----
    def screen(self, candidates: list[dict], library: list[dict], report: RoundReport) -> list[dict]:
        if not candidates:
            return []
        against = [c["stmt"] for c in library]
        with self.lean_server_factory(3 * len(candidates)) as server:
            jobs = []
            for cand in candidates:
                jobs.append({"id": f"c:{cand['key']}", "cmd": "check", "stmt": cand["stmt"]})
                jobs.append({"id": f"t:{cand['key']}", "cmd": "trivial", "stmt": cand["stmt"]})
                jobs.append({"id": f"n:{cand['key']}", "cmd": "novelty", "stmt": cand["stmt"],
                             "against": [cand["target_statement"], *against]})
            responses = server.batch(jobs)
            self._batches += 1

        survivors: list[dict] = []
        for cand in candidates:
            gate = (responses.get(f"c:{cand['key']}") or {}).get("result") or {}
            trivial = (responses.get(f"t:{cand['key']}") or {}).get("result") or {}
            novelty = (responses.get(f"n:{cand['key']}") or {}).get("result") or {}
            report.funnel["candidates"] = report.funnel.get("candidates", 0) + 1
            if gate.get("ok") is not True:
                report.funnel["rejected_gate"] = report.funnel.get("rejected_gate", 0) + 1
                report.reasons[f"gate:{gate.get('reason')}"] = \
                    report.reasons.get(f"gate:{gate.get('reason')}", 0) + 1
                continue
            if trivial.get("trivial") is True:
                report.funnel["rejected_trivial"] = report.funnel.get("rejected_trivial", 0) + 1
                report.reasons[f"trivial:{trivial.get('byTactic')}"] = \
                    report.reasons.get(f"trivial:{trivial.get('byTactic')}", 0) + 1
                continue
            if novelty.get("new") is not True:
                report.funnel["rejected_novelty"] = report.funnel.get("rejected_novelty", 0) + 1
                report.reasons["novelty:duplicate"] = report.reasons.get("novelty:duplicate", 0) + 1
                continue
            report.funnel["passed_hard_gates"] = report.funnel.get("passed_hard_gates", 0) + 1
            survivors.append(cand)
        return survivors

    # ---- ⑤ 求解 + ⑥ 验证 + ⑦ 软分 ----
    def prove_verify_measure(self, survivors: list[dict], report: RoundReport) -> list[dict]:
        verified: list[dict] = []
        if not survivors:
            return verified
        for cand in survivors:
            proofs = solve(cand["stmt"], self.config.k_solve,
                           self.config.solve_endpoint, library=None)
            cand["proofs"] = proofs
            report.funnel["proof_candidates"] = report.funnel.get("proof_candidates", 0) + len(proofs)

        verify_jobs = [
            # 用 `dependencies` 而不是 `verify`：它内部会先跑 `Verify.verify`，
            # 返回值里同时有 `verified` 与 `constants` —— **一次批处理同时拿到
            # "可证硬门"与软分（依赖抽取）**，省掉一整次 Mathlib 导入
            # （导入是本项目最贵的固定成本，每轮省一次就是分钟级的差别）。
            {"id": f"v:{c['key']}:{i}", "cmd": "dependencies", "stmt": c["stmt"], "proof": proof}
            for c in survivors for i, proof in enumerate(c["proofs"])
        ]
        with self.lean_server_factory(len(verify_jobs)) as server:
            responses = server.batch(verify_jobs)
            self._batches += 1

        for cand in survivors:
            ok_proof = None
            constants: list[str] = []
            for i, proof in enumerate(cand["proofs"]):
                result = (responses.get(f"v:{cand['key']}:{i}") or {}).get("result") or {}
                if result.get("ok") is True and ok_proof is None:
                    ok_proof = proof
                    constants = list(result.get("constants") or [])
            if ok_proof is None:
                report.funnel["rejected_unprovable"] = report.funnel.get("rejected_unprovable", 0) + 1
                report.reasons["solve:no_valid_proof"] = \
                    report.reasons.get("solve:no_valid_proof", 0) + 1
                continue
            cand["proof"] = ok_proof
            cand["constants"] = constants
            report.funnel["verified"] = report.funnel.get("verified", 0) + 1
            cand["proof_steps"] = len([line for line in ok_proof.splitlines() if line.strip()])
            if cand["proof_steps"] <= self.config.min_proof_steps:
                report.reasons["soft:cheap_proof"] = report.reasons.get("soft:cheap_proof", 0) + 1
            verified.append(cand)
        return verified

    # ---- ⑧ 选择 ----
    def select(self, verified: list[dict], library_size: int) -> tuple[list[dict], list[int]]:
        """N2：在库容 B 内用子模贪心选引理（覆盖尽量多的**不同父目标**）。

        实现在 `sgsr/pipeline/coverage.py`（那里同时给了子模性与近似比的自检工具）；
        这里只负责把库容换算成剩余预算、把 curriculum 当作目标集传进去。
        """
        remaining = max(0, self.config.library_budget - library_size)
        targets = self.curriculum.batch(self.config.target_limit)
        return coverage_greedy(verified, targets, remaining)

    # ---- ⑨ 物化 + 入库 ----
    def commit(self, selected: list[dict]) -> int:
        if not selected:
            return 0
        written = add_many(
            self.library_path,
            [
                {"stmt": c["stmt"], "proof": c["proof"], "verified": True,
                 "source": f"round{self.config.round_index}:{c['key']}",
                 "constants": c.get("constants", []), "delta_len": None,
                 "proof_steps": c.get("proof_steps")}
                for c in selected
            ],
        )
        # 物化整库（Materialize.emit 是重写整个文件），并编译出 olean 供 `import` 使用
        full = [
            {"stmt": row["stmt"], "proof": row["proof"], "verified": True,
             "source": row.get("source", "library")}
            for row in load_library(self.library_path)
        ]
        with self.lean_server_factory(len(full)) as server:
            server.batch([{"id": "mat", "cmd": "materialize",
                           "path": str(self.generated_path), "entries": full}])
            self._batches += 1
        return written

    # ---- ⑩ 一轮 ----
    def run_round(self) -> RoundReport:
        started = time.perf_counter()
        cfg = self.config
        report = RoundReport(round_index=cfg.round_index,
                             started_at=datetime.now(timezone.utc).isoformat())
        targets = self.curriculum.batch(cfg.target_limit)
        library = load_library(self.library_path)
        report.library_before = [row["stmt"] for row in library]
        lib_for_prompt = [
            {"name": f"sgs_lem_{i + 1}", "stmt": row["stmt"]} for i, row in enumerate(library)
        ][: cfg.seeds_limit]

        self.log(f"[round {cfg.round_index}] 目标 {len(targets)} 条；库 {len(library)} 条")

        # ① 采轨迹（"无库"臂天然来自第一轮；之后各轮的提示词里已有上一轮的库）
        traces, attempts = self.collect(targets, lib_for_prompt)
        # trace 作业内部已经跑过 `Verify.verify`，`TraceResult.verified` 就是结论——
        # 不要再单独起一批 verify（那会多付一次 Mathlib 导入）。
        solved: set[str] = {t["id"] for t in traces if t.get("verified") is True}
        report.solved_targets = sorted(solved)
        if self._solved_prev is not None:
            # 跨轮 cover：本轮（有库）− 上一轮（无库/旧库）。
            # 注意这是**顺序臂**，不是并行臂；最终评测仍用 run_gate_g3_real 的并行两臂。
            report.cover_delta = len(solved) - len(self._solved_prev)
        self.log(f"[round {cfg.round_index}] 解出目标 {len(solved)}/{len(targets)}"
                 + (f"；跨轮 cover 增量 {report.cover_delta}" if report.cover_delta is not None else ""))
        self._solved_prev = solved

        # ② 需求
        demand_report = self.demand(traces)
        report.demand = {k: demand_report[k] for k in
                         ("traces", "verified_traces", "signatures", "buckets", "demand_count")}
        demand_sigs = [e["sig"] for e in demand_report.get("top_demand", [])][: cfg.demand_limit]

        # ③ 猜想（条件化在未解目标 + 需求 + 库范例）
        unsolved = [t for t in targets if t["id"] not in solved] or targets
        candidates = self.conjecture(unsolved, demand_sigs, lib_for_prompt)

        # ④ 判据层
        survivors = self.screen(candidates, library, report)

        # ⑤⑥⑦ 求解 / 验证 / 软分
        verified = self.prove_verify_measure(survivors, report)

        # ⑧⑨ 选择 + 物化入库（记忆注入在下一轮自动生效）
        selected, gains = self.select(verified, len(library))
        written = self.commit(selected)
        report.selected = [{"stmt": c["stmt"], "target": c["target"],
                            "proof_steps": c.get("proof_steps")} for c in selected]
        library_after = load_library(self.library_path)
        report.library_after = [row["stmt"] for row in library_after]
        report.funnel["library_written"] = written
        report.funnel["library_size"] = len(library_after)
        report.funnel["greedy_gains"] = gains
        report.funnel["lean_batches"] = self._batches
        report.timing_s = time.perf_counter() - started
        self.log(f"[round {cfg.round_index}] 入库 {written} 条 → 库 {len(library_after)} 条；"
                 f"漏斗 {report.funnel}")
        return report

    def run(self, rounds: int, on_round=None) -> list[RoundReport]:
        reports: list[RoundReport] = []
        for i in range(rounds):
            self.config.round_index = i
            report = self.run_round()
            reports.append(report)
            if on_round is not None:
                on_round(report)
        return reports


def summarize(reports: list[RoundReport]) -> dict:
    """多轮总览：每轮的解出数、库大小、漏斗关键项。"""
    return {
        "rounds": len(reports),
        "per_round": [
            {
                "round": r.round_index,
                "targets_solved": len(r.solved_targets),
                "cover_delta": r.cover_delta,
                "library_size": len(r.library_after),
                "candidates": r.funnel.get("candidates", 0),
                "passed_hard_gates": r.funnel.get("passed_hard_gates", 0),
                "verified": r.funnel.get("verified", 0),
                "library_written": r.funnel.get("library_written", 0),
                "timing_s": round(r.timing_s, 1),
            }
            for r in reports
        ],
    }

