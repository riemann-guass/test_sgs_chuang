"""闸门 G3：cover 有没有**便宜且仍具子模性**的代理（P3 / N2）。

用法（纯 Python，不调 Lean、不花钱）：

    python tests\\run_gate_g3.py                          # 读最近的 g2 轨迹
    python tests\\run_gate_g3.py --traces <path> --budget 5
    python tests\\run_gate_g3.py --self-test              # 合成数据上的子模性/贪心自测

判定准则（**跑之前定死**）：

* 代理 `sig_cover` 在经验检查里 **0 违例**（边际增益递减），并且
* 贪心在小实例上达到 **≥ (1−1/e)·OPT**，

两条都满足 → G3 通过（代理便宜且仍具子模性，可用贪心 + (1−1/e) 界）；
否则 → 放弃近似保证，N2 改述为启发式选择。

代理的**代价**也如实报告：`sig_cover` 只读已落盘的轨迹签名（0 次 Lean 调用），
而 `dependency_cover` 需要逐条跑 `Measure.dependencies`。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graph.coverage import (  # noqa: E402
    brute_force_optimal,
    dependency_cover,
    greedy,
    sig_cover,
    submodularity_violations,
)
from graph.schema import normalize_sig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "experiments" / "runs"
RESULTS = ROOT / "experiments" / "results"


def latest_g2_traces() -> Path | None:
    candidates = sorted(RUNS.glob("g2_*/traces.jsonl"))
    return candidates[-1] if candidates else None


def load_targets(path: Path) -> tuple[dict[str, set[str]], list[str]]:
    """从 g2 轨迹里取 `{目标: 签名集合}`，候选池 = 所有出现过的签名。"""
    targets: dict[str, set[str]] = {}
    pool: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sigs = {normalize_sig(step.get("signature", "")) for step in row.get("steps", [])}
        sigs.discard("")
        if sigs:
            targets[row["id"]] = sigs
            pool |= sigs
    return targets, sorted(pool)


def self_test() -> int:
    """合成数据自测：子模性必须 0 违例；贪心必须达到 (1−1/e)·OPT。"""
    targets = {
        "t1": {"a", "b"},
        "t2": {"b", "c"},
        "t3": {"c", "d"},
        "t4": {"d"},
    }
    pool = ["a", "b", "c", "d", "e"]
    violations = submodularity_violations(pool, targets, trials=200, seed=1)
    chosen, gains = greedy(pool, targets, budget=3)
    _, opt = brute_force_optimal(pool, targets, budget=3)
    greedy_val = len(sig_cover(set(chosen), targets))
    ratio = greedy_val / opt if opt else 0.0
    print(f"[g3-self-test] 违例={len(violations)} 贪心={chosen}({greedy_val}) 最优={opt} 比={ratio:.3f}")
    if violations or ratio + 1e-9 < 1 - 1 / math.e:
        print("[g3-self-test] FAIL")
        return 1
    print("[g3-self-test] PASS（子模性 0 违例；贪心 ≥ (1−1/e)·OPT）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="闸门 G3：便宜的子模代理")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--traces", default=None)
    parser.add_argument("--budget", type=int, default=5)
    parser.add_argument("--trials", type=int, default=500)
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    trace_path = Path(args.traces) if args.traces else latest_g2_traces()
    if trace_path is None or not trace_path.exists():
        print("[g3] 找不到 g2 轨迹；先跑 tests\\run_gate_g2.py --dry-run")
        return 1
    targets, pool = load_targets(trace_path)
    print(f"[g3] 输入 {trace_path.name}：{len(targets)} 个目标 / 候选池 {len(pool)} 条签名")

    # ① 便宜代理：子模性经验检查（0 违例才过）
    violations = submodularity_violations(pool, targets, trials=args.trials, seed=7)
    # ② 贪心 vs 穷举最优（小实例）
    sub_pool = pool[:12]
    chosen, gains = greedy(sub_pool, targets, budget=min(args.budget, 3))
    greedy_val = len(sig_cover(set(chosen), targets))
    _, opt = brute_force_optimal(sub_pool, targets, budget=min(args.budget, 3))
    ratio = greedy_val / opt if opt else 0.0

    # ③ 两种 cover 的代价对比（只报事实，不判分）
    dep_targets = {t: {s for s in sigs} for t, sigs in targets.items()}  # 代理口径
    dep_val = len(dependency_cover(set(chosen), dep_targets))

    verdict = "pass" if (not violations and ratio + 1e-9 >= 1 - 1 / math.e) else "fail_heuristic_only"
    report = {
        "traces": str(trace_path.relative_to(ROOT)),
        "targets": len(targets),
        "candidate_pool": len(pool),
        "submodularity_violations": len(violations),
        "greedy": {"chosen": chosen, "gains": gains, "covered": greedy_val},
        "brute_force_optimal": opt,
        "approx_ratio": round(ratio, 4),
        "one_minus_1_over_e": round(1 - 1 / math.e, 4),
        "dependency_cover_covered": dep_val,
        "cost": {
            "sig_cover_lean_calls": 0,
            "dependency_cover_lean_calls": len(targets),
            "note": "代理只读已落盘轨迹；可信版需要对每个目标跑一次 Measure.dependencies",
        },
        "verdict": verdict,
        "criterion": "0 子模性违例 且 贪心 ≥ (1−1/e)·OPT",
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "g3_selection.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[g3] 子模性违例={len(violations)}/{args.trials}；贪心覆盖={greedy_val} "
        f"最优={opt} 比={ratio:.3f}（界={report['one_minus_1_over_e']}）"
    )
    print(f"[g3] 贪心选出：{chosen}（边际增益 {gains}）")
    print(f"[g3] 判定：{verdict}；报告：{RESULTS / 'g3_selection.json'}")
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
