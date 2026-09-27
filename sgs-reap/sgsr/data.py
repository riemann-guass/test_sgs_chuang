"""数据层：轨迹/需求的数据模式 + 「Lean 声明 → 闭式命题」的唯一解析实现。

合并自旧 `data/schema.py` + `data/lean_parse.py`：两者都是"把 Lean/轨迹文本
规范化成下游能用的形状"，分成两个模块只会让调用方每次都要想"这个函数在哪"。

下面是第一部分：轨迹与需求的数据模式。

轨迹 = 一条候选证明的逐步记录（`SgsLean/Trace.lean` 的 `trace` 命令输出）：

    {"id": "g36#0", "target": "g36", "statement": "...", "verified": true, "reason": "ok",
     "steps": [{"tactic": "intro n", "goalsLeft": 1, "signature": "2 ∣ n ^ 2 + n",
                "ok": true, "reason": "ok"}, ...]}

需求 = 对所有轨迹里的子目标签名做聚合后的统计条目（见 `sgsr/pipeline/demand.py`）。

这里只放**校验与归一化**，不放业务逻辑：任何脏数据都应该在进入挖掘前被挡住或标记，
而不是让挖掘悄悄算出一个好看的数。

## `id` 与 `target` 必须分开

`id` 是**候选作业**的标识（`<目标>#<序号>`），`target` 才是**数学目标**的标识。
早先的版本只有 `id`，而 `demand.mine` 直接把它当目标用——于是一条目标的 k 篇候选
被算成 k 个"不同目标"，`min_targets` 那条跨目标门槛彻底失效，"需求"退化成
"同一个目标的候选里重复出现的子目标"。这是会让核心判据失真的错，所以 `target`
现在是**必填字段**，`validate_trace` 会把它抓出来。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 候选 id 的分隔符：`<target>#<index>`。用 `#` 而不是 `:`，因为目标 id 里可能带冒号
# （如 `round0:cand:aime_1984_p5#0`）。
CANDIDATE_SEP = "#"


def candidate_id(target: str, index: int) -> str:
    """候选作业 id 的**唯一**生成口径；所有生成候选的地方都走它。"""
    return f"{target}{CANDIDATE_SEP}{index}"


def target_of(candidate: str) -> str:
    """从候选 id 反推目标 id（`a#0` → `a`）。没有分隔符时原样返回——**不要**在这里猜。"""
    head, sep, _ = str(candidate).rpartition(CANDIDATE_SEP)
    return head if sep else str(candidate)


def normalize_sig(text: str) -> str:
    """签名归一化：折叠空白（与 Lean 侧 `normalizeProp` 同口径）。"""
    return re.sub(r"\s+", " ", text or "").strip()


def load_jsonl(path: str | Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def validate_trace(row: dict) -> list[str]:
    """返回问题列表；空列表表示这条轨迹可用。"""
    problems: list[str] = []
    for key in ("id", "statement", "verified", "steps"):
        if key not in row:
            problems.append(f"缺字段 {key}")
    # `target` 是必填字段；缺少它会把候选证明误当成不同数学目标。
    target = str(row.get("target") or "").strip()
    if not target:
        problems.append("缺字段 target（候选作业的 id 不能当目标用）")
    elif target == str(row.get("id") or "").strip():
        problems.append("target 等于 id：候选 id 与目标 id 没有分开")
    steps = row.get("steps")
    if not isinstance(steps, list):
        problems.append("steps 不是数组")
        return problems
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"steps[{idx}] 不是对象")
            continue
        if "tactic" not in step or "signature" not in step:
            problems.append(f"steps[{idx}] 缺 tactic/signature")
        if not isinstance(step.get("goalsLeft", 0), int):
            problems.append(f"steps[{idx}] goalsLeft 不是整数")
    return problems


def trace_from_job(job: dict, result: dict) -> dict:
    """把 server 的 `trace` 响应拼成轨迹记录。

    `job["target"]` 是**数学目标**（必须由调用方显式给出）；缺失时退回按 `id` 反推，
    并在记录里留一个标记，让下游能发现"这次运行的调用方没有传目标"。
    """
    raw_target = str(job.get("target") or "").strip()
    inferred = not raw_target
    target = raw_target or target_of(str(job.get("id") or ""))
    return {
        "id": job["id"],
        "target": target,
        "target_inferred": inferred,
        "domain": job.get("domain"),
        "statement": job["statement"],
        "proof": job.get("proof", ""),
        "verified": result.get("verified") is True,
        "reason": result.get("reason"),
        "steps": [
            {
                "tactic": step.get("tactic", ""),
                "goalsLeft": step.get("goalsLeft", 0),
                "signature": normalize_sig(step.get("signature", "")),
                "ok": step.get("ok") is True,
                "reason": step.get("reason"),
            }
            for step in result.get("steps", [])
        ],
    }


# ─────────────────── 「Lean 声明 → 闭式命题」的唯一实现（旧 data/lean_parse.py） ───────────────────
#
# 此前这件事在仓库里有三份实现，产出形式还不一样：
#
# * `prover.close_declaration`：`theorem f (n : Nat) : P` → `∀ n : Nat, P`；
# * `prompts._strip_declaration`：把模型输出的声明块还原成命题，产出 `(n : Nat) → P`；
# * `tools/minif2f_to_jsonl.convert`：`∀ (n : Nat), P`，并且**丢掉文件头的 `open`**。
#
# 三份实现意味着"基准数据里的命题"与"求解器看到的命题"可以慢慢漂开，而两者一旦
# 不一致，D/T 上的 pass@k 就不再是同一件事。这里把它们收敛成一份。
#
# 解析规则（机械、可复核）：
#
# 1. 剔掉 `import` / `open` / `namespace` / `set_option` / 块注释等声明前导；
# 2. 定位第一条 `theorem` / `lemma` / `example`，截掉证明体（`:= …`）；
# 3. 找**不在括号内**的第一个 `:`，把「名字 + 绑定组」与「命题」分开；
# 4. 绑定组转成 `∀`：`(n : Nat)` → `∀ n : Nat,`，`{a : U}` → `∀ {a : U},`，
#    `[inst : C]` → `∀ [inst : C],`。
#
# 注意 `variable (n : Nat)` 这类**声明式绑定不在覆盖范围内**：这些行会被前导剔除，
# 于是命题里若出现自由变量，得到的就不是闭式命题（门检会拒，错误码是
# `unknown_identifier`/`type_elab_failed`）。需要支持时应在调用方展开 `variable`。

#: 声明关键字（前面允许属性、修饰符）。
DECL_RE = re.compile(
    r"(?:^|\n)\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*"
    r"(?:theorem|lemma|example)\s*",
)

#: 声明之前允许出现的一行式命令（`import` / `open` / `namespace` / `set_option` …）。
#: 它们必须被**剔掉**再找声明，否则 `import Mathlib` 会被拼进命题里。
PRELUDE_RE = re.compile(
    r"(?m)^\s*(?:"
    r"import\s+[^\n]*"
    r"|open\s+[^\n]*"
    r"|namespace\s+[^\n]*"
    r"|end\s+[^\n]*"
    r"|section\s*[^\n]*"
    r"|variable\s+[^\n]*"
    r"|universe\s+[^\n]*"
    r"|set_option\s+[^\n]*"
    r"|local\s+[^\n]*"
    r"|noncomputable\s+section"
    r")\s*$"
)

#: 块注释（含文档注释 `/-- … -/` 与 `/--! … -/`）。
BLOCK_COMMENT_RE = re.compile(r"/-[-!]?.*?-/", re.DOTALL)

#: 行注释：`-- …`（只在**行首或空白后**算注释，避免切掉 `a - -b` 这种写法）。
LINE_COMMENT_RE = re.compile(r"(?m)(?:^|\s)--[^\n]*")


def strip_prelude(text: str) -> str:
    """去掉声明之前的 import/open/namespace/set_option 与注释。"""
    cleaned = BLOCK_COMMENT_RE.sub("\n", text)
    cleaned = PRELUDE_RE.sub("", cleaned)
    return LINE_COMMENT_RE.sub("\n", cleaned)


def find_top_level_colon(text: str) -> int:
    """找**不在括号内**的第一个 `:`。

    不能直接用 `text.find(":")`：`theorem add_zero (n : Nat) : n + 0 = n` 里第一个冒号
    是绑定变量里的那个，按它切会把名字切成一团乱码（实测踩过）。
    """
    depth = 0
    pairs = {"(": ")", "{": "}", "[": "]", "⟨": "⟩"}
    closing = set(pairs.values())
    for index, char in enumerate(text):
        if char in pairs:
            depth += 1
        elif char in closing:
            depth = max(0, depth - 1)
        elif char == ":" and depth == 0:
            return index
    return -1


def split_binders(text: str) -> tuple[list[str], str]:
    """把 `(x : T) (h : P) {a : U} [inst : C]` 前缀切成绑定列表与剩余文本。"""
    binders: list[str] = []
    rest = text.lstrip()
    while rest[:1] in ("(", "{", "["):
        close_ch = {"(": ")", "{": "}", "[": "]"}[rest[0]]
        depth = 0
        end = -1
        for index, char in enumerate(rest):
            if char == rest[0]:
                depth += 1
            elif char == close_ch:
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            break
        binders.append(rest[: end + 1])
        rest = rest[end + 1 :].lstrip()
    return binders, rest


def binder_to_forall(binder: str) -> str:
    """`(x : T)` → `∀ x : T,`；`{a : U}` → `∀ {a : U},`；`[i : C]` → `∀ [i : C],`。"""
    inner = binder[1:-1].strip()
    opener = "" if binder[0] == "(" else binder[0]
    closer = "" if binder[0] == "(" else {"{": "}", "[": "]"}[binder[0]]
    return f"∀ {opener}{inner}{closer},"


def close_declaration(text: str) -> str:
    """把一条 `theorem`/`lemma`/`example` 声明闭包成自足的命题（协议 v1.2 要求闭式）。

    步骤：剔掉 import/open/namespace/注释 → 定位声明 → 切掉证明体 →
    找**顶层**冒号把"名字 + 绑定"与"命题"分开 → 绑定转 `∀`。
    """
    body = strip_prelude(DECL_RE.sub("\n", strip_prelude(text), count=1)).strip()
    # 截掉证明体
    for marker in (":= by", ":=by", ":="):
        index = body.find(marker)
        if index != -1:
            body = body[:index]
            break
    colon = find_top_level_colon(body)
    if colon == -1:
        head, rest = body, ""
    else:
        head, rest = body[:colon], body[colon + 1:]
    # head 有两种形状：
    #   `add_zero (n : Nat)` —— 名字 + 绑定
    #   `(n : Nat)`          —— 只有绑定（`example` 形式）
    # 名字是**不以括号开头**的首个词；剥掉它之后再切绑定。
    head = head.strip()
    if head[:1] not in ("(", "{", "["):
        parts = head.split(None, 1)
        head = parts[1].strip() if len(parts) == 2 else ""
    binders, _ = split_binders(head)
    statement = re.sub(r"\s+", " ", rest.strip())
    if not binders:
        return statement
    return " ".join(binder_to_forall(binder) for binder in binders) + " " + statement


def first_declaration(text: str):
    """返回第一条声明关键字的位置（`re.Match` 或 None）。调用方需要 `:=` 检查时用它。"""
    return DECL_RE.search("\n" + text)
