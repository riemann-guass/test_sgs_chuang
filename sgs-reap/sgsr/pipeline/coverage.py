"""N2 的覆盖度与子模工具（选择层）。

    cover(S) = |{ w ∈ W : 某个 s ∈ S 覆盖 w }|

并集形式，**单调子模**，因此贪心有 (1−1/e) 近似界——这正是"引理库该留哪几条"
（数据库里的物化视图选择）该用的结构。

## 这里定义的是**选择用的代理**，不是评测用的真值

一条候选"覆盖"的是它**为之生成的那个目标**（父目标关系来自生成过程，不来自测量，
所以不循环）。真 cover 需要**两臂测量**（有库/无库各跑一遍工作负载），在
`scripts/run_gate_g3_real.py` 里算；代理与真值的一致性，正是最初计划里闸门 G3 要回答的问题。

## 历史（为什么这里只剩一个 cover）

早期版本在这里放过两个"代理"：`sig_cover`（引理的签名是否出现在目标的证明轨迹里）与
`dependency_cover`。那是**空转**的：候选池本身就是"各目标轨迹签名的并集"，于是每条候选
按构造至少覆盖一个目标，贪心比必然 1.000、子模性必然 0 违例——测的是一个恒真命题。
那套代码连同它的 harness（`run_gate_g3.py`）已删除。
"""

from __future__ import annotations

import itertools
import random


def parent_cover(selected: list[dict], targets: list[dict]) -> set[str]:
    """父目标覆盖：返回被 `selected` 里任意候选"为之生成"的目标集合。"""
    wanted = {t["id"] for t in targets}
    return {c.get("target") for c in selected if c.get("target") in wanted}


def greedy(
    candidates: list[dict],
    targets: list[dict],
    budget: int,
    cover=parent_cover,
) -> tuple[list[dict], list[int]]:
    """子模贪心：在预算 `budget` 内尽量覆盖不同的目标。

    同分时按证明步数排序（更长的证明往往意味着引理更实质；过短的证明常是重言式）。
    返回（选中的候选，每轮的边际增益）。
    """
    chosen: list[dict] = []
    gains: list[int] = []
    covered: set[str] = set()
    pool = list(candidates)
    for _ in range(max(0, budget)):
        best, best_gain = None, -1
        for cand in pool:
            gain = len(covered | cover([cand], targets)) - len(covered)
            if gain > best_gain or (
                gain == best_gain
                and best is not None
                and cand.get("proof_steps", 0) > best.get("proof_steps", 0)
            ):
                best, best_gain = cand, gain
        if best is None or best_gain <= 0:
            break
        chosen.append(best)
        pool.remove(best)
        covered |= cover([best], targets)
        gains.append(best_gain)
    return chosen, gains


def brute_force_optimal(
    candidates: list[dict], targets: list[dict], budget: int, cover=parent_cover
) -> tuple[list[dict], int]:
    """小规模穷举最优（只用于校验贪心的近似比）。"""
    best, best_val = [], 0
    for size in range(1, min(budget, len(candidates)) + 1):
        for combo in itertools.combinations(candidates, size):
            val = len(cover(list(combo), targets))
            if val > best_val:
                best, best_val = list(combo), val
    return best, best_val


def submodularity_violations(
    candidates: list[dict], targets: list[dict], trials: int = 500, seed: int = 0,
    cover=parent_cover,
) -> list[tuple[float, float]]:
    """经验检查子模性（边际增益递减）：对随机 A ⊆ B 与 x ∉ B 检查
    `f(A∪{x}) − f(A) ≥ f(B∪{x}) − f(B)`，返回违例列表。

    并集覆盖在数学上必然子模，所以这个检查的作用是**防实现 bug**，不是证明。
    """
    rng = random.Random(seed)
    violations: list[tuple[float, float]] = []
    for _ in range(trials):
        if len(candidates) < 3:
            break
        pool = rng.sample(candidates, min(len(candidates), rng.randint(3, len(candidates))))
        x = pool.pop()
        split = rng.randint(0, len(pool))
        a = pool[:split]
        b = pool
        gain_a = len(cover(a + [x], targets)) - len(cover(a, targets))
        gain_b = len(cover(b + [x], targets)) - len(cover(b, targets))
        if gain_a < gain_b:
            violations.append((float(gain_a), float(gain_b)))
    return violations
