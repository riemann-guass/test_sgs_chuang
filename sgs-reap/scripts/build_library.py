"""阶段 C：候选引理 → 硬门 → 求解 → 验证 → 物化 → 入库。

这是 SG-Lean 里 **G 的第一次真正接线**。在此之前 `Trivial` / `Novelty` / `Materialize`
三个模块都实现了、也各自有测试，但**没有任何 harness 调用过它们**（审计结论），
于是"硬门"实际上不存在——阶段 B 就吃过这个亏：候选全是抄需求，门检照样 15/15 通过。

漏斗（每一步的淘汰数都进报告，不允许静默丢弃）：

    Phase B 的候选（experiments/runs/conj_*/candidates.jsonl）
      ↓ ① Gate.check        —— 是不是合法命题
      ↓ ② Trivial.isTrivial —— 平凡则淘汰（simp/decide/aesop 秒杀）
      ↓ ③ Novelty.isNew     —— 与目标集/已接受候选等价则淘汰
      ↓ ④ /solve            —— Solver 生成 k 篇证明
      ↓ ⑤ Verify.verify     —— 至少一篇过 kernel 终检才算"可证"
      ↓ ⑥ Materialize.emit  —— 落成有名常量（写 Lean 文件）+ 编译校验
      ↓ ⑦ sgsr/pipeline/library.py —— 写库（带 constants / proof_steps 等统计字段）

用法：

    # 离线（假服务）：候选用假服务的固定输出，除了链路还顺带验证"平凡门真的会拦人"
    python scripts\build_library.py --limit 2 --n 2 --endpoint http://127.0.0.1:8765/solve

    # 真跑：候选来自阶段 B 的真实产物，证明来自真代理
    python scripts\build_library.py --candidates experiments\\runs\\conj_demand_XXX\\candidates.jsonl ^
           --endpoint http://127.0.0.1:8770/solve --k 3

判定：

* `pass`：漏斗跑通，且**至少入库 1 条**（候选过了全部三件硬门 + 被证明 + 物化成功）；
* `fail_not_written`：有候选过了验证，却一条都没写进库（来源守卫拦下、或库内重复）
  ——**这是装置问题，不是"候选太弱"**，以前这里会误报成 pass；
* `fail_name_mismatch`：物化时发现有条目缺 `name`（名字与库行不一致），
  下一轮提示词里的名字将在 Lean 环境里不存在；
* `pass_no_survivor`：漏斗跑通但一条都没活下来（可能是候选太弱，也可能是硬门太严）——
  这不算失败，**漏斗数字本身就是要的产出**；
* `fail_pipeline`：链路本身坏了（例如所有候选连门检都过不了）。
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgsr.utils.http_client import soft_post_json  # noqa: E402
from sgsr.pipeline.library import add_many, load as load_library, name_for  # noqa: E402
from sgsr.verification.client import LeanServer, budget_for_jobs  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
RUNS = ROOT / "experiments" / "runs"
# 物化目标放在 **lib 内部**，这样 `import SgsLean.GeneratedLibrary` 能被解析
# （阶段 D 的两臂对照要求"库真的在 Lean 环境里可用"，而不只是 Python 里一行 JSON）。
GENERATED = ROOT / "sgslean" / "SgsLean" / "GeneratedLibrary.lean"
DEFAULT_CANDIDATES = RUNS / "conj_demand_20260920T042755Z" / "candidates.jsonl"
DEFAULT_SOLVE = "http://127.0.0.1:8765/solve"


def http_post(url: str, payload: dict, timeout: float = 300.0) -> dict:
    """软失败 POST。HTTP 实现统一在 `sgsr/models/http.py`（以前这里有第 N 份副本）。"""
    return soft_post_json(url, payload, timeout=timeout)


def solve(endpoint: str, statement: str, k: int) -> tuple[list[str], dict]:
    payload = http_post(endpoint, {"statement": statement, "num_samples": k})
    if isinstance(payload.get("error"), dict):
        # 端点故障与"模型没出候选"必须分开：返回空列表会让报告显示"0 条可证"。
        return [], {**payload, "backend_error": payload["error"]}
    proofs = [p.get("proof", "") for p in (payload.get("proofs") or [])]
    return [p for p in proofs if p.strip()], payload.get("meta") or {}


def load_candidates(path: Path, limit: int) -> list[dict]:
    """把 Phase B 的 `candidates.jsonl` 摊平成候选列表。"""
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][:limit]
    out: list[dict] = []
    for row in rows:
        for candidate in row.get("candidates") or []:
            out.append(
                {
                    "key": f"{row['target']}#{candidate['index']}",
                    "target": row["target"],
                    "target_statement": row["statement"],
                    "stmt": candidate["type"],
                }
            )
    return out


def resolve(path_str: str) -> Path:
    """把用户给的相对路径解析成绝对路径（报告里要算相对仓库根的路径）。"""
    path = Path(path_str)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 C：候选 → 硬门 → 求解 → 验证 → 物化 → 入库")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--against-all-targets", action="store_true",
                        help="新颖性对比基准加上整份 hard 目标集（默认只与**父目标**比，见下）")
    parser.add_argument("--endpoint", default=DEFAULT_SOLVE, help="/solve 的 URL")
    parser.add_argument("--library", default=str(ROOT / "experiments" / "library.jsonl"))
    parser.add_argument("--source-corpus", default="C1",
                        help="来源语料标识（C/C1/C2/C3）。缺它 `add_many` 会直接拒收——"
                             "库中绝不允许出现无法事后审计来源的引理。")
    parser.add_argument("--limit", type=int, default=2, help="用几条目标（每目标取全部候选）")
    parser.add_argument("--k", type=int, default=3, help="每条候选让 Solver 出几篇证明")
    parser.add_argument("--imports", default="Mathlib")
    parser.add_argument("--skip-compile", action="store_true", help="跳过物化文件的编译校验")
    parser.add_argument("--from-library", action="store_true",
                        help="不跑漏斗，直接把 --library 里已有的条目重新物化"
                             "（改物化路径/重建 olean 时用，省掉重跑求解）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    cand_path = resolve(args.candidates)
    library_path = resolve(args.library)
    # `--from-library` 只需要库文件，不需要候选文件
    if not args.from_library and not cand_path.exists():
        print(f"[lib] 候选文件不存在：{cand_path}")
        return 1
    candidates = [] if args.from_library else load_candidates(cand_path, args.limit)
    if not candidates and not args.from_library:
        print("[lib] 没有候选")
        return 1
    # 新颖性的对比基准：**父目标**（设计里 Novelty 拦的就是"重述父目标"与"重述库中已有引理"）。
    # 早期版本把整份 hard 目标集（37 条大语句）都塞进 `against`，结果是：
    # 每条 novelty 作业要逐条 elaborate 37 条 miniF2F 语句，batch 预算被吃光，
    # 后续作业集体报 `exception`（错误码还是极具误导性的 "maximum number of heartbeats (5800)"，
    # 那个 5800 是**剩余额度**）。这既慢又不符合设计，已改为按候选各自的父目标传。
    library_stmts: list[str] = []
    if library_path.exists():
        library_stmts = [
            json.loads(line)["stmt"]
            for line in library_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    extra_against: list[str] = []
    if args.against_all_targets:
        extra_against = [
            json.loads(line)["statement"]
            for line in (DATA / "targets_hard.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    print(
        f"[lib] 候选 {len(candidates)} 条（来自 {cand_path.name}）；"
        f"新颖性基准 = 父目标 + 库 {len(library_stmts)} 条 + 额外 {len(extra_against)} 条；端点 {args.endpoint}"
    )

    started = time.perf_counter()
    funnel: dict[str, int] = collections.Counter()
    reasons: dict[str, int] = collections.Counter()
    kept: list[dict] = []

    if args.from_library:
        rows = [
            json.loads(line)
            for line in library_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not rows:
            print(f"[lib] --from-library 但库为空：{library_path}")
            return 1
        entries = [
            {"stmt": row["stmt"], "proof": row["proof"], "verified": True,
             "name": str(row.get("name") or name_for(str(row["stmt"]))),
             "source": row.get("source", "library")}
            for row in rows
        ]
        with LeanServer(imports=args.imports, heartbeats=budget_for_jobs(len(entries) + 1),
                        stderr_path=RESULTS / "build_library_stderr.log") as server:
            responses = server.batch(
                [{"id": "mat", "cmd": "materialize", "path": str(GENERATED), "entries": entries}]
            )
        result = (responses.get("mat") or {}).get("result") or {}
        compile_ok = None
        if not args.skip_compile and GENERATED.exists():
            # 必须用**模块目标**：`lake build SgsLean`（库目标）实测不会把
            # `SgsLean.GeneratedLibrary` 编进去（报 208 jobs 成功，但 olean 不存在），
            # 于是处理臂的片段以 "object file ... does not exist" 直接崩——
            # 表现为"给库后全部退化"，是纯粹的假象（phase22 踩过）。
            proc = subprocess.run(
                ["lake", "build", "SgsLean.GeneratedLibrary"], cwd=str(ROOT / "sgslean"),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            compile_ok = proc.returncode == 0
            if not compile_ok:
                print(((proc.stdout or "") + (proc.stderr or ""))[-1200:])
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "from_library",
            "library": rel(library_path),
            "materialized": int(result.get("written") or 0),
            "materialize_missing_name": int(result.get("missingName") or 0),
            "generated_file": rel(GENERATED),
            "generated_compiles": compile_ok,
            "timing": {"total_s": round(time.perf_counter() - started, 1)},
        }
        out_path = Path(args.out) if args.out else RESULTS / "build_library_from_library.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[lib] 从库重新物化 {report['materialized']} 条 → {GENERATED}")
        print(f"[lib] 编译校验 {compile_ok}；报告 {out_path}")
        return 0

    # ── ①②③ 硬门：一次批处理（check + trivial + novelty 三类作业全发，省导入）
    # 预算随批大小放大：心跳是**按 command 累计**的，固定预算会让批次尾部集体报 exception
    # （见 lean_server.py 的说明与 docs/phase20-log.md）。
    hard_gate_jobs = 3 * len(candidates)
    with LeanServer(imports=args.imports, heartbeats=budget_for_jobs(hard_gate_jobs),
                    stderr_path=RESULTS / "build_library_stderr.log") as server:
        jobs = []
        for candidate in candidates:
            key = candidate["key"]
            jobs.append({"id": f"c:{key}", "cmd": "check", "stmt": candidate["stmt"]})
            jobs.append({"id": f"t:{key}", "cmd": "trivial", "stmt": candidate["stmt"]})
            jobs.append({"id": f"n:{key}", "cmd": "novelty", "stmt": candidate["stmt"],
                         "against": [candidate["target_statement"], *library_stmts, *extra_against]})
        responses = server.batch(jobs)
        print(f"[lib] 硬门批处理完成（{len(jobs)} 条作业，frontend {server.frontend_ms_total}ms）")

        for candidate in candidates:
            key = candidate["key"]
            gate = (responses.get(f"c:{key}") or {}).get("result") or {}
            trivial = (responses.get(f"t:{key}") or {}).get("result") or {}
            novelty = (responses.get(f"n:{key}") or {}).get("result") or {}
            candidate["gate"] = gate.get("reason") or "missing"
            candidate["gate_detail"] = (gate.get("detail") or "")[:300]
            candidate["trivial"] = trivial.get("trivial")
            candidate["trivial_by"] = trivial.get("byTactic") or ""
            candidate["trivial_detail"] = (trivial.get("detail") or "")[:300]
            candidate["novel"] = novelty.get("new")
            candidate["novel_matched"] = novelty.get("matchedStmt") or ""
            candidate["novel_detail"] = (novelty.get("detail") or "")[:300]
            funnel["candidates"] += 1

            if gate.get("ok") is not True:
                funnel["rejected_gate"] += 1
                reasons[f"gate:{gate.get('reason')}"] += 1
                continue
            if trivial.get("trivial") is True:
                funnel["rejected_trivial"] += 1
                reasons[f"trivial:{trivial.get('byTactic')}"] += 1
                continue
            if novelty.get("new") is not True:
                funnel["rejected_novelty"] += 1
                reasons["novelty:duplicate"] += 1
                continue
            funnel["passed_hard_gates"] += 1
            kept.append(candidate)

    # ── ④ 求解（API）+ ⑤ 验证
    verified: list[dict] = []
    for candidate in kept:
        proofs, meta = solve(args.endpoint, candidate["stmt"], args.k)
        candidate["proofs"] = proofs
        candidate["solve_meta"] = meta
        candidate["solve_error"] = meta.get("error") if isinstance(meta, dict) else None

    verify_jobs = []
    for candidate in kept:
        for idx, proof in enumerate(candidate["proofs"]):
            verify_jobs.append({"id": f"v:{candidate['key']}:{idx}", "cmd": "verify",
                                "stmt": candidate["stmt"], "proof": proof})
    verify_responses: dict[str, dict] = {}
    if verify_jobs:
        with LeanServer(imports=args.imports, heartbeats=budget_for_jobs(len(verify_jobs)),
                        stderr_path=RESULTS / "build_library_stderr.log") as server:
            verify_responses = server.batch(verify_jobs)
            print(f"[lib] 验证批处理完成（{len(verify_jobs)} 条，frontend {server.frontend_ms_total}ms）")

    for candidate in kept:
        ok_proof = None
        verdicts = []
        for idx, proof in enumerate(candidate["proofs"]):
            result = (verify_responses.get(f"v:{candidate['key']}:{idx}") or {}).get("result") or {}
            verdicts.append({"index": idx, "ok": result.get("ok") is True, "reason": result.get("reason")})
            if result.get("ok") is True and ok_proof is None:
                ok_proof = proof
        candidate["verdicts"] = verdicts
        funnel["proof_candidates"] += len(candidate["proofs"])
        if ok_proof is None:
            funnel["rejected_unprovable"] += 1
            reasons["solve:no_valid_proof"] += 1
            continue
        candidate["proof"] = ok_proof
        funnel["verified"] += 1
        verified.append(candidate)

    # ── ⑥ 物化 + ⑦ 入库
    written_names: list[str] = []
    compile_ok = None
    if verified:
        # 先把本次验证过的条目追加进库（`add_many` 按语句去重）
        written = add_many(
            library_path,
            [
                {"stmt": c["stmt"], "proof": c["proof"], "verified": True,
                 # 来源三件套是**强制字段**（`library._check_source`）：缺 `source_target`
                 # 或 `source_corpus` 会被直接拒收。这条脚本以前正好缺它们，
                 # phase26 收紧守卫之后它就再也写不进任何东西了——却仍然报 `pass`。
                 "source": f"cand:{c['key']}",
                 "source_target": c.get("target", ""),
                 "source_corpus": args.source_corpus,
                 "constants": c.get("constants", []), "uses": [], "delta_len": None,
                 "reuse": 0, "reuse_targets": [], "exposures": 0,
                 "added_round": 0}
                for c in verified
            ],
        )
        funnel["library_written"] = written
        funnel["library_rejected"] = len(rejected)
        for row in rejected[:5]:
            reasons[f"add_many:{row['reason']}"] = reasons.get(f"add_many:{row['reason']}", 0) + 1
        # **物化整库，而不是只物化本次的新条目**：`Materialize.emit` 是**重写**整个文件，
        # 只传新条目会让上一轮的引理从 `GeneratedLibrary.lean` 里消失，
        # 而 Python 侧的 `library.jsonl` 还在——两边不一致，处理臂的"库可用"就成了假的。
        # 写回库时必须用 `add_many` 的返回值判断，而不是假设"一定写进去了"：
        # 缺来源字段/未验证/重复的条目会被拒收，静默假设会让报告与磁盘不一致。
        full_library = load_library(library_path)
        full_entries = [
            {"stmt": row["stmt"], "proof": row["proof"], "verified": True,
             "name": str(row.get("name") or name_for(str(row["stmt"]))),
             "source": row.get("source", "library")}
            for row in full_library
        ]
        with LeanServer(imports=args.imports, heartbeats=budget_for_jobs(len(full_entries) + 1),
                        stderr_path=RESULTS / "build_library_stderr.log") as server:
            responses = server.batch(
                [{"id": "mat2", "cmd": "materialize", "path": str(GENERATED), "entries": full_entries}]
            )
        result2 = (responses.get("mat2") or {}).get("result") or {}
        written_names = list(result2.get("names") or [])
        funnel["materialized"] = int(result2.get("written") or 0)
        funnel["library_size_after"] = int(result2.get("written") or 0)
        funnel["materialize_missing_name"] = int(result2.get("missingName") or 0)
        if not args.skip_compile:
            proc = subprocess.run(
                ["lake", "build", "SgsLean.GeneratedLibrary"],
                cwd=str(ROOT / "sgslean"),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            compile_ok = proc.returncode == 0
            if not compile_ok:
                print("[lib] 物化文件编译失败（`lake build SgsLean.GeneratedLibrary` 非零退出）：")
                print(((proc.stdout or "") + (proc.stderr or ""))[-1200:])

    elapsed = time.perf_counter() - started
    if funnel["candidates"] > 0 and funnel["rejected_gate"] == funnel["candidates"]:
        verdict = "fail_pipeline"
    elif funnel.get("materialize_missing_name", 0) > 0:
        # 名字与库行不一致 ⟹ 下一轮提示词里给的名字在环境里不存在（假"库没用"）。
        verdict = "fail_name_mismatch"
    elif funnel.get("library_written", 0) > 0:
        verdict = "pass"
    elif funnel.get("verified", 0) > 0:
        # 有验证过的候选却一条都没入库：这是**守卫拦住了写入**，不是"候选太弱"。
        # 旧判定在这里给 pass，把"装置坏了"报成了成功。
        verdict = "fail_not_written"
    else:
        verdict = "pass_no_survivor"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates_file": rel(cand_path),
        "endpoint": args.endpoint,
        "k": args.k,
        "funnel": dict(funnel),
        "reasons": dict(reasons),
        "materialized_names": written_names,
        "generated_file": str(GENERATED.relative_to(ROOT)) if verified else None,
        "generated_compiles": compile_ok,
        "library": rel(library_path),
        "verdict": verdict,
        "criterion": "入库 ≥1 条才 pass；验证过但一条没写进库 = fail_not_written；"
                     "名字与库行不一致 = fail_name_mismatch",
        "timing": {"total_s": round(elapsed, 1)},
        "detail": [
            {k: c.get(k) for k in ("key", "target", "stmt", "gate", "gate_detail", "trivial",
                                   "trivial_by", "trivial_detail", "novel", "novel_matched",
                                   "novel_detail", "verdicts")}
            for c in candidates
        ],
    }
    out_path = Path(args.out) if args.out else RESULTS / f"build_library_n{args.limit}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[lib] 漏斗：{dict(funnel)}")
    print(f"[lib] 淘汰原因：{dict(reasons)}")
    if verified:
        print(f"[lib] 物化 {funnel.get('materialized', 0)} 条 → {GENERATED.name}；编译校验 {compile_ok}")
    print(f"[lib] 判定：{verdict}；报告 {out_path}")
    if verdict in ("fail_not_written", "fail_name_mismatch"):
        print("[lib] 提示：以上两条都表示**装置**有问题（来源守卫/名字），"
              "不是模型能力问题；请先修装置再重跑。")
    return 0 if verdict in ("pass", "pass_no_survivor") else 1


if __name__ == "__main__":
    raise SystemExit(main())
