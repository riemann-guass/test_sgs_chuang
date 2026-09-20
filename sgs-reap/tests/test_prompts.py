"""提示词的离线单测（不联网、不花钱）。

为什么单独测提示词：mock 服务不构造提示词，所以 `/conjecture` 的端到端测试只能验证
"需求被送到了端点"（meta 回显）。真正决定 N1 是否生效的是**提示词里有没有需求区块**，
而那属于纯函数，直接断言最可靠。

最关键的一条：**空需求必须等于没有需求**。H1 的对照就是"同预算、同模型，只去掉需求条件化"，
如果 `demand=[]` 仍然渲染出 `DEMAND:` 之类的占位，对照就不干净了。

    python tests\\test_prompts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

import prompts  # noqa: E402

GOAL = "n : ℕ\n⊢ 2 ∣ n ^ 2 + n"
DEMAND = ["n : ℕ ⊢ 2 ∣ n * (n + 1)", "n : ℕ ⊢ n % 2 = 0 ∨ n % 2 = 1"]
SEEDS = ["∀ (n : ℕ), n + 0 = n"]


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    return condition


def main() -> int:
    failures = 0
    print("[test_prompts] 需求区块")
    with_demand = prompts.conjecture_prompt(GOAL, 3, demand=DEMAND)
    failures += not check(
        "有需求时出现 BACKGROUND EVIDENCE 区块", "BACKGROUND EVIDENCE" in with_demand
    )
    failures += not check(
        "每条需求签名都出现在提示词里",
        all(sig in with_demand for sig in DEMAND),
    )
    failures += not check("明确禁止把证据当答案输出", "Do NOT output these subgoals" in with_demand)
    failures += not check(
        "要求引理必须提到目标里的符号", "MUST mention at least one symbol" in with_demand
    )

    print("[test_prompts] 无需求 = 无 DEMAND 区块（H1 的对照必须干净）")
    no_demand = prompts.conjecture_prompt(GOAL, 3)
    empty_demand = prompts.conjecture_prompt(GOAL, 3, demand=[])
    failures += not check("不传 demand 时没有证据区块", "BACKGROUND EVIDENCE" not in no_demand)
    failures += not check("demand=[] 时没有证据区块", "BACKGROUND EVIDENCE" not in empty_demand)
    failures += not check("demand=[] 与不传等价", no_demand == empty_demand)
    failures += not check("无需求时不出现第 7 条约束", "BACKGROUND EVIDENCE only as a hint" not in no_demand)
    failures += not check(
        "符号相关性约束（第 6 条）始终存在",
        "MUST mention at least one symbol" in no_demand,
    )

    print("[test_prompts] 库范例区块")
    with_seeds = prompts.conjecture_prompt(GOAL, 3, seeds=SEEDS)
    failures += not check("有范例时出现 LIBRARY EXCERPTS", "LIBRARY EXCERPTS" in with_seeds)
    failures += not check("范例语句出现在提示词里", SEEDS[0] in with_seeds)
    failures += not check("无范例时没有 LIBRARY EXCERPTS", "LIBRARY EXCERPTS" not in no_demand)

    print("[test_prompts] 基础约束始终存在")
    for keyword in ("PROPOSITION", "PROVABLE", "lean4"):
        failures += not check(f"含关键词 {keyword}", keyword in with_demand and keyword in no_demand)

    if failures:
        print(f"[test_prompts] FAIL（{failures} 处）")
        return 1
    print("[test_prompts] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
