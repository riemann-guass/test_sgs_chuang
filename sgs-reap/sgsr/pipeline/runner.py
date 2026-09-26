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

## 选择层现在怎么做（2026-09-22 重修）

三件事分开，各用各的量（此前挤成一个"门槛"，结果库既长不大也长不住）：

* **准入**：本轮验证通过的候选按**探索额度**入库（`coverage.exploration_admission`）。
  新引理的 `reuse` 按构造是 0，**不能**拿它当准入门槛。
* **复用测量**：`reuse(l)` = l 被多少个**不同目标**的**通过验收的**证明实际引用，
  从**目标侧**的轨迹里数（那里才给了库）。测到的值连同 `reuse_targets` 落盘。
* **淘汰**：`reuse=0` **且被给过机会（进过提示词）** 且超龄 → 冷存。没被给过机会
  的引理不淘汰——否则淘汰的是没抽到签的人。
* **提示词注入**：下一轮的提示词集合由 `coverage.select_by_reuse`（密度贪心 +
  探索期补位）在 token 预算内选出，不再是"取前 N 条"。

评测侧仍用 `proved_cover`（`scripts/run_gate_g3_real.py`）做两臂真 cover。
"""

from __future__ import annotations

import collections
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sgsr.pipeline.conjecture import generate as conjecture_generate
from sgsr.pipeline.conjecture import load_demand, load_seeds
from sgsr.client import BackendUnavailable, soft_post_json
from sgsr.pipeline.selection import cost_of, evict as coverage_evict
from sgsr.pipeline.selection import exploration_admission, reuse_table, select_by_reuse
from sgsr.pipeline.demand import mine as mine_demand
from sgsr.pipeline.library import (
    add_many,
    assert_clean_sources,
    load as load_library,
    name_for,
    write_all as write_library,
)
from sgsr.pipeline.selection import retrieve_library, symbols
from sgsr.data import candidate_id, normalize_sig, target_of, trace_from_job


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
    #: **只用于成本口径与保留判据**，不当准入门槛（新引理按构造没有复用证据）。
    reuse_threshold: int = 2
    #: 僵尸淘汰：`reuse=0` 且入库超过这么多轮 → 冷存
    evict_after: int = 3
    #: 探索额度：一轮最多让多少条"还没有复用证据"的新引理入库 / 进提示词
    exploration_slots: int = 8
    #: 提示词里最多放几条引理（与 `proxy.handle_solve` 的截断上限保持一致）
    prompt_slots: int = 16
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
    #: 准入诊断（探索额度、库容、被额度挡下的条数）
    admission: dict = field(default_factory=dict)
    #: 复用分布与下一轮提示词集合（"库到底有没有被用上"的直接证据）
    reuse: dict = field(default_factory=dict)
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
            "admission": self.admission,
            "reuse": self.reuse,
            "timing_s": round(self.timing_s, 1),
        }


# ─────────────────────────── HTTP 小工具 ───────────────────────────


def _post(url: str, payload: dict, timeout: float = 300.0) -> dict:
    """软失败 POST（错误收进 `{"error": ...}`）。HTTP 实现见 `sgsr/models/http.py`。"""
    return soft_post_json(url, payload, timeout=timeout)


def solve(statement: str, k: int, endpoint: str, library: list[dict] | None = None) -> list[str]:
    """调 `/solve` 取候选证明。

    **后端错误必须抛出去**：离线闭环以前把它吞成空列表，于是"服务挂了"与
    "模型没生成出候选"在报告里长得一模一样——这与在线侧刻意区分
    `backend_error` 的做法自相矛盾（审计点名的失真来源）。
    """
    payload: dict = {"statement": statement, "num_samples": k}
    if library:
        payload["library"] = library
    response = _post(endpoint, payload)
    error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict):
        raise BackendUnavailable(
            f"{error.get('code', 'error')}: {error.get('message', '')}"
        )
    if not isinstance(response, dict):
        raise BackendUnavailable(f"/solve 响应不是对象：{str(response)[:200]}")
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
        materialize_factory=None,
        log=print,
    ) -> None:
        self.curriculum = curriculum
        self.config = config
        self.workdir = workdir
        self.library_path = library_path
        self.generated_path = generated_path
        self.lean_server_factory = lean_server_factory
        #: 写物化文件时用的服务工厂：**import 列表要排除 `SgsLean.GeneratedLibrary`**。
        #: 生成文件的头部照抄 `SGSLEAN_IMPORTS` 时，处理臂会把生成库自己写进去 ⟹
        #: 自我循环导入 ⟹ 编译失败（实测：闭环第一轮就停在这里）。
        #: 不传就退回主工厂（那时调用方要保证 import 里没有生成库）。
        self.materialize_factory = materialize_factory or lean_server_factory
        self.log = log
        self._solved_prev: set[str] | None = None
        # 每轮会起几次 `lean` 子进程（= 几次 Mathlib 导入）。导入是本项目最贵的固定成本，
        # 必须把它记录进报告，否则"一轮要多久"无法解释也无法优化。
        self._batches = 0

    # ---- ① 采轨迹 ----
    def collect(self, targets: list[dict], library: list[dict], report: RoundReport | None = None,
                exposed: set[str] | None = None
                ) -> tuple[list[dict], list[dict], list[dict]]:
        """在目标集上跑 Solver → 记录每条候选证明的轨迹（含子目标签名）。

        返回 `(traces, attempts, backend_errors)`：traces 供需求挖掘**与复用测量**；
        attempts 供诊断；backend_errors 是"装置挂了"的记录（**必须**与"模型没解出"
        分开，否则一次 503 会伪装成 0% 解出率）。

        `attempts` 里的 `id` 是**候选作业**标识、`target` 是**数学目标**标识——
        两者必须分开（见 `sgsr/data/schema.py` 的 P0 说明），否则需求统计会把
        一条目标的 k 篇候选当成 k 个不同目标。
        """
        attempts: list[dict] = []
        backend_errors: list[dict] = []
        injected: list[int] = []
        for target in targets:
            # **按目标**注入：走检索层（符号重叠 + 复用密度），而不是"全轮公用的前 N 条"。
            # 规格 2.2 的分层检索本来就该在求解前对**当前命题**取库；在线路径
            # （`prover.retrieve`）一直是这么做的，离线闭环以前没有，于是提示词里塞的是
            # 库里最早那 12 条（与当前目标无关），模型自然不会引用。
            per_target_library = (
                retrieve_library(target["statement"], library,
                                 n=self.config.prompt_slots) if library else []
            )
            prompt_items = [{"name": p.name, "stmt": p.statement} for p in per_target_library]
            injected.append(len(prompt_items))
            if exposed is not None:
                # 记录"本轮真的进过提示词"的引理名——淘汰判据里的"被给过机会"
                # 依据的就是这个集合（不是"库里有就算有过机会"）。
                exposed.update(str(p.name) for p in per_target_library)
            try:
                proofs = solve(target["statement"], self.config.k_solve,
                               self.config.solve_endpoint, library=prompt_items)
            except BackendUnavailable as exc:
                backend_errors.append({"target": target["id"], "error": str(exc)})
                continue
            for idx, proof in enumerate(proofs):
                attempts.append({
                    "id": candidate_id(f"c:{target['id']}", idx),
                    "target": target["id"],
                    "candidate": target["id"],
                    "statement": target["statement"],
                    "proof": proof,
                })
        if report is not None:
            report.funnel["prompt_injected_per_target"] = (
                round(sum(injected) / len(injected), 2) if injected else 0
            )
            report.funnel["prompt_injected_max"] = max(injected) if injected else 0
        traces: list[dict] = []
        trace_errors = 0
        if attempts:
            with self.lean_server_factory(len(attempts)) as server:
                jobs = [{"id": a["id"], "target": a["target"], "cmd": "trace",
                         "stmt": a["statement"], "proof": a["proof"]} for a in attempts]
                responses = server.batch(jobs)
                self._batches += 1
            for attempt in attempts:
                # 同样的纪律：轨迹批也可能出现"响应缺失"。那意味着这条候选的判定**未知**，
                # 不能当成"证明没过"（那会污染需求统计与复用测量），单列并跳过。
                entry = responses.get(attempt["id"])
                if entry is None or entry.get("error"):
                    trace_errors += 1
                    continue
                result = entry.get("result") or {}
                traces.append(trace_from_job(attempt, result))
            if trace_errors and report is not None:
                report.funnel["protocol_errors_trace"] = trace_errors
                report.reasons["protocol:trace"] = \
                    report.reasons.get("protocol:trace", 0) + trace_errors
        return traces, attempts, backend_errors

    # ---- ②′ 复用测量 ----
    def measure_reuse(self, traces: list[dict]) -> dict[str, set[str]]:
        """`reuse(l)` 的原始材料：`{引理名: {引用过它的不同目标}}`。

        规格 5.1 的定义是"被多少个**不同目标**的**通过验收的**证明实际引用"，
        所以：

        * 只看 `verified=true` 的轨迹（失败证明里出现名字不算复用）；
        * 按 `target` 去重（一条目标的 k 篇候选只算 1）；
        * 引用来自 Lean 侧抽出的**常量集合**（`Trace` 现在一并返回 `constants`），
          不是对证明文本做子串搜索。

        **必须用目标侧的轨迹**：目标是带着库提示词求解的；候选引理自己是"新命题"，
        求解时并没有得到库，从它那里数引用永远数出 0（这正是上一版的死结）。
        """
        cited: dict[str, set[str]] = {}
        for trace in traces:
            if trace.get("verified") is not True:
                continue
            target = str(trace.get("target") or "")
            for constant in trace.get("constants") or []:
                leaf = str(constant).rsplit(".", 1)[-1]
                if leaf.startswith("sgs_lem_"):
                    cited.setdefault(leaf, set()).add(target)
        return cited

    # ---- ② 需求 ----
    def demand(self, traces: list[dict]) -> dict:
        return mine_demand(traces)

    # ---- ③ 猜想 ----
    def conjecture(self, targets: list[dict], demand_sigs: list[str],
                   seeds: list[str]) -> tuple[list[dict], list[dict]]:
        """对每个未解目标请求候选引理。返回 `(候选, backend_errors)`。

        端点报错**必须**单独返回：以前 `ConjectureResult.error` 被直接丢弃，
        于是一次 503 在报告里表现得和"猜想器什么都没产出"一模一样。
        """
        out: list[dict] = []
        backend_errors: list[dict] = []
        for target in targets:
            result = conjecture_generate(
                self.config.conjecture_endpoint, target, demand_sigs, seeds,
                self.config.n_conjecture,
            )
            if result.error:
                backend_errors.append({"target": target["id"], "error": result.error})
                continue
            for candidate in result.candidates:
                out.append({
                    "key": candidate_id(target["id"], int(candidate["index"])),
                    "target": target["id"],                        # 父目标（供来源审计）
                    "target_statement": target["statement"],
                    "stmt": candidate["type"],
                })
        return out, backend_errors

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
        protocol_errors = 0
        for cand in candidates:
            # **协议错误 ≠ 判定为假**：服务端没回判定（响应缺失 / internal_error）时，
            # 我们**不知道**这条候选该不该过门。把它记成"门检拒绝"会让装置故障伪装成
            # "候选质量差"——实测：Lean 的 maxErrors 上限让 156 个作业只回 51 条，
            # 2/3 的候选被静默当成"没过门检"丢掉了。
            entries = [responses.get(f"{tag}:{cand['key']}") for tag in ("c", "t", "n")]
            if any(e is None or e.get("error") for e in entries):
                protocol_errors += 1
                report.reasons["protocol:screen"] = report.reasons.get("protocol:screen", 0) + 1
                continue
            gate = (entries[0] or {}).get("result") or {}
            trivial = (entries[1] or {}).get("result") or {}
            novelty = (entries[2] or {}).get("result") or {}
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
        if protocol_errors:
            report.funnel["protocol_errors_screen"] = protocol_errors
            # 超过 20% 就**当轮停下**：这种规模的静默丢失会让漏斗数字完全失真。
            if protocol_errors > 0.2 * len(candidates):
                raise RuntimeError(
                    f"硬门批有 {protocol_errors}/{len(candidates)} 条候选拿不到判定"
                    "（协议错误）——不要继续，先修装置（见 phase28 的 maxErrors 事件）"
                )
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

    # ---- ⑧ 准入 / 复用记账 / 注入集合 ----
    def select(self, verified: list[dict], library: list[dict]) -> tuple[list[dict], dict]:
        """**准入**：本轮验证通过的候选里，哪些允许入库（规格 5.5 的探索额度）。

        这里**不用** `reuse >= threshold`：新引理的 `reuse` 按构造是 0（还没有任何
        目标引用过它），拿它当准入门槛就是"要求新兵先有战功"——库永远长不大，
        这正是上一版的死锁。复用证据的作用在别处：保留/淘汰与提示词排序。
        """
        admitted, diagnostics = exploration_admission(
            verified,
            library_size=len(library),
            library_budget=self.config.library_budget,
            exploration=self.config.exploration_slots,
        )
        return admitted, diagnostics

    def prompt_set(self, library: list[dict], report: RoundReport) -> list[dict]:
        """下一轮提示词里的引理集合（token 预算下的密度贪心 + 探索期补位）。

        以前这里是 `library[:seeds_limit]`——按**位置**取前 N 条，于是库一超过
        N 条，后面的引理**永远拿不到出场机会**，`reuse` 永远是 0，然后按"reuse=0
        且超龄"被淘汰。选择层的方法性改动（密度贪心）根本没参与过注入。
        """
        chosen, gains, diagnostics = select_by_reuse(
            library,
            ctx_budget=self.config.ctx_lemma_tokens,
            threshold=self.config.reuse_threshold,
            count_budget=self.config.prompt_slots,
            round_index=self.config.round_index,
            evict_after=self.config.evict_after,
            exploration=self.config.exploration_slots,
        )
        report.selection = diagnostics
        report.funnel["greedy_gains"] = gains
        report.funnel["prompt_size"] = len(chosen)
        return chosen

    def update_library(self, report: RoundReport, cited: dict[str, set[str]],
                       prompt_names: set[str]) -> list[dict]:
        """把本轮的复用测量、曝光计数写回库，并淘汰僵尸。返回被冷存的条目。

        **必须落盘**：`reuse` 只留在内存里的话，淘汰读到的是"每条 reuse 都是 0"，
        于是所有老引理一律被杀；检索层的 `reuse/cost` 排序键也永远缺席。
        全库重写集中在这一处（`library.write_all`），不与其他写入路径并存。
        """
        library = load_library(self.library_path)
        for row in library:
            name = str(row.get("name") or name_for(str(row.get("stmt", ""))))
            row["name"] = name
            seen = set(row.get("reuse_targets") or [])
            seen |= cited.get(name, set())
            row["reuse_targets"] = sorted(seen)
            row["reuse"] = len(seen)
            row["cost_tokens"] = cost_of(row)
            if name in prompt_names:
                row["exposures"] = int(row.get("exposures") or 0) + 1
            row.setdefault("exposures", 0)
        kept, evicted = coverage_evict(library, self.config.round_index,
                                       evict_after=self.config.evict_after)
        if evicted:
            cold_path = self.library_path.with_name(self.library_path.stem + "_cold.jsonl")
            with cold_path.open("a", encoding="utf-8") as handle:
                for row in evicted:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            report.funnel["library_evicted"] = len(evicted)
        write_library(self.library_path, kept)
        report.reuse = reuse_table(kept)
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
                 "added_round": self.config.round_index,
                 # 新引理按构造 reuse=0（还没有目标引用过它），这是**真实的测量值**，
                 # 不是缺省值；它连同曝光计数一起落盘，供保留/淘汰判断。
                 "reuse": 0, "reuse_targets": [], "exposures": 0}
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
        self.materialize_library(report)
        return written

    def materialize_library(self, report: RoundReport | None = None) -> int:
        """把**当前库**物化成 `GeneratedLibrary.lean` 并编译（返回物化了多少条）。

        与 `commit` 分开是因为它有一个独立的用途：库没变、但物化文件丢了或过时
        （换机器、清过 `.lake`、手工改过库），需要单独重建一次——
        `scripts/run_round.py --materialize-only` 走的就是这条。

        物化整库（`Materialize.emit` 是重写整个文件），并编译出 olean 供 `import` 使用。
        """
        full = [
            {"stmt": row["stmt"], "proof": row["proof"], "verified": True,
             # 名字来自库行（`library.add_many` 按语句内容生成），不再按数组下标猜：
             # 淘汰会从库中间删条目，按下标命名会让剩下的引理整体改名。
             "name": str(row.get("name") or name_for(str(row["stmt"]))),
             "source": row.get("source", "library")}
            for row in load_library(self.library_path)
        ]
        if not full:
            return 0
        with self.materialize_factory(len(full)) as server:
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
        return len(full)

    def build_generated_library(self, timeout_s: int = 1800) -> dict:
        """`lake build SgsLean.GeneratedLibrary`（**模块目标**，不能用库目标）。

        `lake build SgsLean` 不会编译这个模块——phase22 的假结论就是从这来的。
        返回 `{"ok", "jobs", "tail"}`，失败时把日志尾巴带回去，不吞错。

        工作目录取 **lake 工程根**（`sgslean/`），不取"物化文件的祖父目录"：
        后者在 `--generated` 指向别处时会静默指向错误的目录，`lake build` 直接失败
        （或更糟：编译到另一个工程里）。
        """
        import os
        import re
        import subprocess

        from sgsr.lean import SGSLEAN

        lake = os.environ.get("LAKE", "lake")
        try:
            proc = subprocess.run(
                [lake, "build", "SgsLean.GeneratedLibrary"],
                cwd=str(SGSLEAN),
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
        # 提示词集合由**选择层**给出（token 预算下的密度贪心 + 探索期补位），
        # 不是 `library[:seeds_limit]`：按位置截断会让库尾部的引理永远没有出场机会，
        # 于是它们的 reuse 永远是 0，再被"reuse=0 且超龄"淘汰——淘汰的是没抽到签的人。
        # 库为空时它就是空列表（第一轮天然是"无库臂"）。
        lib_for_prompt = self.prompt_set(library, report) if library else []
        for row in lib_for_prompt:
            row.setdefault("name", name_for(str(row.get("stmt", ""))))

        self.log(f"[round {cfg.round_index}] 目标 {len(targets)} 条；库 {len(library)} 条")

        # ① 采轨迹（"无库"臂天然来自第一轮；之后各轮的提示词里已有上一轮的库）
        exposed: set[str] = set()
        traces, attempts, solve_errors = self.collect(targets, lib_for_prompt, report, exposed)
        if solve_errors:
            # 装置故障要单列：把它混进"没解出"会让解出率凭空变低。
            report.reasons["backend_error:solve"] = len(solve_errors)
            report.funnel["backend_errors_solve"] = len(solve_errors)
            self.log(f"[round {cfg.round_index}] 求解端点报错 {len(solve_errors)} 次"
                     f"（例如 {solve_errors[0]['error'][:120]}）")
        # ①′ 复用测量：l 被多少个**不同目标**的**通过验收的**证明实际引用
        cited = self.measure_reuse(traces)
        report.funnel["cited_lemmas"] = len(cited)
        report.funnel["cited_targets"] = sum(len(v) for v in cited.values())
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
        unsolved = [t for t in targets if t["id"] not in solved]
        if not unsolved:
            # 全部解出时**不再出题**：以前这里 `or targets` 会把已解出的目标再送一遍，
            # 白花一次调用，还让"覆盖增量"的语义变模糊。
            candidates, conjecture_errors = [], []
            report.reasons["conjecture:all_solved"] = 1
        else:
            # 条件化信号可能为空（第一轮没有需求、库里也没有引理）：那时出题器
            # 只剩"未解目标"这一个输入——这正是 SGS 的结构（g 条件化在未解目标上），
            # 照常出题，把"没有需求"如实记进报告即可。
            if not demand_sigs:
                report.reasons["conjecture:no_demand"] = 1
            candidates, conjecture_errors = self.conjecture(unsolved, demand_sigs, lib_for_prompt)
        if conjecture_errors:
            report.reasons["backend_error:conjecture"] = len(conjecture_errors)
            report.funnel["backend_errors_conjecture"] = len(conjecture_errors)
            self.log(f"[round {cfg.round_index}] 出题端点报错 {len(conjecture_errors)} 次")

        # ④ 判据层
        survivors = self.screen(candidates, library, report)

        # ⑤⑥⑦ 求解 / 验证 / 软分
        verified = self.prove_verify_measure(survivors, report)

        # ⑧ 复用记账 + 曝光计数 + 淘汰（写回库），再按探索额度准入
        # 曝光计数用**本轮真的被注入过的名字**（`collect` 里按目标检索得到）。
        evicted = self.update_library(report, cited, exposed)
        if evicted:
            self.log(f"[round {cfg.round_index}] 冷存 {len(evicted)} 条僵尸引理"
                     f"（reuse=0 且被给过机会）")
        library = load_library(self.library_path)
        selected, admission = self.select(verified, library)
        report.admission = admission
        # ⑨ 物化 + 入库
        written = self.commit(selected, report)
        report.selected = [{"stmt": c["stmt"], "target": c["target"],
                            "proof_steps": c.get("proof_steps")} for c in selected]
        library_after = load_library(self.library_path)
        report.library_after = [row["stmt"] for row in library_after]
        report.funnel["library_written"] = written
        report.funnel["library_size"] = len(library_after)
        report.funnel["lean_batches"] = self._batches
        report.funnel["reuse_measured"] = {k: sorted(v) for k, v in sorted(cited.items())}
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
                "reusable": (r.reuse.get("buckets", {}) or {}).get("reusable", 0),
                "cited_targets": r.funnel.get("cited_targets", 0),
                "prompt_size": r.funnel.get("prompt_size", 0),
                "timing_s": round(r.timing_s, 1),
            }
            for r in reports
        ],
    }

