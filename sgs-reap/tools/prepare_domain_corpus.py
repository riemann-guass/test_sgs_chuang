"""统一语料准备（P2，规格 9.2 / `docs/data-protocol.md`）。

三块数据的角色是硬约束，所以**准备语料本身就是一次协议检查**：

| 角色 | 文件 | 用途 | 本工具做什么 |
|---|---|---|---|
| **C** 课程集 | `data/C.jsonl`（本工具产出） | 建库：需求挖掘 → 猜想 → 硬门 → 求解 → 入库 | 由 C1（自建初等引理集）+ C2（Mathlib 定理）合成 |
| **D** 开发集 | `data/minif2f_valid.jsonl` | 调参、看方向 | 只**校验**（计数、哈希、与 C 的命题级重叠） |
| **T** 测试集 | `data/minif2f_test.jsonl` | 框架冻结后只跑一次 | 同上；本工具**绝不改写 T** |

为什么不把 D/T 也"重新输出一份"：D/T 是冻结的评测语料，多写一份就多一个可能与源文件漂开的
副本；本工具改为**原地校验 + 把哈希与计数写进 manifest**，需要复现时按 manifest 比对。

## C 的两个来源（`source_corpus` 字段写进每一条）

* `C1`：`data/lemmas_g1.jsonl`（我们自建的初等引理集，与 miniF2F 无关）；
* `C2`：从 Mathlib 里按**模块前缀**挑定理，取它们的类型当命题（`source_target` = 定理全名）。
  这不是"抄答案"：模型只拿到命题文本，证明仍要自己写；`source_target` 是为了事后审计
  这条引理是为哪个目标生成的。C2 的挑选规则写死在命令行参数里，**可复现**。

## 四道过滤（每一道都记账，不静默丢）

1. `list_theorems` 里的名字排序后切片（可复现）+ 编译器自动生成的名字已在 Lean 侧排除；
2. 文本层：折叠空白、长度 ≤ `--max-type-len`、不含 `?`（pp 占位符不可回代）；
3. **命题级去重**：与 C1 / D / T 的归一化文本相同的一律丢掉（同源检查的文本层）；
4. **门检**：逐条过 `Gate.check`，只留"能 elaborate 成命题"的。

用法：

    # 只做检查与合成（默认不调模型）
    python tools\\prepare_domain_corpus.py --c2-prefixes "Mathlib.Data.Nat,Mathlib.Data.Int" \\
           --c2-limit 300 --sample 60

输出：`data/C.jsonl` + `data/corpus_manifest.json`（含三个角色的路径、计数、sha256、
C/D/T 命题级重叠数、四道过滤的淘汰数）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgsr.data import normalize_sig  # noqa: E402
from sgsr.lean import LeanServer, budget_for_jobs  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"

#: C1：自建初等引理集（与 miniF2F 无关）。
C1_PATH = DATA / "lemmas_g1.jsonl"
#: D / T：冻结语料，只校验不改写。
DT_PATHS = {"D": DATA / "minif2f_valid.jsonl", "T": DATA / "minif2f_test.jsonl"}

#: 领域标签：按模块前缀给 C2 打标（只用于报告分桶，不影响判定）。
DOMAIN_RULES = [
    ("Mathlib.Data.Nat", "nat"), ("Mathlib.Data.Int", "int"),
    ("Mathlib.Data.List", "list"), ("Mathlib.Data.Finset", "finset"),
    ("Mathlib.Data.Set", "set"), ("Mathlib.Data.Real", "real"),
    ("Mathlib.Algebra.Order", "order"), ("Mathlib.Order", "order"),
    ("Mathlib.Data.ZMod", "zmod"), ("Mathlib.Data.Multiset", "multiset"),
]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_of(path: Path) -> str:
    if not path.exists():
        return "missing"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def domain_of_module(module: str) -> str:
    for prefix, label in DOMAIN_RULES:
        if module.startswith(prefix):
            return label
    return "mathlib"


def fetch_mathlib_theorems(prefixes: list[str], per_prefix: int, max_type_len: int) -> dict:
    """**一次** `list_theorems` 把每个前缀的定理都取回来。

    服务端一次遍历环境、按前缀分桶；这里只发一条作业。按前缀发 N 条作业会把
    "遍历几十万个常量"这件事重复付 N 遍（实测 7 个前缀花了十几分钟，还会在
    `Mathlib.Order` 这种超大命名空间上卡住）。
    """
    raw: list[dict] = []
    stats: dict[str, int] = {}
    with LeanServer(imports="Mathlib", heartbeats=budget_for_jobs(1),
                    stderr_path=RESULTS / "corpus_stderr.log") as server:
        responses = server.batch([{"id": "list", "cmd": "list_theorems",
                                   "prefixes": prefixes, "perPrefixLimit": per_prefix,
                                   "maxTypeLen": max_type_len}])
    result = (responses.get("list") or {}).get("result") or {}
    for group in result.get("groups") or []:
        stats[group["prefix"]] = int(group.get("total") or 0)
        for item in group.get("theorems") or []:
            raw.append({"name": item["name"], "type": item["type"], "module": item["module"]})
    return {"theorems": raw, "module_totals": stats}


def gate_check(statements: list[dict]) -> tuple[list[dict], list[dict]]:
    """逐条过 `Gate.check`；返回 `(通过, 拒绝明细)`。"""
    if not statements:
        return [], []
    jobs = [{"id": f"g{i}", "cmd": "check", "stmt": item["statement"]}
            for i, item in enumerate(statements)]
    with LeanServer(imports="Mathlib", heartbeats=budget_for_jobs(len(jobs)),
                    stderr_path=RESULTS / "corpus_stderr.log") as server:
        responses = server.batch(jobs)
    passed: list[dict] = []
    rejected: list[dict] = []
    for i, item in enumerate(statements):
        result = (responses.get(f"g{i}") or {}).get("result") or {}
        if result.get("ok") is True:
            passed.append(item)
        else:
            rejected.append({"id": item["id"], "reason": result.get("reason") or "missing",
                             "detail": str(result.get("detail"))[:160]})
    return passed, rejected


def main() -> int:
    parser = argparse.ArgumentParser(description="统一语料准备（C1 + C2 → data/C.jsonl）+ 同源检查")
    parser.add_argument("--c1", default=str(C1_PATH))
    parser.add_argument("--c2-prefixes", default=(
        "Mathlib.Data.Nat,Mathlib.Data.Int,Mathlib.Data.List,Mathlib.Data.Finset,"
        "Mathlib.Data.Multiset,Mathlib.Data.Real,Mathlib.Data.Rat"),
        help="从这些模块前缀里取定理（逗号分隔；空串 = 不取 C2）")
    parser.add_argument("--c2-limit", type=int, default=200, help="每个前缀最多取多少条定理")
    parser.add_argument("--sample", type=int, default=120,
                        help="C2 过滤后确定性抽多少条（0 = 全部）")
    parser.add_argument("--max-type-len", type=int, default=300)
    parser.add_argument("--out", default=str(DATA / "C.jsonl"))
    parser.add_argument("--manifest", default=str(DATA / "corpus_manifest.json"))
    parser.add_argument("--no-check", action="store_true", help="跳过门检（只做文本层合成，调试用）")
    args = parser.parse_args()

    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "filters": {}}

    # ── C1 ──
    c1_rows = read_jsonl(Path(args.c1))
    c1 = [
        {"id": f"C1:{row['id']}", "statement": normalize_sig(row["statement"]),
         "domain": row.get("domain") or "C1", "source_corpus": "C1",
         "source_target": row["id"], "note": row.get("note", "")}
        for row in c1_rows
    ]
    report["filters"]["c1_rows"] = len(c1_rows)

    # ── D / T（只读，用于命题级同源检查）──
    dt_statements: dict[str, set[str]] = {}
    for role, path in DT_PATHS.items():
        rows = read_jsonl(path)
        dt_statements[role] = {normalize_sig(r.get("statement", "")) for r in rows}
        report.setdefault("datasets", {})[role] = {
            "path": str(path.relative_to(ROOT)), "rows": len(rows), "sha256": sha256_of(path)}

    # ── C2：从 Mathlib 取定理 ──
    prefixes = [p.strip() for p in args.c2_prefixes.split(",") if p.strip()]
    c2_raw: list[dict] = []
    if prefixes:
        fetched = fetch_mathlib_theorems(prefixes, args.c2_limit, args.max_type_len)
        c2_raw = fetched["theorems"]
        report["filters"]["c2_module_totals"] = fetched["module_totals"]
    report["filters"]["c2_fetched"] = len(c2_raw)

    seen = {normalize_sig(e["statement"]) for e in c1}
    forbidden = dt_statements.get("D", set()) | dt_statements.get("T", set())
    dropped = {"too_long": 0, "placeholder": 0, "duplicate_in_c": 0, "same_as_dt": 0, "empty": 0}
    c2_pool: list[dict] = []
    for item in sorted(c2_raw, key=lambda x: x["name"]):     # 排序保证可复现
        text = normalize_sig(item["type"])
        if not text:
            dropped["empty"] += 1
            continue
        if len(text) > args.max_type_len:
            dropped["too_long"] += 1
            continue
        if "?" in text:
            dropped["placeholder"] += 1
            continue
        if text in seen:
            dropped["duplicate_in_c"] += 1
            continue
        if text in forbidden:
            # 文本层同源检查：即使来源是 Mathlib，只要题面与 D/T 逐字相同也丢掉
            # （`Nat.add_comm` 这种"Mathlib 定理恰好是 miniF2F 题目"的情况）。
            dropped["same_as_dt"] += 1
            continue
        seen.add(text)
        c2_pool.append({
            "id": f"C2:{item['name']}", "statement": text,
            "domain": domain_of_module(item["module"]), "source_corpus": "C2",
            "source_target": item["name"],
            "note": f"Mathlib 定理 {item['name']}（模块 {item['module']}）的类型",
        })
    report["filters"]["c2_dropped"] = dropped
    report["filters"]["c2_pool"] = len(c2_pool)

    if args.sample and len(c2_pool) > args.sample:
        # 确定性等距抽样（不是随机种子）：同一份 Mathlib 必然给出同一份 C2。
        step = len(c2_pool) / args.sample
        c2_pool = [c2_pool[int(i * step)] for i in range(args.sample)]
    report["filters"]["c2_sampled"] = len(c2_pool)

    if args.no_check:
        c2 = c2_pool
        report["filters"]["c2_rejected_by_gate"] = None
    else:
        c2, rejected = gate_check(c2_pool)
        report["filters"]["c2_rejected_by_gate"] = len(rejected)
        report["filters"]["c2_rejected_examples"] = rejected[:10]

    corpus = c1 + c2

    # ── 最终同源检查（写入 manifest，供事后审计）──
    c_texts = [e["statement"] for e in corpus]
    overlap_d = sorted(set(c_texts) & dt_statements.get("D", set()))
    overlap_t = sorted(set(c_texts) & dt_statements.get("T", set()))
    duplicates = len(c_texts) - len(set(c_texts))
    report["checks"] = {
        "c_rows": len(corpus),
        "c_by_corpus": {k: sum(1 for e in corpus if e["source_corpus"] == k)
                        for k in ("C1", "C2")},
        "c_by_domain": {},
        "c_duplicate_statements": duplicates,
        "overlap_with_D": len(overlap_d),
        "overlap_with_T": len(overlap_t),
        "overlap_with_D_examples": overlap_d[:3],
        "overlap_with_T_examples": overlap_t[:3],
    }
    by_domain: dict[str, int] = {}
    for entry in corpus:
        by_domain[entry["domain"]] = by_domain.get(entry["domain"], 0) + 1
    report["checks"]["c_by_domain"] = by_domain

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in corpus), encoding="utf-8")
    report["datasets"]["C"] = {"path": str(out_path.relative_to(ROOT)), "rows": len(corpus),
                               "sha256": sha256_of(out_path)}

    manifest_path = Path(args.manifest)
    manifest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    print(f"[corpus] C = {len(corpus)} 条（C1 {report['checks']['c_by_corpus']['C1']} + "
          f"C2 {report['checks']['c_by_corpus']['C2']}）；领域 {by_domain}")
    print(f"[corpus] 与 D 重叠 {len(overlap_d)} 条、与 T 重叠 {len(overlap_t)} 条"
          f"（必须都是 0）、C 内重复 {duplicates}")
    print(f"[corpus] 过滤：{report['filters'].get('c2_dropped')}；"
          f"门检拒 {report['filters'].get('c2_rejected_by_gate')}")
    print(f"[corpus] 写入 {out_path}；manifest {manifest_path}")
    if overlap_d or overlap_t:
        print("[corpus] **警告**：C 与 D/T 有逐字相同的题面，违反同源协议，请检查抽样规则")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
