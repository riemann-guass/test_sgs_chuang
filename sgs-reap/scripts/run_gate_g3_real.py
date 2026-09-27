"""闸门 G3（真定义）：`proved_cover(S) − proved_cover(∅)`。

旧的 G3 是**空转**的：候选池被定义成"目标轨迹签名集合的并集"，于是每条候选按构造至少覆盖
一个目标，贪心比 1.000、子模性 0 违例都是数学必然，跟"引理有没有用"无关
（见 `docs/phase17-log.md` 之前的审计）。本脚本换成真定义：

    在**裸解不出的目标集**（`data/targets_hard.jsonl`）上，同一个 Solver、同样的 k：

      基线臂：不给库            → proved_cover(∅) = 被证出的目标数
      处理臂：给库（提示词列出引理名 + 环境里真的 import 了它们）
                                → proved_cover(S) = 被证出的目标数

      增益 = proved_cover(S) − proved_cover(∅)

两个臂的差别**只有库**：同一批目标、同一个模型、同样的 k、同样的验证预算。
处理臂的验证环境额外 `import SgsLean.GeneratedLibrary`，否则证明里引用 `sgs_lem_i` 会报
`unknown identifier`。

用法：

    # 先起 MODELS\\proxy.py（需网络权限）
    python scripts\run_gate_g3_real.py --limit 12 --k 2 --endpoint http://127.0.0.1:8770/solve

工作负载（`--select`）：

* `nearmiss`（默认）：修正后 G1 里 `0 < solve_rate < 1` 的目标——**偶尔能解出**，
  这是 `cover` 唯一有信号的地方；
* `easy`：`solve_rate = 1` 的目标，用来验证"给库不会让成绩变差"；
* `mixed`：近失手 + 稳定可解；
* `hard`：`solve_rate = 0` 的目标（**不要用它测增益**——phase21 实测两臂都是 0，
  因为集合的定义就是"Solver 最不擅长的那些"，留不出余量）。

度量：**评分制**（不是 0/1 翻转）：

    cover(S) = Σ_w [ solve_rate_with(S, w) − solve_rate_without(S, w) ]
    solve_rate = 验证通过的证明数 / k     （没生成出证明也算失败，所以分母固定是 k）

近失手集合上 0/1 翻转太稀疏（k=2 时一条目标非 0 即 0.5），评分制才有分辨率。

判定（跑之前定死）：

* `pass`：评分制增益 > 0；
* `no_gain`：增益 = 0——**有效结果**；
* `negative`：增益 < 0（给库反而更差）——同样要如实报告，多半是采样噪声或提示词副作用；
* `fail_pipeline`：验证层大面积协议错误（链路坏了）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgsr.pipeline.library import active_rows, name_for  # noqa: E402
from sgsr.pipeline.prover import solve_candidates  # noqa: E402
from sgsr.client import BackendUnavailable  # noqa: E402
from sgsr.lean import LeanServer, budget_for_jobs, preflight_in_session  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
RUNS = ROOT / "experiments" / "runs"
DEFAULT_SOLVE = "http://127.0.0.1:8770/solve"
LIBRARY_IMPORTS = "Mathlib,SgsLean.GeneratedLibrary"
BASE_IMPORTS = "Mathlib"


def load_library(path: Path) -> list[dict]:
    """读 `experiments/library.jsonl`，按 `Materialize` 的命名规则补上 `sgs_lem_<i+1>`。

    命名必须与物化时一致，否则提示词里给出的名字在 Lean 环境里不存在。
    """
    if not path.exists():
        return []
    rows = active_rows(path)
    # 名字**读库行里的 `name`**（由 `library.add_many` 按语句内容生成）。
    # 按下标推名字会在"库被淘汰过"之后与 Lean 环境里的常量对不上，
    # 于是提示词给的名字不存在、模型一引用就 unknown identifier——表现成"库没用"。
    return [
        row | {"name": str(row.get("name") or name_for(str(row["stmt"])))}
        for row in rows
    ]


def load_workload(args, limit: int) -> list[dict]:
    """按难度选工作负载（见文件头）。难度取自修正后的 G1 报告。"""
    if args.select == "hard":
        rows = [
            json.loads(line)
            for line in Path(args.targets).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [{"id": row["id"], "statement": row["statement"]} for row in rows][:limit]

    report = json.loads(Path(args.g1_report).read_text(encoding="utf-8"))
    per_target = (report.get("corrected_g1") or {}).get("per_target") or {}
    if not per_target:
        print(f"[g3r] G1 报告里没有 per_target：{args.g1_report}")
        return []

    def bucket(rate: float) -> str:
        if rate == 0:
            return "hard"
        if rate >= 1:
            return "easy"
        return "nearmiss"

    wanted = {"nearmiss"} if args.select == "nearmiss" else (
        {"easy"} if args.select == "easy" else {"nearmiss", "easy"}
    )
    # 近失手优先按"越接近能解出越好"排（solve_rate 高的先来），这样样本最有信息量
    picked = [
        (tid, entry) for tid, entry in per_target.items() if bucket(float(entry["solve_rate"])) in wanted
    ]
    picked.sort(key=lambda item: (bucket(float(item[1]["solve_rate"])) != "nearmiss",
                                  -float(item[1]["solve_rate"])))
    return [{"id": tid, "statement": entry["stmt"]} for tid, entry in picked][:limit]


def run_arm(targets: list[dict], library: list[dict], args, import_spec: str, server=None
            ) -> tuple[dict, list[dict]]:
    """跑一个臂：/solve → 验证。返回 `({target_id: {...}}, 端点故障列表)`。"""
    attempts: dict[str, list[str]] = {}
    backend_errors: list[dict] = []
    for target in targets:
        prompt_library: list[dict] = []
        if library:
            # **按目标**注入（与在线路径同一套检索口径）：把整库原样塞进 prompt 时，
            # 与当前命题无关的引理占满预算，模型不会引用它们——"引用数=0"会是
            # 假象而不是结论。这里先按相关性过滤，再用复用密度打破相关候选间的平局。
            from sgsr.pipeline.selection import retrieve_library

            premises = retrieve_library(target["statement"], library, n=args.library_slots)
            prompt_library = [{"name": p.name, "stmt": p.statement} for p in premises]
        try:
            proofs, _ = solve_candidates(
                args.endpoint,
                target["statement"],
                args.k,
                prompt_library,
                prompt_mode="measurement",
                sample_salt=f"g3:{target['id']}",
            )
        except BackendUnavailable as exc:
            backend_errors.append({"target": target["id"], "error": str(exc)})
            attempts[target["id"]] = []
            print(f"       {target['id']:30s} \u26a0 端点报错：{exc}")
            continue
        attempts[target["id"]] = [p for p in proofs if p.strip()]
        print(f"       {target['id']:30s} 候选证明 {len(attempts[target['id']])} 篇")

    jobs = [
        {"id": f"{tid}:{i}", "target": tid, "cmd": "dependencies",
         "stmt": stmt, "proof": proof}
        for tid, stmt in ((t["id"], t["statement"]) for t in targets)
        for i, proof in enumerate(attempts[tid])
    ]
    responses: dict[str, dict] = {}
    if jobs:
        if server is None:
            with LeanServer(imports=import_spec, heartbeats=budget_for_jobs(len(jobs)),
                            stderr_path=RESULTS / "g3_real_stderr.log") as local_server:
                responses = local_server.batch(jobs)
                frontend_ms = local_server.frontend_ms_total
        else:
            responses = server.batch(jobs)
            frontend_ms = server.frontend_ms_total
        print(f"       [验证] {len(jobs)} 篇（frontend {frontend_ms}ms）")

    out: dict[str, dict] = {}
    for target in targets:
        tid = target["id"]
        verdicts = []
        for i, _ in enumerate(attempts[tid]):
            entry = responses.get(f"{tid}:{i}")
            if entry is None:
                # 响应缺失是**协议错误**，必须能被下游的 fail_pipeline 判据看到；
                # 旧实现把原因写成 None，于是那两个检测字符串永远匹配不到（死代码）。
                verdicts.append({"index": i, "ok": False, "reason": "missing_response"})
                continue
            if entry.get("error"):
                verdicts.append({"index": i, "ok": False,
                                 "reason": f"protocol_error:{entry['error'].get('code')}"})
                continue
            result = entry.get("result") or {}
            verdicts.append({
                "index": i,
                "ok": result.get("verified") is True,
                "reason": result.get("reason"),
                "constants": list(result.get("constants") or []),
            })
        ok_count = sum(1 for v in verdicts if v["ok"])
        # 只统计通过内核验收的证明项常量；失败文本中出现库名不算复用。
        citations = sum(
            1
            for verdict in verdicts
            if verdict["ok"] and any(
                str(name).rsplit(".", 1)[-1].startswith("sgs_lem_")
                for name in verdict.get("constants", [])
            )
        )
        out[tid] = {
            "provable": ok_count > 0,
            "ok_count": ok_count,
            "library_citations": citations,
            # 分母固定是 k：没生成出证明也算失败，否则"少生成几篇"会虚高
            "solve_rate": round(ok_count / args.k, 4) if args.k else 0.0,
            "verdicts": verdicts,
            "proofs": attempts[tid],
        }
    return out, backend_errors


def main() -> int:
    parser = argparse.ArgumentParser(description="闸门 G3（真定义）：有库/无库的净覆盖")
    parser.add_argument("--targets", default=str(DATA / "targets_hard.jsonl"))
    parser.add_argument("--library", default=str(ROOT / "experiments" / "library.jsonl"))
    parser.add_argument("--select", choices=["hard", "nearmiss", "easy", "mixed"], default="nearmiss",
                        help="工作负载选择（见文件头）")
    parser.add_argument("--g1-report", default=str(RESULTS / "reverify_all_n164.json"),
                        help="用来判定目标难度（修正后的 G1 报告）")
    parser.add_argument("--endpoint", default=DEFAULT_SOLVE)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--k", type=int, default=2)
    parser.add_argument("--library-slots", type=int, default=10,
                        help="处理臂每个目标注入几条库引理（按目标检索，不是整库）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    library = load_library(Path(args.library))
    if not library:
        print("[g3r] 库为空——那两臂就没有差别，先跑 scripts\\run_round.py 把库建起来")
        return 2
    targets = load_workload(args, args.limit)
    if not targets:
        print("[g3r] 目标集为空")
        return 1
    print(f"[g3r] 目标 {len(targets)} 条；库 {len(library)} 条；k={args.k}；端点 {args.endpoint}")

    # 预检：处理臂的 import 必须真的能用。
    # phase22 踩过一次：`lake build SgsLean` 不会编 `SgsLean.GeneratedLibrary`（olea 不存在），
    # 于是处理臂的片段整批崩掉、所有判定为空——却表现为"给库后全部退化"的假结论。
    # 这里用一条最便宜的作业先验证环境，不通就直接退出，不让假数据流进报告。
    started = time.perf_counter()
    print("[g3r] ==== 基线臂（不给库） ====")
    baseline, baseline_errors = run_arm(targets, [], args, BASE_IMPORTS)
    print("[g3r] ==== 处理臂（给库 + 环境 import 库） ====")
    with LeanServer(imports=LIBRARY_IMPORTS, heartbeats=budget_for_jobs(),
                    stderr_path=RESULTS / "g3_real_stderr.log") as treatment_server:
        preflight_ok, preflight_detail = preflight_in_session(treatment_server)
        if not preflight_ok:
            print(f"[g3r] 预检失败：处理臂环境 `import {LIBRARY_IMPORTS}` 不可用。")
            print(f"       详情：{preflight_detail}")
            return 1
        print(f"[g3r] 预检通过（{LIBRARY_IMPORTS} 可用；复用同一会话）")
        treatment, treatment_errors = run_arm(
            targets, library, args, LIBRARY_IMPORTS, server=treatment_server
        )
    elapsed = time.perf_counter() - started
    backend_errors = len(baseline_errors) + len(treatment_errors)

    base_provable = [tid for tid, r in baseline.items() if r["provable"]]
    treat_provable = [tid for tid, r in treatment.items() if r["provable"]]
    gained = sorted(set(treat_provable) - set(base_provable))
    lost = sorted(set(base_provable) - set(treat_provable))
    gain = len(treat_provable) - len(base_provable)
    cover_empty = round(sum(r["solve_rate"] for r in baseline.values()), 4)
    cover_lib = round(sum(r["solve_rate"] for r in treatment.values()), 4)
    graded_gain = round(cover_lib - cover_empty, 4)

    # 把"工具坏了"与"两臂都没证出来"分开：
    # phase21 实测 W = 37 条最难的（**因为难才被选进 targets_hard**）时两臂都是 0——
    # 那是有信息量的结果（库帮不动这么难的题），不是脚本失败。只有当验证层大面积报协议错误时
    # 才判 fail_pipeline。
    protocol_errors = sum(
        1
        for arm in (baseline, treatment)
        for record in arm.values()
        for verdict_row in record["verdicts"]
        if str(verdict_row.get("reason") or "").startswith("protocol_error")
        or verdict_row.get("reason") in ("missing_response", "missing_reason")
    )
    if backend_errors > 0 and not base_provable and not treat_provable:
        # 端点挂了导致两臂都空 → 装置问题，不是"库没用"。
        verdict = "fail_pipeline"
    elif protocol_errors > 0 and not base_provable and not treat_provable:
        verdict = "fail_pipeline"
    elif graded_gain > 0:
        verdict = "pass"
    elif not base_provable and not treat_provable:
        verdict = "no_solvable_targets"
    elif graded_gain < 0:
        verdict = "negative"
    else:
        verdict = "no_gain"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "targets": len(targets),
        "k": args.k,
        "library_size": len(library),
        "library_names": [item["name"] for item in library],
        "endpoint": args.endpoint,
        "baseline_imports": BASE_IMPORTS,
        "treatment_imports": LIBRARY_IMPORTS,
        "proved_cover_empty": len(base_provable),
        "proved_cover_library": len(treat_provable),
        "gain": gain,
        "graded_cover_empty": cover_empty,
        "graded_cover_library": cover_lib,
        "graded_gain": graded_gain,
        "treatment_proofs_citing_library": sum(
            r["library_citations"] for r in treatment.values()
        ),
        "treatment_proofs_total": sum(len(r["proofs"]) for r in treatment.values()),
        "gained_targets": gained,
        "lost_targets": lost,
        "workload": args.select,
        "verdict": verdict,
        "protocol_errors": protocol_errors,
        "backend_errors": backend_errors,
        "backend_error_examples": (baseline_errors + treatment_errors)[:3],
        "criterion": ("评分制增益 > 0 → pass；= 0 → no_gain；< 0 → negative；"
                      "两臂皆 0 → no_solvable_targets；"
                      "端点故障或验证层协议错误导致两臂皆空 → fail_pipeline"),
        "timing": {"total_s": round(elapsed, 1)},
        "per_target": {
            tid: {
                "baseline": baseline[tid]["provable"],
                "treatment": treatment[tid]["provable"],
                "baseline_rate": baseline[tid]["solve_rate"],
                "treatment_rate": treatment[tid]["solve_rate"],
                "treatment_library_citations": treatment[tid]["library_citations"],
                "baseline_verdicts": baseline[tid]["verdicts"],
                "treatment_verdicts": treatment[tid]["verdicts"],
            }
            for tid in baseline
        },
    }
    out_path = Path(args.out) if args.out else RESULTS / f"g3_real_{args.select}_n{len(targets)}_k{args.k}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNS / f"g3real_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}").mkdir(
        parents=True, exist_ok=True
    )

    print(f"[g3r] 工作负载={args.select}  目标={len(targets)}  k={args.k}")
    print(f"[g3r] 评分制 cover：基线 {cover_empty} → 给库 {cover_lib}（增益 {graded_gain}）")
    print(
        f"[g3r] 处理臂引用库的证明：{report['treatment_proofs_citing_library']}"
        f"/{report['treatment_proofs_total']} 篇"
    )
    print(f"[g3r] 二值口径：proved_cover(∅)={len(base_provable)}  proved_cover(S)={len(treat_provable)}  差={gain}")
    print(f"[g3r] 因库而多证出：{gained}")
    if lost:
        print(f"[g3r] 注意：有 {len(lost)} 条在给库后反而没证出（采样波动）：{lost}")
    print(f"[g3r] 判定：{verdict}；报告 {out_path}")
    return 0 if verdict in ("pass", "no_gain", "no_solvable_targets", "negative") else 1


if __name__ == "__main__":
    raise SystemExit(main())
