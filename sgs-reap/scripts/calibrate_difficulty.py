"""难度标定（P2，规格 9.2）：把课程集分档，挑出"有余量"的工作负载。

为什么必须先做这件事：建库的增益只在**近失手**区间有信号——
* 兜底/模型稳定解出的题（`easy`）：给不给库都一样，测出来一定是 0；
* 稳定解不出的题（`hard`）：余量为 0，phase21 实测两臂都是 0（"因为难才被选进来的"）；
* 只有"解出率严格介于 0 与 1"的题才可能被一条引理翻过来。

流程（两次 Lean 批处理，不是每题一次导入）：

    ① 廉价兜底：一批 `cheap_verify`      → 零模型成本地标注 cheap_hit
    ② 模型采样：每题一次 `/solve`（k 篇）→ 记 token
    ③ 内核验证：一批 `verify`            → 每题通过的篇数 → solve_rate = 通过数 / k
    ④ 分档：easy（cheap 命中 或 solve_rate ≥ 1）/ `unknown_cheap`（清扫被墙钟上限截断，
      兜底能否解出未知）/ nearmiss（0 < rate < 1）/ hard（rate = 0）

`solve_rate` 的分母固定是 `k`（没生成出证明也算失败），与 `run_gate_g3_real.py` 同一口径。

用法：

    python scripts\\calibrate_difficulty.py --set C --k 2 --endpoint http://127.0.0.1:8770/solve \\
           --out experiments/results/calib_C_k2.json --tier-dir data

输出：报告 + `data/C_{nearmiss,hard,easy}.jsonl`（分档清单，供 `run_round --curriculum` 用）。
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgsr.pipeline.prover import git_commit, library_hash, usage_total  # noqa: E402
from sgsr.utils.http_client import BackendUnavailable, post_json  # noqa: E402
from sgsr.verification.client import LeanServer, budget_for_jobs, preflight_imports  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
REGISTERED = {"C": DATA / "C.jsonl", "D": DATA / "minif2f_valid.jsonl", "T": DATA / "minif2f_test.jsonl"}
DEFAULT_ENDPOINT = "http://127.0.0.1:8770/solve"


def load_rows(args) -> tuple[list[dict], str]:
    path = Path(args.path) if args.path else REGISTERED.get(args.dataset)
    if path is None or not Path(path).exists():
        raise SystemExit(f"[calib] 找不到语料：{path}")
    label = Path(path).stem
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if args.corpus:
        # 按来源筛（`source_corpus`）：C1 那种"已被兜底整体吃掉"的子集不必再花模型钱，
        # 标定的意义在于挑出**有余量**的那一档。
        wanted_corpora = {c.strip() for c in args.corpus.split(",") if c.strip()}
        rows = [r for r in rows if str(r.get("source_corpus") or "") in wanted_corpora]
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        rows = [r for r in rows if r.get("id") in wanted]
    if args.limit:
        rows = rows[: args.limit]
    return rows, label


def cheap_pass(rows: list[dict], imports: str) -> dict[str, dict]:
    """一批 `cheap_verify`：零模型成本地判定"第 3 步兜底能不能秒掉这道题"。"""
    jobs = [{"id": r["id"], "cmd": "cheap_verify", "stmt": r["statement"]} for r in rows]
    with LeanServer(imports=imports, heartbeats=budget_for_jobs(len(jobs)),
                    stderr_path=RESULTS / "calib_stderr.log") as server:
        responses = server.batch(jobs)
    out: dict[str, dict] = {}
    for r in rows:
        result = (responses.get(r["id"]) or {}).get("result") or {}
        cheap = result.get("cheap") or {}
        verify = result.get("verify") or {}
        out[r["id"]] = {
            "hit": cheap.get("hit") is True and verify.get("ok") is True,
            "tactic": str(cheap.get("tactic") or "").replace("\n", "; "),
            "probes": len(cheap.get("tried") or []),
            # `exhausted` = 清扫被整条墙钟上限截断（"没试完"）。分档时不能把这种
            # 情形当成"兜底解不了"，否则会把本来便宜的题误判成有模型余量。
            "exhausted": cheap.get("exhausted") is True,
            "elapsed_ms": cheap.get("elapsedMs"),
        }
    return out


def solve_pass(rows: list[dict], k: int, endpoint: str, timeout: float) -> dict[str, dict]:
    """每题一次 `/solve`，取 k 篇候选（后端故障单独记，不当成"证不出"）。"""
    out: dict[str, dict] = {}
    for index, row in enumerate(rows, start=1):
        try:
            data = post_json(endpoint, {"statement": row["statement"], "num_samples": k},
                             timeout=timeout)
        except BackendUnavailable as exc:
            out[row["id"]] = {"proofs": [], "error": str(exc), "usage": {}}
            print(f"  [{index}/{len(rows)}] {row['id']}: **后端故障** {exc}")
            continue
        meta = data.get("meta") or {}
        proofs = [str(p.get("proof", "")).strip() for p in (data.get("proofs") or [])]
        proofs = [p for p in proofs if p]
        out[row["id"]] = {"proofs": proofs, "error": "", "usage": meta.get("usage") or {}}
        print(f"  [{index}/{len(rows)}] {row['id']}: 候选 {len(proofs)}/{k}")
    return out


def verify_pass(rows: list[dict], solved: dict[str, dict], imports: str) -> dict[str, dict]:
    """一批 `verify`：每条候选一篇，按 id 索引。"""
    jobs = [
        {"id": f"v:{row['id']}:{i}", "cmd": "verify", "stmt": row["statement"], "proof": proof}
        for row in rows for i, proof in enumerate(solved[row["id"]]["proofs"])
    ]
    responses: dict[str, dict] = {}
    if jobs:
        with LeanServer(imports=imports, heartbeats=budget_for_jobs(len(jobs)),
                        stderr_path=RESULTS / "calib_stderr.log") as server:
            responses = server.batch(jobs)
    out: dict[str, dict] = {}
    for row in rows:
        verdicts = []
        for i, _ in enumerate(solved[row["id"]]["proofs"]):
            entry = responses.get(f"v:{row['id']}:{i}")
            if entry is None:
                verdicts.append({"index": i, "ok": False, "reason": "missing_response"})
            elif entry.get("error"):
                verdicts.append({"index": i, "ok": False,
                                 "reason": f"protocol_error:{entry['error'].get('code')}"})
            else:
                result = entry.get("result") or {}
                verdicts.append({"index": i, "ok": result.get("ok") is True,
                                 "reason": result.get("reason")})
        out[row["id"]] = {"verdicts": verdicts,
                          "ok_count": sum(1 for v in verdicts if v["ok"])}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="难度标定：把课程集分成 easy / nearmiss / hard")
    parser.add_argument("--set", dest="dataset", default="C", choices=sorted(REGISTERED))
    parser.add_argument("--path", default=None, help="直接给语料文件（覆盖 --set）")
    parser.add_argument("--ids", default=None, help="只标定这些 id（逗号分隔）")
    parser.add_argument("--corpus", default=None,
                        help="只标定这些来源（`source_corpus`，如 C2；逗号分隔）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--k", type=int, default=2, help="每题采样几篇（分母固定是 k）")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--imports", default="Mathlib")
    parser.add_argument("--no-cheap", action="store_true", help="跳过第 3 步兜底标注")
    parser.add_argument("--tier-dir", default=str(DATA), help="分档清单输出目录（空串=不写）")
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    rows, label = load_rows(args)
    if not rows:
        raise SystemExit("[calib] 语料为空")
    print(f"[calib] 语料 {label}：{len(rows)} 条；k={args.k}；端点 {args.endpoint}")

    ok, detail = preflight_imports(args.imports, stderr_path=RESULTS / "calib_stderr.log")
    if not ok:
        raise SystemExit(f"[calib] 环境预检失败（imports={args.imports}）：{detail}")
    print(f"[calib] 环境预检通过（imports={args.imports}）")

    started = time.perf_counter()
    cheap = {} if args.no_cheap else cheap_pass(rows, args.imports)
    print(f"[calib] 兜底标注完成（{len(cheap)} 条，{time.perf_counter() - started:.0f}s）")
    solved = solve_pass(rows, args.k, args.endpoint, args.timeout)
    print(f"[calib] 模型采样完成（{time.perf_counter() - started:.0f}s）")
    verified = verify_pass(rows, solved, args.imports)
    print(f"[calib] 内核验证完成（{time.perf_counter() - started:.0f}s）")

    per_target: dict[str, dict] = {}
    tiers: dict[str, list[str]] = collections.defaultdict(list)
    for row in rows:
        tid = row["id"]
        cheap_hit = bool(cheap.get(tid, {}).get("hit"))
        cheap_exhausted = bool(cheap.get(tid, {}).get("exhausted"))
        rate = (verified[tid]["ok_count"] / args.k) if args.k else 0.0
        if cheap_hit:
            tier = "easy"
        elif rate >= 1.0:
            tier = "easy"
        elif cheap_exhausted:
            # 清扫没试完：兜底能否解出**未知**。分到 nearmiss 会让"给库的增益"混进
            # "兜底本来就能解"，所以单列一档，默认不拿去建库。
            tier = "unknown_cheap"
        elif rate > 0.0:
            tier = "nearmiss"
        else:
            tier = "hard"
        tiers[tier].append(tid)
        per_target[tid] = {
            "domain": row.get("domain"),
            "source_corpus": row.get("source_corpus"),
            "cheap_hit": cheap_hit,
            "cheap_tactic": cheap.get(tid, {}).get("tactic", ""),
            "probes": cheap.get(tid, {}).get("probes"),
            "cheap_exhausted": cheap_exhausted,
            "cheap_elapsed_ms": cheap.get(tid, {}).get("elapsed_ms"),
            "proofs": len(solved[tid]["proofs"]),
            "ok_count": verified[tid]["ok_count"],
            "solve_rate": round(rate, 4),
            "tier": tier,
            "backend_error": solved[tid]["error"],
            "verdicts": verified[tid]["verdicts"],
        }

    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    for tid in per_target:
        for key in tokens:
            tokens[key] += int((solved[tid]["usage"] or {}).get(key, 0) or 0)
    total_tokens = usage_total(tokens)
    backend_errors = sum(1 for t in per_target.values() if t["backend_error"])

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": git_commit(),
        "corpus": {"label": label, "path": str(args.path or REGISTERED.get(args.dataset)),
                   "rows": len(rows), "library_hash": library_hash(None)},
        "config": {"k": args.k, "endpoint": args.endpoint, "imports": args.imports,
                   "cheap": not args.no_cheap, "limit": args.limit},
        "tiers": {name: len(ids) for name, ids in sorted(tiers.items())},
        "backend_errors": backend_errors,
        "tokens": {**tokens, "total": total_tokens,
                   "per_call": round(total_tokens / max(1, len(rows)), 1)},
        "timing": {"total_s": round(time.perf_counter() - started, 1)},
        "criterion": ("easy = 兜底命中或 solve_rate=1；unknown_cheap = 兜底清扫被墙钟上限截断；"
                      "nearmiss = 0<rate<1；hard = rate=0。分母固定为 k（没生成出证明也算失败）。"),
        "per_target": per_target,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tier_dir = Path(args.tier_dir) if args.tier_dir else None
    if tier_dir is not None:
        tier_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(args.path or REGISTERED[args.dataset]).stem
        for tier, ids in tiers.items():
            keep = set(ids)
            subset = [dict(r, tier=tier) for r in rows if r["id"] in keep]
            (tier_dir / f"{stem}_{tier}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in subset),
                encoding="utf-8")

    print(f"[calib] 分档：{report['tiers']}（nearmiss 是唯一有增益信号的档）")
    print(f"[calib] token {total_tokens}（{report['tokens']['per_call']}/题）；"
          f"后端故障 {backend_errors}；耗时 {report['timing']['total_s']}s")
    print(f"[calib] 报告 {out_path}；分档清单在 {tier_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
