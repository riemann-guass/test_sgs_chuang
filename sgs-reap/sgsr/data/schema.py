"""轨迹与需求的数据模式（P2）。

轨迹 = 一条候选证明的逐步记录（`SgsLean/Trace.lean` 的 `trace` 命令输出）：

    {"id": "g36#0", "target": "g36", "statement": "...", "verified": true, "reason": "ok",
     "steps": [{"tactic": "intro n", "goalsLeft": 1, "signature": "2 ∣ n ^ 2 + n",
                "ok": true, "reason": "ok"}, ...]}

需求 = 对所有轨迹里的子目标签名做聚合后的统计条目（见 `sgsr/pipeline/demand.py`）。

这里只放**校验与归一化**，不放业务逻辑：任何脏数据都应该在进入挖掘前被挡住或标记，
而不是让挖掘悄悄算出一个好看的数。

## `id` 与 `target` 必须分开（P0 修复）

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
    # `target` 是 P0 修复加上的必填字段：缺了它，需求统计会退回"把候选当目标"的老错。
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
