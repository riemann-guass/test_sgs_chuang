"""在线求解主流程（P1，待实现）。

规格见 `docs/SG-Lean思路文档第二版.pdf` 第 3 节「单题求解流程」与附录 B「函数签名清单」。
九个步骤：输入解析 -> 门检 -> 廉价 tactic 兜底 -> 分层检索 -> 求解 k 篇
          -> 内核验证 -> repair 定向重试 -> 输出与记账 -> 预算与停止条件。

硬规则（不可协商）：
* 未通过内核终检的脚本一律不出现在 `ProofResult.proof` 中；
* 后端不可用（503／超时）必须记为 `backend_error`，不得当成「模型证不出」；
* 所有步骤都要记账（调用次数、token、Lean 墙钟），否则成本指标失去意义。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Budget:
    """单题的预算上限。默认值见规格文档 3.9 节。"""

    total_tokens: int = 20_000
    k: int = 4
    repair_rounds: int = 2
    ctx_lemma_tokens: int = 1_200


@dataclass
class Attempt:
    """一次候选证明及其判定结果。"""

    proof: str
    reason: str = ""
    detail: str = ""


@dataclass
class ProofResult:
    """单题求解的完整记录，字段见规格文档附录 A 的 RunRecord。"""

    solved: bool
    proof: str | None = None
    path: str = ""          # cheap | solve | repair | failed
    attempts: list[Attempt] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    lean_ms: int = 0


class Prover:
    """证明器本体。构造参数与 `solve` 的签名见规格文档附录 B。"""

    def __init__(self, endpoint: str, backend: str = "deepseek", library_path: str | None = None) -> None:
        raise NotImplementedError("P1：见 docs/SG-Lean思路文档第二版.pdf 第 3 节")

    def prove(self, stmt: str, budget: Budget | None = None) -> ProofResult:
        raise NotImplementedError("P1：见 docs/SG-Lean思路文档第二版.pdf 第 3 节")

    def parse_input(self, stmt: str | None = None, lean_file: str | None = None) -> dict:
        """抽取命题并闭包成全称量化形式（规格 3.1）。"""
        raise NotImplementedError("P1：见 docs/SG-Lean思路文档第二版.pdf 3.1 节")
