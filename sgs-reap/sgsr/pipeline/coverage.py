"""选择层（N2）：把"留哪些引理、往提示词里塞哪些引理"写成预算约束下的选择问题。

本项目的**唯一方法性改动**就在这个文件里（规格 5.5 节）：

    score(l) = reuse(l) / cost(l)
    reuse(l) = l 被多少个**不同目标**的**通过验收的**证明实际引用
    cost(l)  = l 渲染进提示词的 token 数

## 三个量各有各的用途（不要混用，混用正是这一版要修的错）

| 量 | 谁在用 | 语义 |
|---|---|---|
| `reuse >= threshold` | **成本口径**（`CostPerReusable` 的分母）与**保留/淘汰** | "已经被证明可复用" |
| `reuse/cost` 密度贪心 | **提示词注入**（token 预算下的选择） | "同等预算下先塞谁" |
| 探索额度 + 曝光计数 | **准入**与**给机会** | 新引理还没有复用证据，**不能**用它没证据来拒它 |

这三件事曾经被挤进一个 `select_by_reuse` 里，后果是致命的：新引理的 `reuse`
按构造是 0，于是（a）它永远过不了 `reuse >= threshold` 的门槛，库**永远长不大**；
（b）淘汰又按 `reuse == 0` 杀老引理，库**永远长不住**。现在把"准入"与"淘汰"
分开，并给每条引理记"被给过几次机会"（`exposures`）——**没被给过机会的引理不淘汰**，
否则淘汰的就不是僵尸，而是没抽到签的人。

## 关于"边际增益"的忠实度（必须写清楚，别过度主张）

真算边际增益要对每条候选重跑两臂，代价过高。工程做法是用 `score(l)` 当代理，
并要求校验代理的忠实度（抽若干条同时算真实边际增益与代理值，报 Spearman；
低于 0.5 就换代理）。`spearman` 就是给这件事用的。

**代理忠实度没测之前，不要声称有近似保证**——保证只对"每条候选有冻结覆盖集合、
目标函数是并集"的抽象问题成立，对"随机语言模型的真实 pass@k"不成立。
"""

from __future__ import annotations

import collections

#: 复用判据的默认门槛：被至少 2 个不同目标引用过才算"可复用"
#: （与 `CostPerReusable` 的分母口径一致）。
DEFAULT_REUSE_THRESHOLD = 2
#: 一轮最多让多少条"还没有复用证据"的新引理入库（探索额度）。
DEFAULT_EXPLORATION_SLOTS = 8


def estimate_tokens(text: str) -> int:
    """便宜的 token 估计（与 `retrieval.estimate_tokens` 同口径）。"""
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def render_tokens(entry: dict) -> int:
    """`cost(l)`：把一条引理渲染进提示词后的 token 数。

    提示词里的形态是 `- \\`sgs_lem_i\\` : <stmt>`（见 `prompts.solve_prompt`），
    所以估算要把名字与包装算进去——只算 `stmt` 会系统性低估短引理的成本。
    """
    name = str(entry.get("name") or "sgs_lem_?")
    stmt = str(entry.get("stmt") or "")
    return estimate_tokens(f"- `{name}` : {stmt}")


def cost_of(entry: dict) -> int:
    """一条引理进提示词的成本：优先用离线写好的 `cost_tokens`，缺了才现算。"""
    stored = entry.get("cost_tokens")
    return int(stored) if stored else render_tokens(entry)


def density(entry: dict) -> float:
    """`score(l) = reuse(l) / cost(l)`；`reuse` 缺失按 0（未测量 = 还没有复用证据）。"""
    cost = float(cost_of(entry))
    reuse = float(entry.get("reuse") or 0.0)
    return reuse / cost if cost > 0 else 0.0


def is_reusable(entry: dict, threshold: int = DEFAULT_REUSE_THRESHOLD) -> bool:
    """这条引理有没有"已被证明可复用"的证据。"""
    return int(entry.get("reuse") or 0) >= threshold


def is_exploring(entry: dict, round_index: int | None, evict_after: int) -> bool:
    """这条引理还在探索期吗（入库不足 `evict_after` 轮）。

    `round_index=None` 表示调用方没给轮次（例如纯函数测试）——此时**不算**在探索期，
    避免"没有轮次信息"被当成"永远新"。
    """
    if round_index is None:
        return False
    added = entry.get("added_round")
    if added is None:
        return False
    return (int(round_index) - int(added)) <= evict_after


# ─────────────────────── 密度贪心（提示词预算口径） ───────────────────────


def reuse_cost_greedy(
    pool: list[dict],
    ctx_budget: int,
    count_budget: int | None = None,
) -> tuple[list[dict], list[float], dict]:
    """token 预算下的密度贪心：按"边际增益 ÷ 成本"挑引理，并与最优单条取较优。

    边际增益的代理：**同一批候选之间不重复计同一目标的覆盖**（并集形式），
    即每条引理贡献的目标集合是它被引用过的目标集合 `entry["reuse_targets"]`；
    缺这个字段时退化成"贡献 `reuse` 的数值"（仍单调，但不再是并集）。

    只有 `reuse > 0` 的条目参与——它是**有证据**的那一批的排序工具；
    没有证据的新引理走 `select_by_reuse` 的探索额度，不从这里进。

    返回 `(选中列表, 每轮边际增益, 诊断)`；诊断里带 `mode`（`density` / `best_single`）。
    """
    items = []
    for entry in pool:
        cost = cost_of(entry)
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
    greedy_cost = sum(cost_of(e) for e in chosen)

    # 基准：只取一条最优的。**必须用同一个目标函数比较**——规格 5.5 节要的是
    # "与最优单条取较优"，而目标函数是覆盖增益；拿单条的 `reuse` 标量去比贪心的
    # 并集增益是拿两个不同的量比大小（实测踩过：reuse=5 的单条会把真正更优的两条顶掉）。
    single, single_gain, single_cost = None, 0.0, 0
    for item in items:
        # **基准也必须守预算**：否则"最优单条"会选出一条放不进预算的引理。
        if item["cost"] > ctx_budget:
            continue
        gain = float(len(item["covered"])) if item["covered"] else float(item["reuse"])
        cost = int(item["cost"])
        if single is None or gain > single_gain or (gain == single_gain and cost < single_cost):
            single, single_gain, single_cost = item["entry"], gain, cost

    mode = "density"
    if single is not None and (single_gain > greedy_value
                               or (single_gain == greedy_value and single_cost < greedy_cost)):
        chosen, gains, mode = [single], [single_gain], "best_single"
        greedy_cost = single_cost
    diagnostics = {
        "mode": mode,
        "greedy_value": round(greedy_value, 4),
        "best_single_value": round(single_gain, 4),
        "budget": ctx_budget,
        "remaining": ctx_budget - greedy_cost,
        "cost": sum(cost_of(e) for e in chosen),
    }
    return chosen, gains, diagnostics


# ─────────────────────── 注入/保留：证据 + 探索额度 ───────────────────────


def select_by_reuse(
    pool: list[dict],
    ctx_budget: int,
    threshold: int = DEFAULT_REUSE_THRESHOLD,
    count_budget: int | None = None,
    round_index: int | None = None,
    evict_after: int = 3,
    exploration: int = DEFAULT_EXPLORATION_SLOTS,
) -> tuple[list[dict], list[float], dict]:
    """选**注入提示词 / 保留在库**的引理集合（规格 5.5 的选择问题）。

    优先级（写死，避免临时争论）：

    1. **有复用证据的**（`reuse >= threshold`）：按 `reuse/cost` 密度贪心；
    2. **探索期的**（入库不足 `evict_after` 轮）：按库内顺序补位，最多 `exploration` 条
       ——没有这一步，库空时永远选不出东西，闭环冷启动不了（这是上一版的死锁）；
    3. 两批都空（例如库里的引理全都超龄且 reuse=0）：**如实返回空集**，
       并在诊断里标 `"empty"`，不要偷偷放宽门槛。

    返回 `(选中, 每轮增益, 诊断)`；诊断里给出被门槛拦下的条数、探索额度用量与模式，
    便于回答"库为什么没长大 / 提示词里为什么没有引理"。
    """
    reusable = [e for e in pool if is_reusable(e, threshold)]
    reusable_ids = {id(e) for e in reusable}
    exploring = [e for e in pool
                 if id(e) not in reusable_ids and is_exploring(e, round_index, evict_after)]
    blocked = len(pool) - len(reusable) - len(exploring)

    slots = None if count_budget is None else max(0, count_budget)
    chosen, gains, diagnostics = reuse_cost_greedy(reusable, ctx_budget, slots)
    used_tokens = sum(cost_of(e) for e in chosen)
    used_slots = len(chosen)

    # 预算与条数都还有余量时，用探索额度补位（按库内顺序，保证可复现）
    extra = 0
    for entry in exploring:
        if extra >= max(0, exploration):
            break
        if slots is not None and used_slots >= slots:
            break
        cost = cost_of(entry)
        if used_tokens + cost > ctx_budget:
            continue
        chosen.append(entry)
        gains.append(0.0)
        used_tokens += cost
        used_slots += 1
        extra += 1

    diagnostics.update({
        "reuse_threshold": threshold,
        "pool": len(pool),
        "reusable": len(reusable),
        "exploring": len(exploring),
        "exploration_used": extra,
        "blocked_by_threshold": blocked,
        "selected": len(chosen),
    })
    return chosen, gains, diagnostics


def exploration_admission(
    verified: list[dict],
    library_size: int,
    library_budget: int,
    exploration: int = DEFAULT_EXPLORATION_SLOTS,
) -> tuple[list[dict], dict]:
    """**准入**：本轮验证通过的候选里，哪些允许入库。

    新引理的 `reuse` 按构造是 0（它还没被任何目标引用过），所以**不能**用
    `reuse >= threshold` 当准入门槛——那会让库永远长不大。准入只用两条约束：

    * 库容（`library_budget - library_size`）；
    * 探索额度（`exploration`，防止一轮灌进太多没证据的引理）。

    返回 `(准入列表, 诊断)`。
    """
    room = max(0, library_budget - library_size)
    quota = min(room, max(0, exploration))
    admitted = list(verified)[:quota]
    diagnostics = {
        "verified": len(verified),
        "library_size": library_size,
        "library_budget": library_budget,
        "room": room,
        "exploration_quota": quota,
        "admitted": len(admitted),
        "dropped_by_quota": max(0, len(verified) - len(admitted)),
    }
    return admitted, diagnostics


# ─────────────────────── 淘汰 ───────────────────────


def is_evictable(entry: dict, round_index: int, evict_after: int = 3) -> bool:
    """僵尸判据：`reuse=0` **且被给过机会**（进过提示词）**且**超龄。

    第三条"被给过机会"很关键：提示词预算有限，库里的引理不一定每轮都进提示词。
    若按"超龄即淘汰"，淘汰的就不是僵尸而是"没抽到签的人"——那些引理**没有机会**
    被引用，`reuse=0` 是装置造成的，不是它没用。
    """
    if int(entry.get("reuse") or 0) != 0:
        return False
    if int(entry.get("exposures") or 0) <= 0:
        return False
    added = entry.get("added_round")
    age = round_index - int(added) if added is not None else round_index + 1
    return age > evict_after


def evict(
    library: list[dict],
    round_index: int,
    evict_after: int = 3,
) -> tuple[list[dict], list[dict]]:
    """淘汰：僵尸引理移入冷存（**不删除**）。返回 `(保留, 冷存)`。"""
    kept: list[dict] = []
    evicted: list[dict] = []
    for entry in library:
        if is_evictable(entry, round_index, evict_after):
            evicted.append(entry | {"status": "cold", "evicted_round": round_index})
        else:
            kept.append(entry)
    return kept, evicted


# ─────────────────────── 诊断工具 ───────────────────────


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


def reuse_table(entries: list[dict]) -> dict:
    """复用分布摘要（报告用）：有多少条达到门槛、有多少条还在探索期。"""
    counter = collections.Counter()
    for entry in entries:
        reuse = int(entry.get("reuse") or 0)
        counter["reusable" if reuse >= DEFAULT_REUSE_THRESHOLD else
                ("used_once" if reuse == 1 else "unused")] += 1
    return {
        "size": len(entries),
        "reuse_histogram": dict(sorted(collections.Counter(
            int(e.get("reuse") or 0) for e in entries).items())),
        "buckets": dict(counter),
    }
