"""选择层（N2）：把"留哪些引理"写成预算约束下的选择问题（规格 5.5 节）。

本项目的**唯一方法性改动**就在这个文件里：用

    score(l) = reuse(l) / cost(l)
    reuse(l) = l 被多少个**不同目标**的**通过验收的**证明实际引用
    cost(l)  = l 渲染进提示词的 token 数

替换 SGS 的 LLM Guide，并让它在**准入、排序、淘汰**三处都用同一个量。

## 选择算法：密度贪心 + 与最优单条取较优

规格 5.5 节写死了两种口径，这里都实现：

* `reuse_cost_greedy(pool, ctx_budget)`：**预算约束**（token）下的密度贪心
  （边际增益 ÷ 成本），并与"只取最优单条"的结果取较优 → `½(1−1/e)` 的近似保证；
* `greedy(candidates, targets, budget)`：**条数约束**下的标准贪心（1−1/e），
  保留给"按条数 B 报一份结果便于与文献对照"（DreamProver 用的是 100 条）。

## 关于"边际增益"的忠实度（必须写清楚，别过度主张）

规格 5.5 节警告过：真算边际增益要对每条候选重跑两臂，代价过高。第一阶段的
工程做法是用 `score(l)` 当边际增益的**代理**，并要求**校验代理的忠实度**
（抽 10 条同时算真实边际增益与代理值，报 Spearman 相关；低于 0.5 就换代理）。

`spearman` 就是给这件事用的。**代理忠实度没测之前，不要声称有近似保证**——
保证只对"每条候选有冻结覆盖集合、目标函数是并集"的抽象问题成立，
对"随机语言模型的真实 pass@k"不成立（审计意见第 1.2 条，已采纳）。

## 历史（为什么这里曾经只剩一个 cover）

早期版本在这里放过两个"代理"：`sig_cover`（引理的签名是否出现在目标的证明轨迹里）与
`dependency_cover`。那是**空转**的：候选池本身就是"各目标轨迹签名的并集"，于是每条候选
按构造至少覆盖一个目标，贪心比必然 1.000、子模性必然 0 违例——测的是一个恒真命题。
那套代码连同它的 harness 已删除。`parent_cover` 保留下来只作**条数约束**口径，
**不**再用于主流程的准入与淘汰。
"""

from __future__ import annotations

import itertools

#: 复用判据的默认门槛：被至少 2 个不同目标引用过才算"可复用"。
DEFAULT_REUSE_THRESHOLD = 2


def estimate_tokens(text: str) -> int:
    """便宜的 token 估计（与 `retrieval.estimate_tokens` 同口径，见那里的说明）。"""
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def render_tokens(entry: dict) -> int:
    """`cost(l)`：把一条引理渲染进提示词后的 token 数。

    提示词里的形态是 `- \\`sgs_lem_i\\` : <stmt>`（见 `prompts.solve_prompt`），
    所以估算要把名字与包装算进去——只算 `stmt` 会系统性低估短引理的成本。
    """
    name = str(entry.get("name") or "sgs_lem_?")
    stmt = str(entry.get("stmt") or "")
    return estimate_tokens(f"- `{name}` : {stmt}")


def density(entry: dict) -> float:
    """`score(l) = reuse(l) / cost(l)`；`reuse` 缺失按 0（未测量 = 还没有复用证据）。"""
    reuse = float(entry.get("reuse") or 0.0)
    cost = float(entry.get("cost_tokens") or render_tokens(entry))
    return reuse / cost if cost > 0 else 0.0


def reuse_cost_greedy(
    pool: list[dict],
    ctx_budget: int,
    count_budget: int | None = None,
) -> tuple[list[dict], list[float], dict]:
    """密度贪心：在 token 预算内按"边际增益 ÷ 成本"挑引理，并与最优单条取较优。

    `pool` 的每条要有 `reuse`（不同目标上的验收引用数）与 `stmt`；
    `cost_tokens` 缺失时用 `render_tokens` 现算。

    边际增益的代理：**同一批候选之间不重复计同一目标的覆盖**（并集形式），
    即每条引理贡献的目标集合是它被引用过的目标集合 `entry["reuse_targets"]`；
    缺这个字段时退化成"贡献 `reuse` 的数值"（仍单调，但不再是并集）。

    返回 `(选中列表, 每轮边际增益, 诊断)`。诊断里带 `mode`：
    `density`（贪心更优）或 `best_single`（单条更优，按规格必须取较优的那个）。
    """
    items = []
    for entry in pool:
        cost = int(entry.get("cost_tokens") or render_tokens(entry))
        reuse = float(entry.get("reuse") or 0.0)
        covered = set(entry.get("reuse_targets") or [])
        items.append({"entry": entry, "cost": cost, "reuse": reuse, "covered": covered})
    items = [item for item in items if item["cost"] > 0 and item["reuse"] > 0]

    chosen: list[dict] = []
    gains: list[float] = []
    covered: set[str] = set()
    remaining = ctx_budget
    available = list(items)
    while available and (count_budget is None or len(chosen) < count_budget):
        best, best_gain, best_density = None, 0.0, 0.0
        for item in available:
            if item["cost"] > remaining:
                continue
            # 并集口径下的边际增益；没有 reuse_targets 时退回标量 reuse
            if item["covered"]:
                gain = float(len(item["covered"] - covered))
            else:
                gain = float(item["reuse"])
            if gain <= 0:
                continue
            dens = gain / item["cost"]
            if dens > best_density or (dens == best_density and best is not None
                                       and gain > best_gain):
                best, best_gain, best_density = item, gain, dens
        if best is None:
            break
        chosen.append(best["entry"])
        gains.append(best_gain)
        covered |= best["covered"]
        remaining -= best["cost"]
        available.remove(best)

    greedy_value = sum(gains)
    greedy_cost = sum(int(e.get("cost_tokens") or render_tokens(e)) for e in chosen)

    # 基准：只取一条最优的。**必须用同一个目标函数比较**——规格 5.5 节要的是
    # "与最优单条取较优"，而目标函数是覆盖增益；拿单条的 `reuse` 标量去比贪心的
    # 并集增益是拿两个不同的量比大小（实测踩过：reuse=5 的单条会把真正更优的
    # 两条组合顶掉）。这里两者的增益都按同一口径算。
    single, single_gain, single_cost = None, 0.0, 0
    for item in items:
        # **基准也必须守预算**：不筛的话"最优单条"会选出一条根本放不进预算的引理，
        # 与它取较优就变成了"越预算越占便宜"（实测踩过）。
        if int(item["cost"]) > ctx_budget:
            continue
        gain = float(len(item["covered"])) if item["covered"] else float(item["reuse"])
        cost = int(item["cost"])
        if single is None or gain > single_gain or (gain == single_gain and cost < single_cost):
            single, single_gain, single_cost = item["entry"], gain, cost

    mode = "density"
    # 同增益时取成本更低的；都相同则保留贪心结果（实现更简单的那条）
    if single is not None and (single_gain > greedy_value
                               or (single_gain == greedy_value and single_cost < greedy_cost)):
        chosen, gains, mode = [single], [single_gain], "best_single"
        greedy_cost = single_cost
    best_single_value = single_gain
    diagnostics = {
        "mode": mode,
        "greedy_value": round(greedy_value, 4),
        "best_single_value": round(best_single_value, 4),
        "budget": ctx_budget,
        "remaining": ctx_budget - greedy_cost,
        "cost": sum(int(e.get("cost_tokens") or render_tokens(e)) for e in chosen),
    }
    return chosen, gains, diagnostics


def select_by_reuse(
    pool: list[dict],
    ctx_budget: int,
    threshold: int = DEFAULT_REUSE_THRESHOLD,
    count_budget: int | None = None,
) -> tuple[list[dict], list[float], dict]:
    """**准入 + 选择**：先按 `reuse >= threshold` 过门槛，再做密度贪心。

    门槛用 `>= threshold` 而不是 `>= 1`：规格 1.2 的 `CostPerReusable` 定义就是
    "被至少两个不同目标引用过"的引理数——准入口径必须与成本口径一致，
    否则 `CostPerReusable` 的分母里会混进只被一个目标用过的引理。

    返回 `(选中, 每轮增益, 诊断)`；诊断里额外给出被门槛拦下的条数，
    便于回答"库为什么没长大"。
    """
    passed = [e for e in pool if int(e.get("reuse") or 0) >= threshold]
    blocked = len(pool) - len(passed)
    chosen, gains, diagnostics = reuse_cost_greedy(passed, ctx_budget, count_budget)
    diagnostics["reuse_threshold"] = threshold
    diagnostics["pool"] = len(pool)
    diagnostics["blocked_by_threshold"] = blocked
    return chosen, gains, diagnostics


def evict(
    library: list[dict],
    round_index: int,
    evict_after: int = 3,
) -> tuple[list[dict], list[dict]]:
    """淘汰：`reuse=0` 且入库超过 `evict_after` 轮 → 移入冷存（**不删除**）。

    两个边界（规格 5.5 节写死）：

    * **探索额度**：入库不足 `evict_after` 轮的新引理不参与淘汰——否则新引理
      永远活不过第一轮，库会退化成只增不减的僵化集合；
    * 只淘汰 `reuse=0` 的僵尸，低效降级（`score` 低于中位数一半）留到 P2 再接线，
      因为那需要先有稳定的 `reuse` 测量。
    """
    kept: list[dict] = []
    evicted: list[dict] = []
    for entry in library:
        added = entry.get("added_round")
        age = round_index - int(added) if added is not None else 0
        if int(entry.get("reuse") or 0) == 0 and age > evict_after:
            evicted.append(entry | {"status": "cold", "evicted_round": round_index})
        else:
            kept.append(entry)
    return kept, evicted


def spearman(pairs: list[tuple[float, float]]) -> float | None:
    """Spearman 相关系数（供"代理忠实度"审计用；并列取平均秩）。"""
    n = len(pairs)
    if n < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx = ranks([p[0] for p in pairs])
    ry = ranks([p[1] for p in pairs])
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    num = sum((rx[i] - mean_x) * (ry[i] - mean_y) for i in range(n))
    den_x = sum((rx[i] - mean_x) ** 2 for i in range(n)) ** 0.5
    den_y = sum((ry[i] - mean_y) ** 2 for i in range(n)) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


# ─────────────────────── 条数约束口径（与文献对照） ───────────────────────


def parent_cover(selected: list[dict], targets: list[dict]) -> set[str]:
    """父目标覆盖：返回被 `selected` 里任意候选"为之生成"的目标集合。

    **只用于条数约束口径**（规格 5.5 节要求额外报一份"按条数 B"的结果便于与
    DreamProver 的 100 条对照）。它把"一条引理覆盖的目标"定义成它**为之生成**的那个目标，
    与真复用无关，所以**不参与**主流程的准入与淘汰。
    """
    wanted = {t["id"] for t in targets}
    return {c.get("target") for c in selected if c.get("target") in wanted}


def greedy(
    candidates: list[dict],
    targets: list[dict],
    budget: int,
    cover=parent_cover,
) -> tuple[list[dict], list[int]]:
    """条数约束下的子模贪心：在 `budget` 条内尽量覆盖不同的父目标。"""
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
