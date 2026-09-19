"""引理库（P3）：库的物理形态与读写。

v1 用 JSONL 存库，每条：

    {"id": "lib001", "stmt": "...", "proof": "...", "verified": true,
     "source": "greedy:cover", "uses": ["Nat.add_comm"], "delta_len": 3}

设计取舍：

* **只存验证过的引理**（`verified=true`）—— 库是"变强的载体"，混进没过的引理等于污染载体；
* 存 `uses`（依赖抽取结果）与 `delta_len`（压缩收益），供后续审计"这条引理到底有没有被用上"；
* 检索 v1 只做**归一化文本匹配**（`graph.schema.normalize_sig` 同口径）；
  真正的符号/相似度召回等有规模了再说（现在库是几十条量级）。
"""

from __future__ import annotations

import json
from pathlib import Path

from graph.schema import normalize_sig


def add_many(path: str | Path, entries: list[dict]) -> int:
    """把通过验证的条目写进库；返回实际写入条数（未通过的会被拒绝并计数）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = load(target)
    seen = {normalize_sig(row["stmt"]) for row in existing}
    written = 0
    with target.open("a", encoding="utf-8") as handle:
        for entry in entries:
            stmt = normalize_sig(entry.get("stmt", ""))
            if not stmt or entry.get("verified") is not True:
                continue
            if stmt in seen:          # 库内自我去重（α-等价由 Lean 侧的 Novelty 负责）
                continue
            seen.add(stmt)
            handle.write(json.dumps(entry | {"stmt": stmt}, ensure_ascii=False) + "\n")
            written += 1
    return written


def load(path: str | Path) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    return [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def query(path: str | Path, text: str) -> list[dict]:
    """归一化文本匹配（子串级）：用于"这条候选是不是库里的"这一类粗筛。"""
    needle = normalize_sig(text)
    return [row for row in load(path) if needle and needle in row["stmt"]]


def stats(path: str | Path) -> dict:
    rows = load(path)
    return {
        "size": len(rows),
        "verified": sum(1 for row in rows if row.get("verified") is True),
        "with_dependency": sum(1 for row in rows if row.get("uses")),
        "sources": sorted({row.get("source", "?") for row in rows}),
    }
