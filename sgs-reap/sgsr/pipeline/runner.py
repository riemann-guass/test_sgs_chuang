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
from sgsr.pipeline.coverage import evict as coverage_evict
from sgsr.pipeline.coverage import select_by_reuse
from sgsr.pipeline.demand import mine as mine_demand
from sgsr.pipeline.library import add_many, assert_clean_sources, load as load_library
from sgsr.pipeline.retrieval import symbols
from sgsr.data.schema import candidate_id, normalize_sig, target_of, trace_from_job


def _verify_ok(result: dict) -> bool:
    """判定"这条候选的证明过没过内核"。

    **协议有两套字段名**：`verify` 命令回 `ok`（来自 `VerifyResult.ok`），
    `dependencies` 命令回 `verified`（来自 `Measure.Dependency` 的字段）。
    闭环用 `dependencies` 跑验证以省一次 Mathlib 导入，因此这里必须容忍两种写法——
    phase25 的审计发现旧代码只检查 `result["ok"]`，于是**通过验证的候选也被判成不可证**，
    库根本长不大（P0 第 3 条）。

    两种都缺失时返回 `None`（而不是 `False`）：那是协议错误，要与"判定为假"分开记。
    """
    if "ok" in result:
        return result.get("ok") is True
    if "verified" in result:
        return result.get("verified") is True
    return None


# ─────────────────────────── 数据角色 ───────────────────────────


ROLE_CURRICULUM = "curriculum"
ROLE_DEV = "dev"
ROLE_TEST = "test"

#: 仓库根（`sgs-reap/`）。禁止路径以此为基准解析，**不能**以"候选文件的父目录"
#: 为基准——那样把数据放在别的目录里就绕过了（实测踩过）。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 这些文件**永远**不许当建库来源，无论调用方怎么改名（`docs/data-protocol.md` 硬约束 1）。
#: 用解析后的绝对路径比对，而不是文件名里找子串——旧守卫只看名字里有没有 `minif2f`，
#: 把文件复制成别的名字就能绕过（审计 P0 第 5 条）。
FORBIDDEN_ROLE_PATHS = (
    REPO_ROOT / "data" / "minif2f_valid.jsonl",
    REPO_ROOT / "data" / "minif2f_test.jsonl",
)


def _forbidden_hashes() -> dict[str, str]:
    """禁止数据集的内容指纹（sha256）。

    **为什么不只比路径**：把 D 复制到工作区外的另一个目录、改个名字，路径比对就失效了
    （实测：`assert_buildable` 只比路径时，复制品照样过关）。内容指纹是最后一道——
    只要内容还是那份 D/T，无论叫什么名字、放在哪里都拒。

    D/T 是冻结的只读数据，所以按内容判不会误伤"以后新写的课程集"。
    """
    import hashlib

    out: dict[str, str] = {}
    for path in FORBIDDEN_ROLE_PATHS:
        try:
            out[hashlib.sha256(path.read_bytes()).hexdigest()] = path.name
        except OSError:
            continue
    return out


FORBIDDEN_ROLE_HASHES = _forbidden_hashes()


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
        """只有 C 允许进库/挖需求。**这个方法必须被真的调用**（审计 P0 第 5 条）。"""
        if self.role != ROLE_CURRICULUM:
            raise DataRoleError(
                f"{self.path.name} 的角色是 {self.role}；"
                "按 docs/data-protocol.md，只有 curriculum 可以进库/挖需求"
            )
        resolved = self.path.resolve()
        for forbidden in FORBIDDEN_ROLE_PATHS:
            if resolved == forbidden.resolve():
                raise DataRoleError(
                    f"{resolved} 是 D/T 数据集（{forbidden.name}）——改名也不能当建库来源"
                )
        # 内容指纹：换了目录、换了名字但内容还是 D/T 的，一样拒
        if FORBIDDEN_ROLE_HASHES:
            import hashlib

            try:
                digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except OSError:
                digest = ""
            if digest in FORBIDDEN_ROLE_HASHES:
                raise DataRoleError(
                    f"{resolved} 的内容与 D/T 数据集 {FORBIDDEN_ROLE_HASHES[digest]} 完全相同"
                    "——建库来源按**来源**判定，不看文件名与路径"
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
    #: 提示词预算（token）：选择层的约束口径（规格 5.5 节默认 1200）
    ctx_lemma_tokens: int = 1_200
    #: 复用门槛：被至少这么多**不同目标**引用过才准入（与 CostPerReusable 口径一致）
    reuse_threshold: int = 2
    #: 僵尸淘汰：`reuse=0` 且入库超过这么多轮 → 冷存
    evict_after: int = 3
    #: 本库的来源语料标识（写进每条引理的 `source_corpus`，供事后审计）
    source_corpus: str = "C1"
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
    #: 选择层诊断（密度贪心 vs 最优单条、预算用量、被门槛拦下的条数）
    selection: dict = field(default_factory=dict)
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
            "selection": self.selection,
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
        `attempts` 里的 `id` 是**候选作业**标识、`target` 是**数学目标**标识——
        两者必须分开（见 `sgsr/data/schema.py` 的 P0 说明），否则需求统计会把
        一条目标的 k 篇候选当成 k 个不同目标。
        """
        attempts: list[dict] = []
        for target in targets:
            proofs = solve(target["statement"], self.config.k_solve,
                           self.config.solve_endpoint, library=library)
            for idx, proof in enumerate(proofs):
                attempts.append({
                    "id": candidate_id(f"c:{target['id']}", idx),
                    "target": target["id"],
                    "candidate": target["id"],
                    "statement": target["statement"],
                    "proof": proof,
                })
        traces: list[dict] = []
        if attempts:
            with self.lean_server_factory(len(attempts)) as server:
                jobs = [{"id": a["id"], "target": a["target"], "cmd": "trace",
                         "stmt": a["statement"], "proof": a["proof"]} for a in attempts]
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
                    "key": candidate_id(target["id"], int(candidate["index"])),
                    "target": target["id"],                        # 父目标（供 parent_cover 用）
                    "target_statement": target["statement"],
                    "stmt": candidate["type"],
                })
        return out

    # ---- ④ 判据层：门检 + 硬门 ----
    def screen(self, candidates: list[dict], library: list[dict], report: RoundReport) -> list[dict]:
        """门检 + 硬门（非平凡 ∧ 新颖）+ **相关度**。

        相关度过滤（规格 5.3 节）：候选必须提到父目标里出现过的至少一个符号，
        否则记 `fail_irrelevant` 淘汰。它是**廉价文本代理**而不是语义判据——
        phase19 踩过"候选原样抄需求"的坑，光靠提示词约束不住，必须有硬门。
        """
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
            # 相关度硬门：先做（免费），再做门检与硬门（要 Lean）
            parent_symbols = symbols(str(cand.get("target_statement", "")))
            if parent_symbols and not (symbols(cand["stmt"]) & parent_symbols):
                report.funnel["rejected_irrelevant"] = report.funnel.get("rejected_irrelevant", 0) + 1
                report.reasons["relevance:no_symbol_overlap"] = \
                    report.reasons.get("relevance:no_symbol_overlap", 0) + 1
                continue
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
            last_reason = ""
            for i, proof in enumerate(cand["proofs"]):
                result = (responses.get(f"v:{cand['key']}:{i}") or {}).get("result") or {}
                verdict = _verify_ok(result)
                if verdict is None:
                    # 协议错误：既没有 ok 也没有 verified。这与"判定为假"不同，
                    # 必须单列，否则工具坏了会伪装成"模型证不出"。
                    report.reasons["protocol:missing_ok_or_verified"] = \
                        report.reasons.get("protocol:missing_ok_or_verified", 0) + 1
                    last_reason = "protocol_error"
                    continue
                if verdict and ok_proof is None:
                    ok_proof = proof
                    constants = list(result.get("constants") or [])
                elif not verdict:
                    last_reason = str(result.get("reason") or "")
            if ok_proof is None:
                report.funnel["rejected_unprovable"] = report.funnel.get("rejected_unprovable", 0) + 1
                key = f"solve:{last_reason or 'no_candidate'}"
                report.reasons[key] = report.reasons.get(key, 0) + 1
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
    def select(
        self,
        verified: list[dict],
        library: list[dict],
        traces: list[dict],
    ) -> tuple[list[dict], list[float], dict]:
        """N2：**按复用判据**选引理（规格 5.5 节的密度贪心）。

        这是项目唯一的方法性改动，所以这里必须用真判据而不是父目标代理：

        * `reuse(l)` = l 被多少个**不同目标**的**通过验收的**证明实际引用；
        * 只统计**通过验收**的证明（`attempts` 里过内核的那些），
          失败的尝试里出现某个名字不算复用——否则模型乱写名字就能刷分；
        * 引用从 Lean 侧抽出的**常量集合**（`dependencies` 的 `constants`）里取，
          不是对证明文本做子串搜索。

        本轮的测量范围（诚实地写进报告）：`reuse` 用**本轮**的验收证明算，
        `reuse_targets` 也只含本轮引用过它的目标。这与规格 5.5 节"在 W_sel 上、
        给库条件下"的完整定义差一项——那需要两臂测量（`scripts/run_gate_g3_real.py`）。
        差别是**口径更窄**（不含历史轮次、不含在线任务），不会虚高。
        """
        # 库条目的物化名：`Materialize.emit` 按**顺序**命名 `sgs_lem_<i+1>`，
        # 所以名字必须按库内顺序推，不能按语句内容猜。
        named_library = [
            dict(row, name=f"sgs_lem_{index + 1}") for index, row in enumerate(library)
        ]
        name_by_stmt = {normalize_sig(row["stmt"]): row["name"] for row in named_library}

        # 按目标去重后统计引用：同一目标的多篇候选引用同一条引理只记 1 次
        cited_by_target: dict[str, set[str]] = {}
        for row in verified:
            target = str(row.get("target") or "")
            for constant in row.get("constants") or []:
                leaf = str(constant).rsplit(".", 1)[-1]
                if not leaf.startswith("sgs_lem_"):
                    continue
                cited_by_target.setdefault(leaf, set()).add(target)

        pool: list[dict] = []
        for row in named_library:
            cited = cited_by_target.get(row["name"], set())
            pool.append(dict(row, reuse=len(cited), reuse_targets=sorted(cited)))

        # 候选池 = 已有库条目 + 本轮验证通过的候选（后者尚未入库，reuse 为 0）
        existing_stmts = {normalize_sig(row["stmt"]) for row in named_library}
        for cand in verified:
            if normalize_sig(cand["stmt"]) in existing_stmts:
                continue
            pool.append({
                "stmt": cand["stmt"],
                "name": f"candidate:{cand['key']}",
                "reuse": 0,
                "reuse_targets": [],
                "candidate": cand,
            })

        chosen, gains, diagnostics = select_by_reuse(
            pool,
            ctx_budget=self.config.ctx_lemma_tokens,
            threshold=self.config.reuse_threshold,
            count_budget=max(0, self.config.library_budget - len(library)),
        )
        # 只有"本轮新验证、且被选中"的候选才入库；已在库里的条目不再重复写
        new_entries = [row["candidate"] for row in chosen if "candidate" in row]
        return new_entries, gains, diagnostics

    def evict(self, report: RoundReport) -> list[dict]:
        """僵尸淘汰：`reuse=0` 且入库超过 R_evict 轮的引理移入冷存（不删除）。"""
        library = load_library(self.library_path)
        kept, evicted = coverage_evict(library, self.config.round_index,
                                       evict_after=self.config.evict_after)
        if not evicted:
            return []
        cold_path = self.library_path.with_name(self.library_path.stem + "_cold.jsonl")
        with cold_path.open("a", encoding="utf-8") as handle:
            for row in evicted:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        # 重写整库（保留探索额度的那些）
        self.library_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept), encoding="utf-8"
        )
        report.funnel["library_evicted"] = len(evicted)
        return evicted

    # ---- ⑨ 物化 + 入库 ----
    def commit(self, selected: list[dict], report: RoundReport | None = None) -> int:
        """把选中的候选写进库 → 物化整库 → **编译**（必须点名模块目标）。

        审计的 P0 第 4 条：旧代码只调 `materialize`，既不 `lake build
        SgsLean.GeneratedLibrary`，也不检查返回。后果是下一轮提示词里给了
        `sgs_lem_i` 的名字，而环境里没有对应的 olean——模型引用时得到
        `unknown identifier`，表现成"库没用"。phase22 已经用两臂测量踩过一次，
        这里是同一条坑在闭环里的复现。
        """
        if not selected:
            return 0
        written, rejected = add_many(
            self.library_path,
            [
                {"stmt": c["stmt"], "proof": c["proof"], "verified": True,
                 "source": f"round{self.config.round_index}:{c['key']}",
                 "source_target": c.get("target", ""),
                 "source_corpus": self.config.source_corpus,
                 "constants": c.get("constants", []), "delta_len": None,
                 "proof_steps": c.get("proof_steps"),
                 "added_round": self.config.round_index}
                for c in selected
            ],
        )
        if report is not None:
            report.funnel["library_rejected"] = len(rejected)
            if rejected:
                for row in rejected[:5]:
                    key = f"commit:{row['reason']}"
                    report.reasons[key] = report.reasons.get(key, 0) + 1
        # 入库后立刻复核来源（第二道防线，见 library.assert_clean_sources）
        assert_clean_sources(self.library_path)
        if written == 0:
            return 0
        # 物化整库（Materialize.emit 是重写整个文件），并编译出 olean 供 `import` 使用
        full = [
            {"stmt": row["stmt"], "proof": row["proof"], "verified": True,
             "source": row.get("source", "library")}
            for row in load_library(self.library_path)
        ]
        with self.lean_server_factory(len(full)) as server:
            response = server.batch([{"id": "mat", "cmd": "materialize",
                                      "path": str(self.generated_path), "entries": full}])
            self._batches += 1
        materialized = (response.get("mat") or {}).get("result") or {}
        # `MaterializeResult` 的字段是 `written`/`skipped`/`names`，**没有 `ok`**
        # （审计 P0 第 4 条的同类坑：按 `ok` 判会永远失败）。协议错误时才没有 written。
        if "written" not in materialized:
            detail = json.dumps(materialized, ensure_ascii=False)[:300]
            raise RuntimeError(f"物化失败：{detail}")
        build = self.build_generated_library()
        if report is not None:
            report.funnel["generated_build_ok"] = build["ok"]
            report.funnel["generated_build_jobs"] = build.get("jobs")
            if not build["ok"]:
                report.reasons["materialize:build_failed"] = 1
        if not build["ok"]:
            raise RuntimeError(
                "物化文件编译失败——下一轮的 import 会失败，必须当轮就停下："
                f"{build.get('tail', '')[:400]}"
            )
        return written

    def build_generated_library(self, timeout_s: int = 1800) -> dict:
        """`lake build SgsLean.GeneratedLibrary`（**模块目标**，不能用库目标）。

        `lake build SgsLean` 不会编译这个模块——phase22 的假结论就是从这来的。
        返回 `{"ok", "jobs", "tail"}`，失败时把日志尾巴带回去，不吞错。
        """
        import os
        import re
        import subprocess

        lake = os.environ.get("LAKE", "lake")
        try:
            proc = subprocess.run(
                [lake, "build", "SgsLean.GeneratedLibrary"],
                cwd=str(self.generated_path.parents[1]),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "jobs": None, "tail": f"{type(exc).__name__}: {exc}"}
        output = (proc.stdout or "") + (proc.stderr or "")
        match = re.search(r"\((\d+) jobs?\)", output)
        return {
            "ok": proc.returncode == 0,
            "jobs": int(match.group(1)) if match else None,
            "tail": output[-1500:],
        }

    # ---- ⑩ 一轮 ----
    def run_round(self) -> RoundReport:
        started = time.perf_counter()
        cfg = self.config
        report = RoundReport(round_index=cfg.round_index,
                             started_at=datetime.now(timezone.utc).isoformat())
        targets = self.curriculum.batch(cfg.target_limit)
        library = load_library(self.library_path)
        # 读库即复核来源（硬约束 1 的第二道防线）；有 D/T 来源就直接停，不继续算。
        assert_clean_sources(self.library_path)
        report.library_before = [row["stmt"] for row in library]
        lib_for_prompt = [
            {"name": f"sgs_lem_{i + 1}", "stmt": row["stmt"]} for i, row in enumerate(library)
        ][: cfg.seeds_limit]

        self.log(f"[round {cfg.round_index}] 目标 {len(targets)} 条；库 {len(library)} 条")

        # ① 采轨迹（"无库"臂天然来自第一轮；之后各轮的提示词里已有上一轮的库）
        traces, attempts = self.collect(targets, lib_for_prompt)
        # trace 作业内部已经跑过 `Verify.verify`，`TraceResult.verified` 就是结论——
        # 不要再单独起一批 verify（那会多付一次 Mathlib 导入）。
        # **`target` 才是数学目标**：`id` 是候选作业标识，把 id 当目标会让"已解出的目标"
        # 与"未解目标"两个集合对不上，已解出的目标会被反复送去猜（审计 P0 第 2 条）。
        solved: set[str] = {
            str(t.get("target") or target_of(str(t.get("id"))))
            for t in traces if t.get("verified") is True
        }
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
                         ("traces", "verified_traces", "distinct_targets", "signatures",
                          "buckets", "demand_count")}
        if demand_report.get("problems"):
            report.reasons["demand:missing_target"] = len(demand_report["problems"])
        demand_sigs = [e["sig"] for e in demand_report.get("top_demand", [])][: cfg.demand_limit]

        # ③ 猜想（条件化在未解目标 + 需求 + 库范例）
        unsolved = [t for t in targets if t["id"] not in solved] or targets
        candidates = self.conjecture(unsolved, demand_sigs, lib_for_prompt)

        # ④ 判据层
        survivors = self.screen(candidates, library, report)

        # ⑤⑥⑦ 求解 / 验证 / 软分
        verified = self.prove_verify_measure(survivors, report)

        # ⑧⑨ 淘汰 + 选择 + 物化入库（记忆注入在下一轮自动生效）
        evicted = self.evict(report)
        if evicted:
            library = load_library(self.library_path)
            self.log(f"[round {cfg.round_index}] 冷存 {len(evicted)} 条僵尸引理（reuse=0）")
        selected, gains, selection = self.select(verified, library, traces)
        report.selection = selection
        written = self.commit(selected, report)
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

