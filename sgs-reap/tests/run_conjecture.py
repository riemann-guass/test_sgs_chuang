"""阶段 B：把猜想器接上（需求 → 候选引理 → 门检）。

这是把项目拉回 SG-Lean 的**第一根线**：审计发现 `/conjecture` 在 P1–P3 的任何 harness 里
都没被调用过，S 只剩 Solver 一个角色。本脚本补上另一半：

    data/targets_hard.jsonl（裸解不出的目标）
        + experiments/results/g2_demand.json（需求签名，N1 的产出）
        + 可选：引理库范例
              ↓  POST /conjecture
        候选引理语句（n 条/目标）
              ↓  sgslean-server 的 check（门检，Mathlib 模式）
        "是不是合法命题"的判定

用法：

    # ① 离线：用确定性假服务跑通链路（不花钱）
    python tests\\run_conjecture.py --limit 3 --n 3

    # ② 真跑：先起 service\\proxy.py（需网络权限），再指向它
    python tests\\run_conjecture.py --limit 5 --n 3 --endpoint http://127.0.0.1:8770/conjecture

    # ③ 反向对照：不送需求，检查端点上确实没有需求（H1 的对照组）
    python tests\\run_conjecture.py --limit 3 --no-demand

判定（跑之前定死）：

* `pass`：至少一半目标各有 ≥1 条**既过门检、又与目标符号相关**的候选，且总数 ≥ 3；
* `fail_no_candidates`：模型没产出候选（链路或提示词坏了）；
* `fail_gate`：产出了候选但几乎都过不了门检（提示词或输出格式不对）；
* `fail_irrelevant`：候选过得了门检，但**与目标无关**。

最后一条是踩出来的：第一版提示词把需求签名列成清单，模型直接把那 4 条原样抄回来当候选
（`∀ (n : ℕ), n + 0 = n` 之类），而目标是 miniF2F 的 `ℝ`/对数题——门检 15/15 通过，
但**一条都没用**。所以"过门检"必须与"与目标相关"合起来看，否则指标可以被"输出任意真命题"刷满。

相关度用的是**文本符号重叠**这个廉价代理（排除 `∀`/`→`/`ℕ` 这类无处不在的符号）；
正式版本应该是 Lean 侧抽取两侧的常量集合求交——那是后续工作，这里先如实报告。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from graph.conjecture import generate, load_demand, load_seeds  # noqa: E402
from lean_server import LeanServer  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
RUNS = ROOT / "experiments" / "runs"
DEFAULT_ENDPOINT = "http://127.0.0.1:8765/conjecture"  # 缺省用假服务（离线）

# 相关度代理：这些符号太常见，交集里有它们不算"相关"
STOP_SYMBOLS = {
    "∀", "→", "↔", "∧", "∨", "¬", "=", "≠", "≤", "<", ">", "∈", "⊢", "(", ")", "，", ",",
    "ℕ", "Nat", "Prop", "Type", "True", "False", "by", "fun", "theorem", "lemma", "example",
}
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'\.]*|[ℝℤℚℂℕ∀→↔∧∨¬≠≤≥∈⊢∪∩∅∑∏√]", re.UNICODE)


def symbols(text: str) -> set[str]:
    """从语句文本里抽出符号集合（廉价代理，见文件头）。"""
    return {t for t in TOKEN_RE.findall(text or "") if t not in STOP_SYMBOLS}


def shared_symbols(candidate: str, target: str) -> list[str]:
    return sorted(symbols(candidate) & symbols(target))


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 B：需求 → 候选引理 → 门检")
    parser.add_argument("--targets", default=str(DATA / "targets_hard.jsonl"))
    parser.add_argument("--demand", default=str(RESULTS / "g2_demand.json"))
    parser.add_argument("--library", default=str(ROOT / "experiments" / "library.jsonl"))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--limit", type=int, default=3, help="用多少条 hard 目标")
    parser.add_argument("--n", type=int, default=3, help="每条目标要几条候选")
    parser.add_argument("--demand-limit", type=int, default=8)
    parser.add_argument("--no-demand", action="store_true", help="反向对照：不送需求")
    parser.add_argument("--skip-gate", action="store_true",
                        help="跳过 Lean 门检（只验证请求链路/提示词条件化，省掉一次 Mathlib 导入）")
    parser.add_argument("--imports", default="Mathlib", help="门检用的导入环境")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    targets = [
        json.loads(line)
        for line in Path(args.targets).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][: args.limit]
    if not targets:
        print(f"[conj] 目标文件为空：{args.targets}")
        return 1

    demand = [] if args.no_demand else load_demand(args.demand, limit=args.demand_limit)
    seeds = load_seeds(args.library)
    print(
        f"[conj] 目标 {len(targets)} 条；需求 {len(demand)} 条（no_demand={args.no_demand}）；"
        f"库范例 {len(seeds)} 条；端点 {args.endpoint}"
    )

    started = time.perf_counter()
    results = []
    for target in targets:
        result = generate(args.endpoint, target, demand, seeds, args.n)
        results.append(result)
        status = f"error={result.error}" if result.error else f"候选 {len(result.candidates)} 条"
        print(f"       {target['id']:28s} {status}")

    # 门检：一次全喂（一个 lean 子进程 = 一次 Mathlib 导入）
    gate_items: list[dict] = []
    for result in results:
        for candidate in result.candidates:
            gate_items.append(
                {
                    "id": f"g:{result.target_id}:{candidate['index']}",
                    "stmt": candidate["type"],
                    "proof": "",
                }
            )
    gate_ok: dict[str, bool] = {}
    gate_reason: dict[str, str] = {}
    if gate_items and not args.skip_gate:
        with LeanServer(imports=args.imports, stderr_path=RESULTS / "conj_gate_stderr.log") as server:
            jobs = [{"id": it["id"], "cmd": "check", "stmt": it["stmt"]} for it in gate_items]
            responses = server.batch(jobs)
        for it in gate_items:
            result = (responses.get(it["id"]) or {}).get("result") or {}
            gate_ok[it["id"]] = result.get("ok") is True
            gate_reason[it["id"]] = result.get("reason") or "missing"
    elapsed = time.perf_counter() - started

    # 统计
    per_target = []
    total_candidates = 0
    total_gate_ok = 0
    total_relevant = 0
    for result in results:
        ok = 0
        relevant = 0
        rows = []
        for candidate in result.candidates:
            key = f"g:{result.target_id}:{candidate['index']}"
            gate_passed = bool(gate_ok.get(key))
            shared = shared_symbols(candidate["type"], result.statement)
            is_relevant = bool(shared)
            ok += 1 if gate_passed else 0
            relevant += 1 if (gate_passed and is_relevant) else 0
            rows.append(
                {
                    "index": candidate["index"],
                    "type": candidate["type"],
                    "gate": gate_reason.get(key, "skipped" if args.skip_gate else "missing"),
                    "shared_symbols": shared,
                    "relevant": is_relevant,
                }
            )
        total_candidates += len(result.candidates)
        total_gate_ok += ok
        total_relevant += relevant
        per_target.append(
            {
                "target": result.target_id,
                "candidates": len(result.candidates),
                "gate_ok": ok,
                "gate_ok_and_relevant": relevant,
                "demand_used": (result.meta or {}).get("demand_used"),
                "seeds_used": (result.meta or {}).get("seeds_used"),
                "error": result.error,
                "candidate_detail": rows,
            }
        )

    targets_with_ok = sum(1 for row in per_target if row["gate_ok"] > 0)
    targets_with_relevant = sum(1 for row in per_target if row["gate_ok_and_relevant"] > 0)
    gated = not args.skip_gate
    if total_candidates == 0:
        verdict = "fail_no_candidates"
    elif not gated:
        # 只验证链路时不做门检判定：把"有没有候选"当作唯一判据
        verdict = "pass_plumbing_only"
    elif targets_with_relevant * 2 >= len(per_target) and total_relevant >= 3:
        verdict = "pass"
    elif total_gate_ok > 0 and total_relevant == 0:
        verdict = "fail_irrelevant"
    else:
        verdict = "fail_gate"

    # 反向对照的断言：--no-demand 时端点必须收到 0 条需求
    reverse_check = None
    if args.no_demand:
        seen = [row["demand_used"] for row in per_target if row["demand_used"] is not None]
        reverse_check = {"all_zero": all(v == 0 for v in seen), "values": seen}
        if not reverse_check["all_zero"]:
            verdict = "fail_reverse_check"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / f"conj_{'nodemand' if args.no_demand else 'demand'}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "candidates.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": args.endpoint,
        "mode": "no_demand" if args.no_demand else "demand",
        "targets": len(targets),
        "demand_sent": len(demand),
        "seeds_sent": len(seeds),
        "n": args.n,
        "total_candidates": total_candidates,
        "gate_skipped": not gated,
        "total_gate_ok": total_gate_ok,
        "targets_with_gate_ok": targets_with_ok,
        "total_gate_ok_and_relevant": total_relevant,
        "targets_with_relevant_candidate": targets_with_relevant,
        "gate_reasons": _histogram(gate_reason.values()),
        "reverse_check": reverse_check,
        "verdict": verdict,
        "criterion": "≥一半目标各有 ≥1 条「过门检 ∧ 与目标符号相关」，且总数 ≥3 → pass",
        "timing": {"total_s": round(elapsed, 1)},
        "per_target": per_target,
        "candidates_dir": str(run_dir.relative_to(ROOT)),
    }
    out_path = Path(args.out) if args.out else RESULTS / f"conjecture_{report['mode']}_n{len(targets)}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"[conj] 候选 {total_candidates} 条 / 过门检 {total_gate_ok} 条 / "
        f"有候选过门检的目标 {targets_with_ok}/{len(per_target)}"
    )
    print(f"[conj] 门检原因分布 {report['gate_reasons']}")
    if reverse_check is not None:
        print(f"[conj] 反向对照（--no-demand）：{reverse_check}")
    print(f"[conj] 判定：{verdict}；报告 {out_path}；候选 {run_dir / 'candidates.jsonl'}")
    return 0 if verdict in ("pass", "pass_plumbing_only") else 1


def _histogram(values) -> dict:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
