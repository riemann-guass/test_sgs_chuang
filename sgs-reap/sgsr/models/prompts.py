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

from sgsr.data import DECL_RE, close_declaration

NO_RELEVANCE_SCORE_FOUND = -1113.0
NO_REDUNDANCY_SCORE_FOUND = -1111.0
NO_CONCLUSION_COMPLEXITY_SCORE_FOUND = -1112.0
NO_GUIDE_SCORE_FOUND = -1234567890.0

#: 独立的 `by` 前缀（后面不是标识符字符）。`by_cases` / `by_contra` 不算前缀。
_BY_PREFIX_RE = re.compile(r"^by\b")


def conjecture_prompt(
    goal_state: str,
    num_samples: int,
    demand: list[str] | None = None,
    seeds: list[str] | None = None,
) -> str:
    """出题提示词。

    `demand`（N1）与 `seeds`（库范例）都是**可选区块**，且刻意做成"空列表 = 不出现"：
    这样 `demand=[]` 就是天然的消融对照组（同预算、同模型，只去掉需求条件化），
    H1 要比较的正是这两者。若把它们写成"DEMAND: (none)"之类的占位，
    对照就不再干净了。
    """
    parts: list[str] = [
        "You are helping a Lean 4 theorem prover. Here is the TARGET the prover is stuck on:\n\n"
        "```lean4\n"
        f"{goal_state.strip()}\n"
        "```\n"
    ]

    if demand:
        bullets = "\n".join(f"- {item}" for item in demand if str(item).strip())
        parts.append(
            "\nBACKGROUND EVIDENCE (not the answer): the following subgoals repeatedly appeared in "
            "EARLIER proof attempts and were never discharged directly. They are signals about which "
            "kinds of gaps the library has — NOT a list of lemmas to output.\n"
            f"{bullets}\n"
            "Do NOT output these subgoals (or trivial rephrasings of them) as your answer.\n"
        )

    if seeds:
        blocks = "\n".join(f"```lean4\n{item.strip()}\n```" for item in seeds if str(item).strip())
        parts.append(
            "\nLIBRARY EXCERPTS: lemmas that were already proved and turned out useful before.\n"
            f"{blocks}\n"
        )

    parts.append(
        f"\nPropose {num_samples} DIFFERENT auxiliary lemma(s) that would help prove the target. "
        "Each lemma must satisfy ALL of the following:\n"
        "1. It is a Lean 4 PROPOSITION (a type), not a theorem declaration: no `theorem`/`lemma` keyword, "
        "no name, no proof, no `sorry`.\n"
        "2. It only mentions variables and hypotheses that already appear in the local context above; "
        "introduce any extra variables with `∀` and their assumptions with `→`.\n"
        "3. It is PROVABLE from the hypotheses already in the local context (do not invent assumptions).\n"
        "4. It is strictly simpler than the main goal, and useful for proving it.\n"
        "5. It must NOT be identical or equivalent (up to renaming of variables) to the main goal.\n"
        "6. It MUST mention at least one symbol (constant, function, type) that occurs in the TARGET "
        "above. A lemma about unrelated subjects is useless even if it is true.\n"
    )
    if demand:
        parts.append(
            "7. Use the BACKGROUND EVIDENCE only as a hint about the *style* of gap; each lemma you "
            "output must still be about the TARGET. Do not copy the evidence bullets.\n"
        )
    parts.append(
        "\nOutput each proposition in its own ```lean4 code block, one proposition per block, "
        "and put nothing else inside the block."
    )
    return "".join(parts)


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
    """若模型输出了完整定理声明，尝试还原成命题。

    解析规则与 `sgsr/data/lean_parse.py` **共用同一份实现**。此前这里是第二份
    "声明 → 命题"的代码（产出 `(n : Nat) → P`），而 `prover.close_declaration`
    产出 `∀ n : Nat, P`；两份都会随改动漂开，而"模型输出的命题"与"求解器解析出的
    命题"一旦不一致，pass@k 就不再是同一件事。
    """
    text = text.strip()
    for marker in (":= by", ":="):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
            break
    if not DECL_RE.match(text):
        return text
    return close_declaration(text) or text


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


# ── /solve：整篇证明生成 ─────────────────────────────────────────
# 与 /conjecture 的分工：conjecture 出的是**命题**（`have`-ready），solve 出的是**证明脚本**。
# 两者都只做文本生成与轻量规范化，"证明对不对"一律由 Lean 侧（SgsLean/Server.lean）判定。


def solve_prompt(
    statement: str,
    num_samples: int,
    library: list[dict] | None = None,
    *,
    mode: str = "product",
    sample_salt: str | None = None,
) -> str:
    """整篇证明的提示词：输出 tactic 脚本，不输出定理声明。

    约束刻意写得死，因为 Lean 侧的 `Verify.verify` 会把证明脚本包进 `exact by ...` 后跑
    reap 的重放 + kernel 终检：多一个 `theorem` 头、多一个 `sorry`、少一步都会直接判负。

    `library`（阶段 D/E 的记忆注入）：已在 Lean 环境里**物化成有名常量**的引理列表，
    形如 `[{"name": "sgs_lem_1", "stmt": "..."}]`。空列表 = 不出现该区块，
    这样可以让同一批题的有库/无库条件保持可比较。
    """
    parts: list[str] = [
        "You are a Lean 4 theorem prover. Prove the following statement:\n\n"
        "```lean4\n"
        f"{statement.strip()}\n"
        "```\n"
    ]
    if mode not in {"product", "measurement"}:
        raise ValueError(f"unknown solve prompt mode: {mode}")
    if library:
        blocks = "\n".join(
            f"- `{item.get('name', '?')}` : {str(item.get('stmt', '')).strip()}"
            for item in library
            if str(item.get("stmt", "")).strip()
        )
        if mode == "measurement":
            parts.append(
                "\nAVAILABLE LEMMAS: these verified lemmas are present in the environment under "
                "the names shown below. You may use any lemma when it is mathematically useful. "
                "Do not prefer or avoid a lemma merely because it appears in this list:\n"
                f"{blocks}\n"
            )
        else:
            parts.append(
                "\nAVAILABLE LEMMAS: the following lemmas are ALREADY PROVED and available in the "
                "environment under exactly these names (they are NOT in Mathlib, so Mathlib will not "
                "find them for you). **Check them FIRST**: if one of them (possibly after `intro`/`rw`) "
                "closes or shortens the goal, cite it by name — `exact sgs_lem_xxx`, "
                "`rw [sgs_lem_xxx]`, `simpa using sgs_lem_xxx …` — instead of re-deriving the fact "
                "from scratch. Citing an available lemma is cheaper and much less error-prone than "
                "guessing Mathlib names. They are optional only in the sense that you may ignore them "
                "when none applies:\n"
                f"{blocks}\n"
            )
    parts.append(
        f"\nGive {num_samples} DIFFERENT proof(s) of it. Each proof must satisfy ALL of:\n"
        "1. It is a TACTIC SCRIPT — the body of `by ...` only. No `theorem`/`lemma`/`example` "
        "keyword, no statement, no `by` keyword.\n"
        "2. No `sorry`, no `admit`, no `?_` placeholder.\n"
        "3. It must CLOSE the goal: no subgoal may remain after it runs.\n"
        "4. Use `intro` for `∀`/`→` binders; separate tactics with newlines; use `·` bullets "
        "if one tactic creates several goals.\n"
        "5. Only names available in the current environment may be used. If unsure, prefer "
        "elementary tactics (`intro`, `exact`, `rfl`, `simp`, `rw`, `constructor`, `cases`, "
        "`induction`) over guessed lemma names.\n\n"
        "Output each proof in its own ```lean4 code block, one proof per block, "
        "and put nothing else inside the block."
    )
    if sample_salt:
        # 只用于打破代理缓存；不携带轮次结论、引理偏好或答案信息。
        parts.append(f"\nSampling nonce (ignore semantically): {sample_salt}\n")
    return "".join(parts)


def extract_proofs(generation: str) -> list[str]:
    """抽取所有 ```lean4 代码块作为 tactic 脚本。

    只做两件机械的规范化：去掉可能被一起贴进来的 `by` 前缀、去掉首尾空白。
    **不做** sorry/占位符过滤——按 `docs/api-contract.md` 的分工，判定真伪是 Lean 的职责，
    在这里过滤会让 solve_rate 统计失去意义（负例被偷偷扔掉，指标看起来变好）。
    """
    proofs: list[str] = []
    for block in re.findall(r"```(?:lean4|lean)\s*(.*?)```", generation, flags=re.DOTALL):
        text = block.strip()
        # 只剥**独立的** `by` 前缀。不能写成 `startswith("by")`：那会把 `by_cases`、
        # `by_contra` 这类 tactic 名的头两个字母削掉（`by_cases h : P` → `_cases h : P`），
        # 于是一条合法证明被判成"模型证不出"——实测踩过。
        if _BY_PREFIX_RE.match(text):
            text = _BY_PREFIX_RE.sub("", text, count=1).lstrip()
        if not text:
            continue
        proofs.append(text)
    return proofs


def repair_prompt(
    statement: str,
    failed: list[dict],
    num_samples: int = 4,
    library: list[dict] | None = None,
) -> str:
    """repair 提示词（规格文档 3.7 节 / 4.3 节的 `REPAIR` 区块）。

    内核给的是**结构化失败码**加 Lean 错误原文，这比一句"证明失败"信息量大得多：
    `unknown_identifier` 说名字用错了、`type_error` 说类型不匹配、`unclosed_goals`
    说还差步骤、`mvar_or_sorry` 说根本没证。把它们按固定模板回灌，等于把 Lean 的
    诊断变成下一次生成的输入——这是"推理期不做梯度更新"的前提下唯一能"从错误里学"的通道。

    `failed` 的每项形如 `{"proof": str, "reason": str, "detail": str}`，与
    规格附录 B 的 `Attempt` 一致。`reason` 为空时也照样列出错误原文——
    不要因为拿不到码就把这条失败记录丢掉（那会让 repair 看不见真实错误）。
    """
    parts: list[str] = [
        "You are a Lean 4 theorem prover. A previous attempt at the statement below FAILED. "
        "Rewrite the proof, fixing the reported errors.\n\n"
        "TARGET:\n\n"
        "```lean4\n"
        f"{statement.strip()}\n"
        "```\n"
    ]
    if library:
        blocks = "\n".join(
            f"- `{item.get('name', '?')}` : {str(item.get('stmt', '')).strip()}"
            for item in library
            if str(item.get("stmt", "")).strip()
        )
        parts.append(
            "\nAVAILABLE LEMMAS: already proved and available under exactly these names:\n"
            f"{blocks}\n"
        )

    blocks: list[str] = []
    for index, attempt in enumerate(failed):
        reason = str(attempt.get("reason") or "").strip() or "unknown"
        detail = str(attempt.get("detail") or "").strip()
        proof = str(attempt.get("proof") or "").strip()
        chunk = [f"--- FAILED ATTEMPT {index + 1} ---", f"error_code: {reason}"]
        if detail:
            # 错误原文可能很长（Lean 的类型错误常带完整上下文）；截断是为了让
            # 提示词预算可预测，而不是为了好看。
            chunk.append("error_message:\n" + detail[:1200])
        chunk.append("proof_script:\n```lean4\n" + (proof[:2000] or "(empty)") + "\n```")
        blocks.append("\n".join(chunk))
    parts.append("\nREPAIR: the previous attempts and what the kernel said about them:\n\n"
                 + "\n\n".join(blocks) + "\n")

    # `mvar_or_sorry` 意味着上一轮模型在用 sorry/admit/占位符糊弄——
    # 这不是"证明写错了"而是"没有给出证明"，必须单独用硬指令堵住。
    reasons = {str(a.get("reason") or "").strip() for a in failed}
    if reasons and reasons <= {"mvar_or_sorry", ""}:
        parts.append(
            "\nSTRICT: every attempt above used `sorry`, `admit`, or a `?_` placeholder. "
            "These are FORBIDDEN and are detected by the kernel. Produce a genuine proof.\n"
        )

    parts.append(
        f"\nGive {num_samples} DIFFERENT corrected proof(s). Each proof must satisfy ALL of:\n"
        "1. It is a TACTIC SCRIPT — the body of `by ...` only. No `theorem`/`lemma`/`example` "
        "keyword, no statement, no `by` keyword.\n"
        "2. No `sorry`, no `admit`, no `?_` placeholder.\n"
        "3. It must CLOSE the goal: no subgoal may remain after it runs.\n"
        "4. Address the specific error above: do not repeat the same proof text.\n"
        "5. Only names available in the current environment may be used. If unsure, prefer "
        "elementary tactics (`intro`, `exact`, `rfl`, `simp`, `rw`, `constructor`, `cases`, "
        "`induction`) over guessed lemma names.\n\n"
        "Output each proof in its own ```lean4 code block, one proof per block, "
        "and put nothing else inside the block."
    )
    return "".join(parts)
