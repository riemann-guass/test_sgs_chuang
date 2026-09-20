"""诊断 Guide 的判定：对比「把证明状态当 seed」与「把定理语句当 seed」两种 target 形式。

动机：首轮冒烟中 Guide 把最有用的候选 `∀ k : ℕ, 2 ∣ k * (k + 1)` 判成 relevance=0，
而它是证明 `2 ∣ n^2 + n` 的关键引理。这里直接看原始输出，判断是 target 形式问题还是
Guide 迁移失效——这正是闸门 M1 要回答的问题。

用法：
    python probe_guide.py
"""

from __future__ import annotations

import json

from sgsr.models import config
from sgsr.models import prompts
from sgsr.models.backend import Backend

GOAL_STATE = "n : ℕ\n⊢ 2 ∣ n ^ 2 + n"
THEOREM_FORM = "theorem goal (n : ℕ) : 2 ∣ n ^ 2 + n := by"

CANDIDATES = [
    "∀ k : ℕ, 2 ∣ k * (k + 1)",
    "n % 2 = 0 → 2 ∣ n ^ 2 + n",
]


def main() -> int:
    config.ensure_utf8_stdout()
    backend = Backend()
    print(f"[probe-guide] model={backend.model} thinking={not backend.disable_thinking}")

    for label, target in (("proof-state", GOAL_STATE), ("theorem-form", THEOREM_FORM)):
        print(f"\n[probe-guide] ===== target 形式: {label} =====")
        for candidate in CANDIDATES:
            text, meta = backend.chat(
                [{"role": "user", "content": prompts.guide_prompt(target, candidate)}],
                max_tokens=4096,
                temperature=1.0,
            )
            parsed = prompts.parse_guide_scores(text)
            print(f"  candidate: {candidate}")
            print(f"    -> {json.dumps(parsed, ensure_ascii=False)}")
            print(f"    理由片段: {text.strip().replace(chr(10), ' ')[:220]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
