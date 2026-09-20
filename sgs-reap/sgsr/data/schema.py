"""轨迹与需求的数据模式（P2）。

轨迹 = 一条候选证明的逐步记录（`SgsLean/Trace.lean` 的 `trace` 命令输出）：

    {"id": "g36", "statement": "...", "verified": true, "reason": "ok",
     "steps": [{"tactic": "intro n", "goalsLeft": 1, "signature": "2 ∣ n ^ 2 + n",
                "ok": true, "reason": "ok"}, ...]}

需求 = 对所有轨迹里的子目标签名做聚合后的统计条目（见 `sgsr/pipeline/demand.py`）。

这里只放**校验与归一化**，不放业务逻辑：任何脏数据都应该在进入挖掘前被挡住或标记，
而不是让挖掘悄悄算出一个好看的数。
"""

from __future__ import annotations

import json
import re
from pathlib import Path


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
    """把 server 的 `trace` 响应拼成轨迹记录。"""
    return {
        "id": job["id"],
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
