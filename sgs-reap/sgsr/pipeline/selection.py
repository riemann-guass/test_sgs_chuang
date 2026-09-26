"""选择层：分层检索（规格 3.4）+ 引理选择/准入/淘汰（规格 5.5）。

合并自旧 `pipeline/retrieval.py` + `pipeline/coverage.py`——两者回答的是同一个问题
的上下半段："给定当前命题，**挑哪些引理**"：检索层从库/Mathlib 里取出候选并排序，
选择层决定谁进提示词、谁被准入、谁该淘汰。分成两个模块只会让"排序口径"漂成两套。

下面是第一部分：分层检索。

```
第一层（精排层）：从**我们自己的库** L 里取前 n1 条，按 score 降序。
                 库小于 200 条时 score 用廉价的符号重叠；超过 200 条再换嵌入检索。
第二层（兜底层）：调用**外部**前提检索工具（LeanSearch v2 API / 自托管 LeanExplore）。
                 **不自己训练检索器**——这条赛道已被充分占据，自研期望收益低。
```

两条硬约束（都在这里实现，调用方不用再管）：

* 两类前提拼进提示词的总 token 数不得超过 `budget`（默认 1200），超出则按分数截断；
* 外部检索不可用时**降级跳过第二层**并记 `degraded=True`，绝不因此中断求解。

## 为什么第一层用"符号重叠"而不是相似度模型

库在几十条量级时，符号重叠（两个命题共有的标识符比例）已经足够把"提到同一批常量"的
引理排到前面；引入嵌入模型要额外付一次前向、还要维护向量库，而这条赛道上
LeanSearch / LeanExplore / LeanPremise 已经把检索本身做到位了——我们的第二层直接用它们。
重叠打分只是**在自有库上**排序用的便宜代理，等库超过 200 条再按规格换成嵌入。

## 与复用判据的关系（规格 5.5 节）

库条目若带 `reuse` / `cost_tokens`（离线建库时会写），排序首先按 `reuse/cost_tokens`
降序——那才是项目的方法性判据；只有缺失这两个字段时才退回符号重叠。
这样"检索层"与"选择层"用的是同一个量，不会出现两套排序口径。
"""

from __future__ import annotations

import collections
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Lean 语法与常见记号：比较两个命题时它们是噪声，会把"都在 Nat 上"当成相似。
_STOPWORDS = {
    "forall", "fun", "theorem", "lemma", "example", "def", "by", "sorry", "admit",
    "prop", "type", "true", "false", "intro", "exact", "rw", "simp", "apply",
    "nat", "int", "real", "bool", "rat", "complex", "hof", "h", "h1", "h2", "hyp",
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")


@dataclass
class Premise:
    """一条可注入提示词的前提。"""

    name: str
    statement: str
    source: str          # "library" | "mathlib"
    tokens: int = 0
    score: float = 0.0
    #: 库条目的 `reuse/cost` 密度（规格 5.5 的 score(l)）；外部检索没有这个量。
    #: 它才是项目的方法性判据，**排序必须用它**——以前 `retrieve()` 最后按
    #: `score`（符号重叠）重排，把库层好不容易按密度排好的顺序整个丢掉了。
    density: float | None = None


@dataclass
class RetrievalResult:
    """检索结果 + 降级状态。`premises` 经 `as_prompt_items` 喂给 `/solve` 的 `library` 字段。"""

    premises: list[Premise] = field(default_factory=list)
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    library_candidates: int = 0
    mathlib_candidates: int = 0

    def as_prompt_items(self) -> list[dict]:
        return [{"name": p.name, "stmt": p.statement} for p in self.premises]


def estimate_tokens(text: str) -> int:
    """便宜的 token 估计（规格 5.5 节的 `cost(l)` 用同一个口径）。

    没有后端分词器可用（我们只调 API，不加载模型），所以按字节长度做保守估计：
    ASCII 约 4 字符/token。**这是估计不是测量**，报告里要如实写明；
    它的用途是"截断预算"，差一两条不影响正确率，但让预算可控。
    """
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def symbols(text: str) -> set[str]:
    """抽出命题里的标识符集合（小写化、去停用词）。"""
    return {
        match.group().lower()
        for match in _IDENT_RE.finditer(text or "")
        if match.group().lower() not in _STOPWORDS
    }


def overlap_score(stmt: str, query_symbols: set[str]) -> float:
    """符号重叠：查询与命题共有的标识符占查询标识符的比例（0–1）。"""
    if not query_symbols:
        return 0.0
    shared = symbols(stmt) & query_symbols
    return len(shared) / len(query_symbols)


def _reuse_density(row: dict) -> float | None:
    """库条目的 `reuse/cost_tokens`（规格的 score(l)）；字段缺失时返回 None。"""
    reuse = row.get("reuse")
    cost = row.get("cost_tokens")
    if reuse is None or not cost:
        return None
    return float(reuse) / float(cost)


def retrieve_library(stmt: str, lib: list[dict], n: int = 8) -> list[Premise]:
    """第一层：从自有库里排序取前 `n` 条。

    排序键是 `(复用密度, 符号重叠)`——把"项目判据"放在第一位，
    但**不因此丢掉**没有复用记录的条目（新入库的引理在前几轮还没被测量过，
    直接扔掉等于它永远测不到）。缺 `reuse`/`cost_tokens` 时第一键退回重叠，
    于是排序键退化成 `(重叠, 重叠)`，与"只用重叠"等价。
    """
    query = symbols(stmt)
    scored: list[tuple[tuple[float, float], int, dict]] = []
    for index, row in enumerate(lib):
        text = str(row.get("stmt", "")).strip()
        if not text:
            continue
        overlap = overlap_score(text, query)
        density = _reuse_density(row)
        key = (density if density is not None else overlap, overlap)
        scored.append((key, index, row))
    # 同分时按库内顺序（稳定），保证检索可复现
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    out: list[Premise] = []
    for _, index, row in scored[: max(0, n)]:
        text = str(row["stmt"]).strip()
        out.append(
            Premise(
                name=str(row.get("name") or f"sgs_lem_{index + 1}"),
                statement=text,
                source="library",
                tokens=estimate_tokens(text),
                score=overlap_score(text, query),
                density=_reuse_density(row),
            )
        )
    return out


def retrieve_mathlib(
    stmt: str,
    n: int = 8,
    timeout_ms: int = 10_000,
    endpoint: str | None = None,
) -> list[Premise]:
    """第二层：外部前提检索（LeanSearch v2 / LeanExplore）。

    **默认关闭**（`endpoint=None`）：本机没有配置外部检索服务，而规格明确要求
    "外部检索不可用则跳过第二层并记 `retrieval_degraded=true`，不得因此中断求解"。
    因此这里的行为是：给定 `endpoint` 才发请求，否则立刻返回空列表；
    失败（网络/超时/格式不对）也返回空列表——**永不抛异常**。

    调用方用 `retrieve()` 的 `degraded` 字段区分"没配"与"配了但挂了"：
    两者都会让 Mathlib 那层为空，但报告里的解释不同。
    """
    if not endpoint:
        return []
    payload = json.dumps({"query": stmt, "num_results": n}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout_ms / 1000.0)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        return []
    out: list[Premise] = []
    for item in results[: max(0, n)]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("statement") or item.get("stmt") or "").strip()
        if not text:
            continue
        out.append(
            Premise(
                name=str(item.get("name") or item.get("full_name") or "?"),
                statement=text,
                source="mathlib",
                tokens=estimate_tokens(text),
                score=float(item.get("score") or 0.0),
                density=None,
            )
        )
    return out


def retrieve(
    stmt: str,
    lib: list[dict] | None = None,
    budget: int = 1_200,
    n1: int = 8,
    n2: int = 8,
    mathlib_endpoint: str | None = None,
    mathlib_timeout_ms: int = 10_000,
) -> RetrievalResult:
    """分层检索入口：先库、后外部，按 token 预算截断。

    截断规则：按 `score` 降序依次收入，收不下就**跳过该条继续试后面的**
    （而不是遇到第一条超预算就整体停下）——一条长引理不该把后面所有短引理挡掉。
    库层优先于 Mathlib 层（同分时稳定排序里库在前）：自有的、被验证过的、
    带复用记录的引理才是我们想让它用的。
    """
    library_rows = list(lib or [])
    library_premises = retrieve_library(stmt, library_rows, n=n1)
    mathlib_premises = retrieve_mathlib(
        stmt, n=n2, timeout_ms=mathlib_timeout_ms, endpoint=mathlib_endpoint
    )
    result = RetrievalResult(
        library_candidates=len(library_rows),
        mathlib_candidates=len(mathlib_premises),
        degraded=not mathlib_endpoint,
        notes=[] if mathlib_endpoint else ["未配置外部检索端点（第二层跳过）"],
    )
    if library_rows and not any(row.get("reuse") is not None for row in library_rows):
        # 库里有引理但一条复用记录都没有：排序只能退回符号重叠。这不是错（第一轮
        # 本来就还没有复用证据），但必须写在报告里，否则"库没起作用"无法归因。
        result.notes.append(
            f"库 {len(library_rows)} 条均无 reuse 记录，库层排序退回符号重叠"
        )
    if mathlib_endpoint and not mathlib_premises:
        result.notes.append("外部检索返回空（端点不可用或没有命中）")

    ranked = sorted(
        library_premises + mathlib_premises,
        # 第一键是项目判据（reuse/cost 密度），第二键是符号重叠，第三键让库优先。
        key=lambda p: (p.density if p.density is not None else -1.0,
                       p.score, p.source == "library"),
        reverse=True,
    )
    remaining = budget
    for premise in ranked:
        if premise.tokens > remaining:
            result.notes.append(f"跳过超预算前提 {premise.name}（{premise.tokens} > {remaining}）")
            continue
        result.premises.append(premise)
        remaining -= premise.tokens
    if result.premises:
        result.notes.append(
            f"注入 {len(result.premises)} 条前提，约 {budget - remaining} token"
        )
    return result


# ═══════════════════════ 第二部分：引理选择/准入/淘汰（旧 pipeline/coverage.py） ═══════════════════════
#
# 本项目的**唯一方法性改动**就在这一段（规格 5.5 节）：
#
#     score(l) = reuse(l) / cost(l)
#     reuse(l) = l 被多少个**不同目标**的**通过验收的**证明实际引用
#     cost(l)  = l 渲染进提示词的 token 数
#
# ## 三个量各有各的用途（不要混用，混用正是这一版要修的错）
#
# | 量 | 谁在用 | 语义 |
# |---|---|---|
# | `reuse >= threshold` | **成本口径**（`CostPerReusable` 的分母）与**保留/淘汰** | "已经被证明可复用" |
# | `reuse/cost` 密度贪心 | **提示词注入**（token 预算下的选择） | "同等预算下先塞谁" |
# | 探索额度 + 曝光计数 | **准入**与**给机会** | 新引理还没有复用证据，**不能**用它没证据来拒它 |
#
# 这三件事曾经被挤进一个 `select_by_reuse` 里，后果是致命的：新引理的 `reuse`
# 按构造是 0，于是（a）它永远过不了 `reuse >= threshold` 的门槛，库**永远长不大**；
# （b）淘汰又按 `reuse == 0` 杀老引理，库**永远长不住**。现在把"准入"与"淘汰"
# 分开，并给每条引理记"被给过几次机会"（`exposures`）——**没被给过机会的引理不淘汰**，
# 否则淘汰的就不是僵尸，而是没抽到签的人。
#
# ## 关于"边际增益"的忠实度（必须写清楚，别过度主张）
#
# 真算边际增益要对每条候选重跑两臂，代价过高。工程做法是用 `score(l)` 当代理，
# 并要求校验代理的忠实度（抽若干条同时算真实边际增益与代理值，报 Spearman；
# 低于 0.5 就换代理）。`spearman` 就是给这件事用的。
#
# **代理忠实度没测之前，不要声称有近似保证**——保证只对"每条候选有冻结覆盖集合、
# 目标函数是并集"的抽象问题成立，对"随机语言模型的真实 pass@k"不成立。
#
# `estimate_tokens` 与检索层共用上面那一个定义（口径必须一致，这里不再抄一份）。

#: 复用判据的默认门槛：被至少 2 个不同目标引用过才算"可复用"
#: （与 `CostPerReusable` 的分母口径一致）。
DEFAULT_REUSE_THRESHOLD = 2
#: 一轮最多让多少条"还没有复用证据"的新引理入库（探索额度）。
DEFAULT_EXPLORATION_SLOTS = 8


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
