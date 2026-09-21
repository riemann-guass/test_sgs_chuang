"""repair 定向重试（P1，待实现）。

规格见 `docs/SG-Lean思路文档第二版.pdf` 3.7 节。

核心思路：内核返回的是**结构化失败码**加 Lean 错误原文，比一句「失败」信息量大得多。
把 `[失败码 + 错误原文 + 原命题]` 按固定模板回灌给求解器，要求它针对这个错误重写。

停止条件：轮数上限 R（默认 2）或 token 预算耗尽。
若上一轮全部是 `mvar_or_sorry`，提示词中追加「不要使用 sorry / admit」的硬指令。
"""

from __future__ import annotations


def repair(stmt: str, failed: list[dict], k: int = 4, endpoint: str | None = None) -> list[str]:
    """给定失败记录，产出新一轮 k 篇候选证明脚本。

    `failed` 的每项形如 `{"proof": str, "reason": str, "detail": str}`。
    """
    raise NotImplementedError("P1：见 docs/SG-Lean思路文档第二版.pdf 3.7 节")
