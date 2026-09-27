"""引理库（P3）：库的物理形态与读写。

当前用 JSONL 存库，每条：

    {"stmt": "...", "proof": "...", "verified": true,
     "source": "round0:cand:g01#0", "source_target": "g01", "source_corpus": "C1",
     "uses": ["Nat.add_comm"], "delta_len": 3, "added_round": 0,
     "status": "probation"}

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

import hashlib
import json
from pathlib import Path

from sgsr.data import normalize_sig

#: 允许进库的语料标识：C 类（课程集）。`C1/C2/C3` 是 `docs/data-protocol.md` 里的三个来源。
ALLOWED_SOURCE_CORPORA = {"C", "C1", "C2", "C3"}
#: 明确禁止的语料标识（开发集 / 测试集）。
FORBIDDEN_SOURCE_CORPORA = {"D", "T", "dev", "test", "minif2f_valid", "minif2f_test"}
VALID_STATUSES = {"probation", "active", "cold"}


class LibrarySourceError(RuntimeError):
    """库里出现 D/T 来源的引理（违反 `docs/data-protocol.md` 硬约束 1）。"""


def name_for(stmt: str) -> str:
    """引理的**稳定**名字：`sgs_lem_<归一化语句 sha256 前 8 位>`。

    为什么不用序号：`Materialize` 以前按数组下标命名 `sgs_lem_<i+1>`，而淘汰会从
    库中间删条目，于是删一条就让后面所有名字整体错位——历史 `constants`、
    提示词里给的名字、已验证的证明文本全部张冠李戴。名字必须由**内容**决定。
    """
    digest = hashlib.sha256(normalize_sig(stmt).encode("utf-8")).hexdigest()
    return f"sgs_lem_{digest[:8]}"


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
            named = entry | {"stmt": stmt}
            # 名字在**入库这唯一一处**生成：物化、检索、提示词三边都读同一个字段，
            # 谁都不许再按下标猜名字。
            named["name"] = str(entry.get("name") or "").strip() or name_for(stmt)
            named["status"] = status_of(named)
            handle.write(json.dumps(named, ensure_ascii=False) + "\n")
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


def status_of(row: dict) -> str:
    """旧条目没有状态时按 probation 处理，绝不默认为 active。"""
    status = str(row.get("status") or "probation")
    return status if status in VALID_STATUSES else "probation"


def active_rows(path: str | Path) -> list[dict]:
    """正式在线快照唯一允许读取的条目。"""
    return [row for row in load(path) if status_of(row) == "active"]


def active_hash(path: str | Path) -> str:
    """活动库内容哈希；与 JSONL 行序和无关诊断字段无关。"""
    payload = sorted([
        {"name": row.get("name"), "stmt": row.get("stmt"), "proof": row.get("proof")}
        for row in active_rows(path)
    ], key=lambda row: (str(row.get("name") or ""), str(row.get("stmt") or "")))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def snapshot_manifest_path(path: str | Path) -> Path:
    target = Path(path)
    return target.with_name(target.stem + "_snapshot.json")


def write_snapshot_manifest(path: str | Path, generated_path: str | Path) -> dict:
    """发布活动库后写入可审计 manifest。"""
    generated = Path(generated_path)
    manifest = {
        "schema": 1,
        "library": str(Path(path)),
        "active_count": len(active_rows(path)),
        "active_hash": active_hash(path),
        "generated": str(generated),
        "generated_hash": (
            "sha256:" + hashlib.sha256(generated.read_bytes()).hexdigest()[:16]
            if generated.exists() else "missing"
        ),
    }
    snapshot_manifest_path(path).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def assert_snapshot_ready(path: str | Path, generated_path: str | Path) -> None:
    """活动库、生成源码和 manifest 必须属于同一快照。"""
    rows = active_rows(path)
    if not rows:
        return
    manifest_path = snapshot_manifest_path(path)
    if not manifest_path.exists():
        raise LibrarySourceError("活动库存在但 snapshot manifest 缺失，请先 --materialize-only")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    generated = Path(generated_path)
    generated_hash = (
        "sha256:" + hashlib.sha256(generated.read_bytes()).hexdigest()[:16]
        if generated.exists() else "missing"
    )
    if manifest.get("active_hash") != active_hash(path):
        raise LibrarySourceError("活动库已变化，snapshot manifest 过期")
    if manifest.get("generated_hash") != generated_hash:
        raise LibrarySourceError("GeneratedLibrary.lean 与 snapshot manifest 不一致")


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


def write_all(path: str | Path, rows: list[dict]) -> None:
    """整库重写（保持给定顺序）。

    只有 `runner` 的"复用记账 + 曝光计数 + 淘汰"这一步该调用它：它需要把内存里
    测到的 `reuse` / `reuse_targets` / `exposures` 落盘。别的地方一律用 `add_many`
    追加，避免把库改成非原子的读写混合。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
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
