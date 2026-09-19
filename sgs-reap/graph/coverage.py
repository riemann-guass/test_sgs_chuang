"""N2 的覆盖度与子模贪心（P3 / 闸门 G3）。

`cover(S) = |{w ∈ W : ∃ s ∈ S, s 能帮助证明 w}|` 是**并集形式**，因此在任何
"候选→命中的目标集合"映射下都**单调子模**，贪心有 (1−1/e) 近似界。

G3 要回答的是："cover 有没有**便宜**且仍具子模性的代理？"本模块给出两个层次：

1. `sig_cover`：**代理**。用轨迹里出现过的子目标签名来判"这条引理对 w 有用"——
   引理语句的签名出现在 w 的证明轨迹里，就算它能帮上 w。**不需要任何 Lean 调用**
   （签名来自已经落盘的轨迹），因此便宜；而且它仍是"并集覆盖"，**天然子模**。
2. `dependency_cover`：**昂贵但更可信**的版本。用 `uses`（依赖抽取结果）判"w 的证明真的引用了 s"。
   需要逐条跑 Lean（`Measure.dependencies`），只在库规模小时用于校验代理。

本模块只做纯函数计算，Lean 侧的调用由 harness 负责。
"""

from __future__ import annotations

import itertools
import random

from graph.schema import normalize_sig


def sig_cover(selected: set[str], targets: dict[str, set[str]]) -> set[str]:
    """代理覆盖度：返回被 S 覆盖的目标集合。

    `targets` 是 `{目标 id: 该目标证明轨迹里出现过的签名集合}`。
    """
    return {
        target
        for target, sigs in targets.items()
        if any(normalize_sig(s) in sigs for s in selected)
    }


def dependency_cover(selected: set[str], target_deps: dict[str, set[str]]) -> set[str]:
    """可信覆盖度：`target_deps` 是 `{目标 id: 证明里真正引用过的引理名集合}`。"""
    return {target for target, deps in target_deps.items() if deps & selected}


def greedy(candidates: list[str], targets: dict[str, set[str]], budget: int,
           cover=sig_cover) -> tuple[list[str], list[int]]:
    """子模贪心：每轮选边际增益最大的候选。返回（选中的候选，每轮增益）。"""
    chosen: list[str] = []
    gains: list[int] = []
    covered: set[str] = set()
    pool = list(candidates)
    for _ in range(budget):
        best, best_gain = None, -1
        for cand in pool:
            gain = len(cover({cand} | set(chosen), targets)) - len(covered)
            if gain > best_gain:
                best, best_gain = cand, gain
        if best is None or best_gain <= 0:
            break
        chosen.append(best)
        pool.remove(best)
        covered = cover(set(chosen), targets)
        gains.append(best_gain)
    return chosen, gains


def brute_force_optimal(candidates: list[str], targets: dict[str, set[str]], budget: int,
                        cover=sig_cover) -> tuple[list[str], int]:
    """小规模穷举最优（只用于校验贪心的近似比）。"""
    best, best_val = [], 0
    for size in range(1, min(budget, len(candidates)) + 1):
        for combo in itertools.combinations(candidates, size):
            val = len(cover(set(combo), targets))
            if val > best_val:
                best, best_val = list(combo), val
    return best, best_val


def submodularity_violations(candidates: list[str], targets: dict[str, set[str]],
                             trials: int = 500, seed: int = 0,
                             cover=sig_cover) -> list[tuple[str, str, float, float]]:
    """经验检查子模性（边际增益递减）：对随机 A ⊆ B 与 x ∉ B 检查
    `f(A∪{x}) − f(A) ≥ f(B∪{x}) − f(B)`，返回违例列表。"""
    rng = random.Random(seed)
    violations: list[tuple[str, str, float, float]] = []
    for _ in range(trials):
        if len(candidates) < 3:
            break
        pool = rng.sample(candidates, min(len(candidates), rng.randint(3, len(candidates))))
        x = pool.pop()
        split = rng.randint(0, len(pool))
        a = set(pool[:split])
        b = set(pool)
        gain_a = len(cover(a | {x}, targets)) - len(cover(a, targets))
        gain_b = len(cover(b | {x}, targets)) - len(cover(b, targets))
        if gain_a < gain_b - 1e-9:
            violations.append(("A⊆B", x, float(gain_a), float(gain_b)))
    return violations
