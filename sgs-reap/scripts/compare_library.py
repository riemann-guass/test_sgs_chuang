"""LeanReuse 配对比较：同一批题在“无库”和“有库”条件下各运行一次。

两个条件只允许引理库不同；模型、采样数、验证预算和题目必须相同。报告同时给出
解题数变化和“通过验证的证明数 / 生成数”变化。默认使用开发集 D，也可以通过
``--targets`` 指定其他非测试数据。这个入口不会修改引理库。

示例：先启动模型代理，再运行

    python scripts/compare_library.py --limit 12 --k 2
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


def load_workload(path: Path, limit: int) -> list[dict]:
    """读取统一 JSONL 题目文件；只保留比较所需字段。"""
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        {"id": str(row["id"]), "statement": str(row["statement"])}
        for row in rows[:limit or None]
    ]


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
                sample_salt=f"compare:{target['id']}",
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
                            stderr_path=RESULTS / "compare_library_stderr.log") as local_server:
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
    parser = argparse.ArgumentParser(description="逐题比较无库与有库的证明结果")
    parser.add_argument("--targets", default=str(DATA / "minif2f_dev.jsonl"),
                        help="JSONL 题目文件；默认使用开发集 D")
    parser.add_argument("--library", default=str(ROOT / "experiments" / "library.jsonl"))
    parser.add_argument("--endpoint", default=DEFAULT_SOLVE)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--k", type=int, default=2)
    parser.add_argument("--library-slots", type=int, default=10,
                        help="处理臂每个目标注入几条库引理（按目标检索，不是整库）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    library = load_library(Path(args.library))
    if not library:
        print("[compare] 库中没有可发布引理；请先运行 scripts\\build_library.py")
        return 2
    targets = load_workload(Path(args.targets), args.limit)
    if not targets:
        print("[compare] 目标集为空")
        return 1
    print(f"[compare] 目标 {len(targets)} 条；库 {len(library)} 条；k={args.k}；端点 {args.endpoint}")

    # 预检：处理臂的 import 必须真的能用。
    # phase22 踩过一次：`lake build SgsLean` 不会编 `SgsLean.GeneratedLibrary`（olea 不存在），
    # 于是处理臂的片段整批崩掉、所有判定为空——却表现为"给库后全部退化"的假结论。
    # 这里用一条最便宜的作业先验证环境，不通就直接退出，不让假数据流进报告。
    started = time.perf_counter()
    print("[compare] ==== 无库条件 ====")
    baseline, baseline_errors = run_arm(targets, [], args, BASE_IMPORTS)
    print("[compare] ==== 有库条件 ====")
    with LeanServer(imports=LIBRARY_IMPORTS, heartbeats=budget_for_jobs(),
                    stderr_path=RESULTS / "compare_library_stderr.log") as treatment_server:
        preflight_ok, preflight_detail = preflight_in_session(treatment_server)
        if not preflight_ok:
            print(f"[compare] 预检失败：有库环境 `import {LIBRARY_IMPORTS}` 不可用。")
            print(f"       详情：{preflight_detail}")
            return 1
        print(f"[compare] 预检通过（{LIBRARY_IMPORTS} 可用；复用同一会话）")
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

    # “两个条件都没解出”仍是有效结果；只有模型服务或验证协议失败才判流程故障。
    protocol_errors = sum(
        1
        for arm in (baseline, treatment)
        for record in arm.values()
        for verdict_row in record["verdicts"]
        if str(verdict_row.get("reason") or "").startswith("protocol_error")
        or verdict_row.get("reason") in ("missing_response", "missing_reason")
    )
    if backend_errors > 0 and not base_provable and not treat_provable:
        # 端点挂了导致两个条件都空，是服务故障，不是“库没用”。
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
        "target_file": str(Path(args.targets)),
        "verdict": verdict,
        "protocol_errors": protocol_errors,
        "backend_errors": backend_errors,
        "backend_error_examples": (baseline_errors + treatment_errors)[:3],
        "criterion": ("评分制增益 > 0 → pass；= 0 → no_gain；< 0 → negative；"
                      "两个条件皆 0 → no_solvable_targets；"
                      "端点故障或验证协议错误导致结果皆空 → fail_pipeline"),
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
    out_path = Path(args.out) if args.out else RESULTS / f"library_comparison_n{len(targets)}_k{args.k}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNS / f"compare_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}").mkdir(
        parents=True, exist_ok=True
    )

    print(f"[compare] 目标={len(targets)}  k={args.k}")
    print(f"[compare] 通过率总和：无库 {cover_empty} → 有库 {cover_lib}（变化 {graded_gain}）")
    print(
        f"[compare] 有库条件下实际引用引理的证明：{report['treatment_proofs_citing_library']}"
        f"/{report['treatment_proofs_total']} 篇"
    )
    print(f"[compare] 解题数：无库 {len(base_provable)}，有库 {len(treat_provable)}，变化 {gain}")
    print(f"[compare] 仅有库条件解出：{gained}")
    if lost:
        print(f"[compare] 注意：有 {len(lost)} 条在给库后反而没证出（采样波动）：{lost}")
    print(f"[compare] 判定：{verdict}；报告 {out_path}")
    return 0 if verdict in ("pass", "no_gain", "no_solvable_targets", "negative") else 1


if __name__ == "__main__":
    raise SystemExit(main())
