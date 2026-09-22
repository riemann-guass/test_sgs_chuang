"""分层检索（P1，规格第 3.4 节）。

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
