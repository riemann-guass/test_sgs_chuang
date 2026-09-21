"""分层检索（P1，待实现）。

规格见 `docs/SG-Lean思路文档第二版.pdf` 3.4 节。

第一层：从我们自己的库里取前 n1 条，按 `score = reuse/cost` 降序（库小于 200 条时先用廉价符号重叠）。
第二层：调用**外部**前提检索工具（LeanSearch v2 的 API，或自托管的 LeanExplore）取前 n2 条。
        **不自己训练检索器**——这条赛道已被充分占据，自研期望收益低。

两条硬约束：
* 两类前提拼进提示词的总 token 数不得超过 `CTX_LEMMA_BUDGET`（默认 1200）；
* 外部检索不可用时降级跳过第二层并记 `retrieval_degraded=true`，不得中断求解。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Premise:
    """一条可注入提示词的前提。"""

    name: str
    statement: str
    source: str          # "library" | "mathlib"
    tokens: int = 0
    score: float = 0.0


def retrieve(stmt: str, lib: list[dict], budget: int = 1_200) -> list[Premise]:
    """分层检索入口：先库、后外部，按预算截断。"""
    raise NotImplementedError("P1：见 docs/SG-Lean思路文档第二版.pdf 3.4 节")


def retrieve_mathlib(stmt: str, n: int = 8, timeout_ms: int = 10_000) -> list[Premise]:
    """外部前提检索（LeanSearch v2 / LeanExplore）。失败时返回空列表并记降级。"""
    raise NotImplementedError("P1：见 docs/SG-Lean思路文档第二版.pdf 3.4 节")
