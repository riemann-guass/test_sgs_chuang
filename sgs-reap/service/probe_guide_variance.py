"""量化 Guide 评分的方差，并检验 temperature=0 是否可用、是否更稳。

这是闸门 M1 的前置实验：Guide 是搜索先验的来源，它的稳定性直接决定先验是否可信。

用法：
    python probe_guide_variance.py
"""

from __future__ import annotations

import json
import statistics

import config
import prompts
from backend import Backend

GOAL_STATE = "n : ℕ\n⊢ 2 ∣ n ^ 2 + n"
CANDIDATES = [
    "∀ k : ℕ, 2 ∣ k * (k + 1)",
    "n % 2 = 0 → 2 ∣ n ^ 2 + n",
    "∀ a b : ℕ, 2 ∣ a → 2 ∣ a * b",
]
REPEATS = 5


def main() -> int:
    config.ensure_utf8_stdout()
    backend = Backend()
    print(f"[variance] model={backend.model} thinking={not backend.disable_thinking} repeats={REPEATS}")
    summary: dict = {}
    for temperature in (1.0, 0.0):
        print(f"\n[variance] ===== temperature = {temperature} =====")
        for candidate in CANDIDATES:
            reviews = []
            failures = 0
            for _ in range(REPEATS):
                # 每次重复都绕过缓存：加一个无害的前缀后缀扰动
                prompt = prompts.guide_prompt(GOAL_STATE, candidate) + "\n" + " " * (len(reviews) + 1)
                text, _meta = backend.chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=4096,
                    temperature=temperature,
                )
                parsed = prompts.parse_guide_scores(text)
                if parsed is None:
                    failures += 1
                else:
                    reviews.append(parsed["review"])
            if reviews:
                stats = (
                    f"n={len(reviews)} min={min(reviews):.0f} max={max(reviews):.0f} "
                    f"mean={statistics.mean(reviews):.2f} median={statistics.median(reviews):.1f} "
                    f"stdev={statistics.pstdev(reviews):.2f}"
                )
            else:
                stats = "no valid scores"
            print(f"  {candidate[:42]:44s} {stats}  (解析失败 {failures})")
            summary[f"T={temperature}|{candidate}"] = {
                "reviews": reviews,
                "failures": failures,
                "mean": statistics.mean(reviews) if reviews else None,
                "median": statistics.median(reviews) if reviews else None,
                "stdev": statistics.pstdev(reviews) if len(reviews) > 1 else None,
            }
    print("\n[variance] JSON:", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
