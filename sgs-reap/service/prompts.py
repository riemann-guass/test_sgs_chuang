"""提示词与解析器。

Conjecturer 与 Guide 的口径移植自 SGS：
  * `sgs/utils/prompts.py:get_deepseek_prover_v2_conjecturer_prompt`
  * `sgs/utils/prompts.py:get_guide_prompt` 与三个 `<begin_*_score>` 解析器
  * `sgs/models/guide/llm_judge_guide.py:sub_scores_to_review`

与 SGS 的差异（推理期适配，见 docs/api-contract.md）：
  1. Conjecturer 不再输出完整 `theorem ... := by sorry`，而是输出 **`have`-ready 的命题**，
     只允许引用当前局部上下文里的变量；
  2. 一次请求返回多条候选（推理模型的 reasoning token 很贵，合并调用比多次调用便宜）；
  3. Guide 的 seed 由"完整定理"变成"当前目标"，这是已知的分布偏移，需要在闸门 M1 中评估。
"""

from __future__ import annotations

import re

NO_RELEVANCE_SCORE_FOUND = -1113.0
NO_REDUNDANCY_SCORE_FOUND = -1111.0
NO_CONCLUSION_COMPLEXITY_SCORE_FOUND = -1112.0
NO_GUIDE_SCORE_FOUND = -1234567890.0


def conjecture_prompt(goal_state: str, num_samples: int) -> str:
    return (
        "You are helping a Lean 4 theorem prover. Here is the CURRENT PROOF STATE "
        "(the line starting with `⊢` is the main goal; everything above it is the local context):\n\n"
        "```lean4\n"
        f"{goal_state.strip()}\n"
        "```\n\n"
        f"Propose {num_samples} DIFFERENT auxiliary lemma(s) that would help prove the main goal. "
        "Each lemma must satisfy ALL of the following:\n"
        "1. It is a Lean 4 PROPOSITION (a type), not a theorem declaration: no `theorem`/`lemma` keyword, "
        "no name, no proof, no `sorry`.\n"
        "2. It only mentions variables and hypotheses that already appear in the local context above; "
        "introduce any extra variables with `∀` and their assumptions with `→`.\n"
        "3. It is PROVABLE from the hypotheses already in the local context (do not invent assumptions).\n"
        "4. It is strictly simpler than the main goal, and useful for proving it.\n"
        "5. It must NOT be identical or equivalent (up to renaming of variables) to the main goal.\n\n"
        "Output each proposition in its own ```lean4 code block, one proposition per block, "
        "and put nothing else inside the block."
    )


def extract_propositions(generation: str) -> list[str]:
    """抽取所有 ```lean4 代码块，规范化成单行命题。

    容忍模型偶尔仍输出 `theorem name ... : P := by sorry` 形式：此时尽量还原出命题部分；
    若无法还原，该条被丢弃，由 Lean 侧承担最终校验。
    """
    props: list[str] = []
    for block in re.findall(r"```(?:lean4|lean)\s*(.*?)```", generation, flags=re.DOTALL):
        text = block.strip()
        if not text:
            continue
        text = _strip_declaration(text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or text in ("sorry", "by sorry"):
            continue
        props.append(text)
    return props


def _strip_declaration(text: str) -> str:
    """若模型输出了完整定理声明，尝试还原成命题。"""
    text = text.strip()
    for marker in (":= by", ":="):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    match = re.match(r"^\s*(?:@\[[^\]]*\]\s*)?(theorem|lemma|example)\b", text)
    if not match:
        return text
    text = text[match.end():].strip()
    parts = text.split(None, 1)
    if len(parts) == 2 and ":" not in parts[0]:
        text = parts[1]
    binders: list[str] = []
    while text.startswith("("):
        depth = 0
        close = -1
        for i, ch in enumerate(text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close == -1:
            break
        binders.append(text[: close + 1])
        text = text[close + 1:].strip()
    while text[:1] in ("{", "["):
        close_ch = "}" if text[0] == "{" else "]"
        idx = text.find(close_ch)
        if idx == -1:
            break
        binders.append(text[: idx + 1])
        text = text[idx + 1:].strip()
    if text.startswith(":"):
        text = text[1:].strip()
    if not binders:
        return text
    return " ".join(binders) + " → " + text


def guide_prompt(target: str, conjecture: str) -> str:
    """SGS `get_guide_prompt` 的移植：seed 用当前目标，conjecture 用候选命题。"""
    return f"""You are a math expert. Here is a seed lean4 problem statement:

```lean4
{target}
```

Here is a lemma or related lemma that is supposed to be useful for proving the above statement. It can be useful in that either it is a lemma
that can be directly used to help prove the above statement, or it is a related lemma that plausible requires similar proof techniques to solve.

```lean4
{conjecture}
```

Please rate the relevance of the lemma to the seed problem on a scale of 0 to 5, where 0 is "not at all related" and 5 is "very useful for proving the target statement" If
the lemma is trivial, you should give it a low score.

Here is a rubric for how to score the lemma:
- 0: The lemma is not at all related to the seed problem and is trivial to prove. OR the lemma is identical (including equivalent by renaming of premises and variables) to the seed problem.
- 1: The lemma is not at all related to the seed problem.
- 2: The lemma is related to the seed problem in that it concerns a similar subfield of mathematics, but is not directly useful for proving the seed problem.
- 3: The lemma is related to the seed problem and may be useful for proving the seed problem.
- 4: The lemma is directly useful for solving the seed problem. That is if the lemma was proved, the seed problem would be easier to solve.
- 5: The lemma is very useful for solving the seed problem, and solving the lemma will dramatically reduce the difficulty of the original seed problem

Next decide how redundant the premises are. Rate this as 0 or 1:

- 0: There are no redundant premises.
- 1: There are redundant premises. That is premises that are not needed to prove the conclusion.

Next decide if the conclusion is overly complex. Rate this on a score of 0 to 4:

- 0: The conclusion is minimally complex. That is it is a single, atomic statement (e.g., a simple equality, inequality, or property) that is maximally clear and easy to apply to other problems.
- 1: The conclusion has low complexity. That is it has multiple related parts (e.g., 2-3 conjunctions) but they form a cohesive statement where all parts directly relate to each other and the premises, making it still straightforward to apply.
- 2: The conclusion has moderate complexity. That is it contains disjunctions (or clauses) but they are closely related alternatives, or it contains multiple conjunctions (3-4) that are all on-theme. The structure is clear but requires some thought to apply.
- 3: The conclusion has high complexity. That is it contains multiple unrelated or clauses (2-3 disjunctions), or contains deep nesting of logical operators that obscure the main claim. Different parts address somewhat different aspects with weak connections to each other or the premises, making it difficult to understand when and how to apply.
- 4: The conclusion has very high complexity. That is it is a disjunction of many (3 or more) largely unrelated clauses, or contains deeply nested logical structure that is hard to parse. Parts address completely different mathematical objects or properties which would make it nearly impossible to apply meaningfully to other problems. It feels like multiple lemmas packaged as one.

Once you are done reasoning about all of these things, output the scores between tags:

1) Relevance score: <begin_relevance_score> <end_relevance_score>
2) Redundancy score: <begin_redundancy_score> <end_redundancy_score>
3) Conclusion complexity score: <begin_conclusion_complexity_score> <end_conclusion_complexity_score>
"""


def _extract_tagged_float(text: str, tag: str, default: float) -> float:
    start = text.rfind(f"<begin_{tag}>")
    end = text.rfind(f"<end_{tag}>")
    if start == -1 or end == -1 or end <= start:
        return default
    chunk = text[start + len(f"<begin_{tag}>") : end]
    match = re.search(r"[-+]?\d*\.?\d+", chunk)
    if not match:
        return default
    try:
        return float(match.group())
    except ValueError:
        return default


def parse_guide_scores(generation: str) -> dict | None:
    relevance = _extract_tagged_float(generation, "relevance_score", NO_RELEVANCE_SCORE_FOUND)
    redundancy = _extract_tagged_float(generation, "redundancy_score", NO_REDUNDANCY_SCORE_FOUND)
    complexity = _extract_tagged_float(
        generation, "conclusion_complexity_score", NO_CONCLUSION_COMPLEXITY_SCORE_FOUND
    )
    if NO_RELEVANCE_SCORE_FOUND in (relevance, redundancy, complexity):
        return None
    if NO_REDUNDANCY_SCORE_FOUND in (relevance, redundancy, complexity):
        return None
    if NO_CONCLUSION_COMPLEXITY_SCORE_FOUND in (relevance, redundancy, complexity):
        return None
    return {
        "relevance": relevance,
        "redundancy": redundancy,
        "complexity": complexity,
        "review": sub_scores_to_review(relevance, complexity, redundancy),
    }


def sub_scores_to_review(relevance: float, complexity: float, redundancy: float) -> float:
    """SGS 原式：complexity ∈ {3,4} 直接判 0。"""
    if complexity in (3.0, 4.0):
        return 0.0
    return max(0.0, relevance + (2.0 - complexity) + (1.0 - redundancy))
