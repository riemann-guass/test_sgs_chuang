"""引理库（P3）：库的物理形态与读写。

v1 用 JSONL 存库，每条：

    {"stmt": "...", "proof": "...", "verified": true,
     "source": "round0:cand:g01#0", "source_target": "g01", "source_corpus": "C1",
     "uses": ["Nat.add_comm"], "delta_len": 3, "added_round": 0}

设计取舍：

* **只存验证过的引理**（`verified=true`）—— 库是"变强的载体"，混进没过的引理等于污染载体；
* 存 `uses`（依赖抽取结果）与 `delta_len`（压缩收益），供后续审计"这条引理到底有没有被用上"；
* 检索 v1 只做**归一化文本匹配**（`data.schema.normalize_sig` 同口径）；
  真正的符号/相似度召回等有规模了再说（现在库是几十条量级）。

## `source_target` / `source_corpus` 是**强制字段**（P0 修复）

规范第四条硬约束是"库中绝不允许出现 D 或 T 来源的引理"。此前库记录里没有来源字段，
这条约束**无法事后审计**——只能相信写入时没犯错。现在：

* 写入时缺 `source_target` 或非法 `source_corpus` 的条目**直接拒收并计数**；
* `assert_clean_sources` 在读库时复核一遍，任何 D/T 来源都会抛错；
* `add_many` 返回 `(写入数, 拒收明细)`，拒收不静默。

两道防线都要有：写入侧的检查挡新数据，读取侧的检查挡"用别的工具写进来的"历史数据。
"""

from __future__ import annotations

import json
from pathlib import Path

from sgsr.data.schema import normalize_sig

#: 允许进库的语料标识：C 类（课程集）。`C1/C2/C3` 是 `docs/data-protocol.md` 里的三个来源。
ALLOWED_SOURCE_CORPORA = {"C", "C1", "C2", "C3"}
#: 明确禁止的语料标识（开发集 / 测试集）。
FORBIDDEN_SOURCE_CORPORA = {"D", "T", "dev", "test", "minif2f_valid", "minif2f_test"}


class LibrarySourceError(RuntimeError):
    """库里出现 D/T 来源的引理（违反 `docs/data-protocol.md` 硬约束 1）。"""


def _check_source(entry: dict) -> str:
    """返回拒绝原因；空串表示这条可以入库。"""
    corpus = str(entry.get("source_corpus") or "").strip()
    target = str(entry.get("source_target") or "").strip()
    if not target:
        return "缺 source_target"
    if not corpus:
        return "缺 source_corpus"
    if corpus in FORBIDDEN_SOURCE_CORPORA:
        return f"source_corpus={corpus} 属于 D/T，禁止进库"
    if corpus not in ALLOWED_SOURCE_CORPORA:
        return f"source_corpus={corpus} 不在允许集合 {sorted(ALLOWED_SOURCE_CORPORA)}"
    return ""


def add_many(path: str | Path, entries: list[dict]) -> tuple[int, list[dict]]:
    """把通过验证的条目写进库。

    返回 `(写入条数, 拒收明细)`。拒收的四类原因：空语句、未验证、来源字段不合法、库内重复。
    **汇总成元组而不是只回一个 int**：静默拒收会让"库为什么没长大"变得无法诊断。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = load(target)
    seen = {normalize_sig(row["stmt"]) for row in existing}
    written = 0
    rejected: list[dict] = []
    with target.open("a", encoding="utf-8") as handle:
        for entry in entries:
            stmt = normalize_sig(entry.get("stmt", ""))
            if not stmt:
                rejected.append({"stmt": "", "reason": "空语句"})
                continue
            if entry.get("verified") is not True:
                rejected.append({"stmt": stmt, "reason": "verified 不为 true"})
                continue
            reason = _check_source(entry)
            if reason:
                rejected.append({"stmt": stmt, "reason": reason})
                continue
            if stmt in seen:          # 库内自我去重（α-等价由 Lean 侧的 Novelty 负责）
                rejected.append({"stmt": stmt, "reason": "库内重复"})
                continue
            seen.add(stmt)
            handle.write(json.dumps(entry | {"stmt": stmt}, ensure_ascii=False) + "\n")
            written += 1
    return written, rejected


def load(path: str | Path) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    return [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def assert_clean_sources(path: str | Path) -> None:
    """读库时复核来源：任何 D/T 来源的引理都抛 `LibrarySourceError`。"""
    offenders = []
    for row in load(path):
        corpus = str(row.get("source_corpus") or "").strip()
        if corpus in FORBIDDEN_SOURCE_CORPORA:
            offenders.append({
                "stmt": str(row.get("stmt", ""))[:80],
                "source_corpus": corpus,
                "source_target": row.get("source_target"),
            })
    if offenders:
        raise LibrarySourceError(
            f"{Path(path).name} 里有 {len(offenders)} 条 D/T 来源的引理，"
            f"违反 docs/data-protocol.md 硬约束 1：{offenders[:3]}"
        )


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
        "corpora": sorted({str(row.get("source_corpus", "?")) for row in rows}),
        "source_targets": sorted({str(row.get("source_target", "?")) for row in rows}),
    }
