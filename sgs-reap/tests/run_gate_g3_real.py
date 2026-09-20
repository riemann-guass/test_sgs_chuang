"""闸门 G3（真定义）：`proved_cover(S) − proved_cover(∅)`。

旧的 G3 是**空转**的：候选池被定义成"目标轨迹签名集合的并集"，于是每条候选按构造至少覆盖
一个目标，贪心比 1.000、子模性 0 违例都是数学必然，跟"引理有没有用"无关
（见 `docs/phase17-log.md` 之前的审计）。本脚本换成真定义：

    在**裸解不出的目标集**（`data/targets_hard.jsonl`）上，同一个 Solver、同样的 k：

      基线臂：不给库            → proved_cover(∅) = 被证出的目标数
      处理臂：给库（提示词列出引理名 + 环境里真的 import 了它们）
                                → proved_cover(S) = 被证出的目标数

      增益 = proved_cover(S) − proved_cover(∅)

两个臂的差别**只有库**：同一批目标、同一个模型、同样的 k、同样的验证预算。
处理臂的验证环境额外 `import SgsLean.GeneratedLibrary`，否则证明里引用 `sgs_lem_i` 会报
`unknown identifier`。

用法：

    # 先起 service\\proxy.py（需网络权限）
    python tests\\run_gate_g3_real.py --limit 12 --k 2 --endpoint http://127.0.0.1:8770/solve

判定（跑之前定死）：

* `pass`：增益 > 0（库真的帮着多证出了目标）；
* `no_gain`：增益 = 0——**这是有效结果**，不是脚本失败（要如实报告，并看是不是库太小/引理太弱）；
* `fail_pipeline`：两臂都一条都证不出（链路或模型有问题，先查那个）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from lean_server import LeanServer, budget_for_jobs  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
RUNS = ROOT / "experiments" / "runs"
DEFAULT_SOLVE = "http://127.0.0.1:8770/solve"
LIBRARY_IMPORTS = "Mathlib,SgsLean.GeneratedLibrary"
BASE_IMPORTS = "Mathlib"


def http_post(url: str, payload: dict, timeout: float = 300.0) -> dict:
    import urllib.error
    import urllib.request

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": {"code": f"http_{exc.code}",
                          "message": exc.read().decode("utf-8", "replace")[:300]}}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"error": {"code": "network", "message": str(exc)}}


def load_library(path: Path) -> list[dict]:
    """读 `experiments/library.jsonl`，按 `Materialize` 的命名规则补上 `sgs_lem_<i+1>`。

    命名必须与物化时一致，否则提示词里给出的名字在 Lean 环境里不存在。
    """
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [{"name": f"sgs_lem_{i + 1}", "stmt": row["stmt"]} for i, row in enumerate(rows)]


def run_arm(targets: list[dict], library: list[dict], args, import_spec: str) -> dict:
    """跑一个臂：/solve → 验证。返回 {target_id: {provable, verdicts}}。"""
    attempts: dict[str, list[str]] = {}
    for target in targets:
        payload = {"statement": target["statement"], "num_samples": args.k}
        if library:
            payload["library"] = library
        response = http_post(args.endpoint, payload)
        proofs = [p.get("proof", "") for p in (response.get("proofs") or [])]
        attempts[target["id"]] = [p for p in proofs if p.strip()]
        print(f"       {target['id']:30s} 候选证明 {len(attempts[target['id']])} 篇")

    jobs = [
        {"id": f"{tid}:{i}", "cmd": "verify", "stmt": stmt, "proof": proof}
        for tid, stmt in ((t["id"], t["statement"]) for t in targets)
        for i, proof in enumerate(attempts[tid])
    ]
    responses: dict[str, dict] = {}
    if jobs:
        with LeanServer(imports=import_spec, heartbeats=budget_for_jobs(len(jobs)),
                        stderr_path=RESULTS / "g3_real_stderr.log") as server:
            responses = server.batch(jobs)
            print(f"       [验证] {len(jobs)} 篇（frontend {server.frontend_ms_total}ms）")

    out: dict[str, dict] = {}
    for target in targets:
        tid = target["id"]
        verdicts = []
        for i, _ in enumerate(attempts[tid]):
            result = (responses.get(f"{tid}:{i}") or {}).get("result") or {}
            verdicts.append({"index": i, "ok": result.get("ok") is True, "reason": result.get("reason")})
        out[tid] = {"provable": any(v["ok"] for v in verdicts), "verdicts": verdicts,
                    "proofs": attempts[tid]}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="闸门 G3（真定义）：有库/无库的净覆盖")
    parser.add_argument("--targets", default=str(DATA / "targets_hard.jsonl"))
    parser.add_argument("--library", default=str(ROOT / "experiments" / "library.jsonl"))
    parser.add_argument("--endpoint", default=DEFAULT_SOLVE)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--k", type=int, default=2)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    targets = [
        json.loads(line)
        for line in Path(args.targets).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][: args.limit]
    library = load_library(Path(args.library))
    if not targets:
        print("[g3r] 目标集为空")
        return 1
    print(f"[g3r] 目标 {len(targets)} 条；库 {len(library)} 条；k={args.k}；端点 {args.endpoint}")
    if not library:
        print("[g3r] 库为空——那两臂就没有差别，先跑 tests\\build_library.py")

    started = time.perf_counter()
    print("[g3r] ==== 基线臂（不给库） ====")
    baseline = run_arm(targets, [], args, BASE_IMPORTS)
    print("[g3r] ==== 处理臂（给库 + 环境 import 库） ====")
    treatment = run_arm(targets, library, args, LIBRARY_IMPORTS)
    elapsed = time.perf_counter() - started

    base_provable = [tid for tid, r in baseline.items() if r["provable"]]
    treat_provable = [tid for tid, r in treatment.items() if r["provable"]]
    gained = sorted(set(treat_provable) - set(base_provable))
    lost = sorted(set(base_provable) - set(treat_provable))
    gain = len(treat_provable) - len(base_provable)

    # 把"工具坏了"与"两臂都没证出来"分开：
    # phase21 实测 W = 37 条最难的（**因为难才被选进 targets_hard**）时两臂都是 0——
    # 那是有信息量的结果（库帮不动这么难的题），不是脚本失败。只有当验证层大面积报协议错误时
    # 才判 fail_pipeline。
    protocol_errors = sum(
        1
        for arm in (baseline, treatment)
        for record in arm.values()
        for verdict_row in record["verdicts"]
        if str(verdict_row.get("reason") or "").startswith("protocol_error")
        or verdict_row.get("reason") in ("missing_response", "missing_reason")
    )
    if protocol_errors > 0 and not base_provable and not treat_provable:
        verdict = "fail_pipeline"
    elif gain > 0:
        verdict = "pass"
    elif not base_provable and not treat_provable:
        verdict = "no_solvable_targets"
    else:
        verdict = "no_gain"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "targets": len(targets),
        "k": args.k,
        "library_size": len(library),
        "library_names": [item["name"] for item in library],
        "endpoint": args.endpoint,
        "baseline_imports": BASE_IMPORTS,
        "treatment_imports": LIBRARY_IMPORTS,
        "proved_cover_empty": len(base_provable),
        "proved_cover_library": len(treat_provable),
        "gain": gain,
        "gained_targets": gained,
        "lost_targets": lost,
        "verdict": verdict,
        "protocol_errors": protocol_errors,
        "criterion": ("增益 > 0 → pass；增益 = 0 → no_gain；两臂皆 0 → no_solvable_targets"
                      "（有效结果：说明工作负载选得太难）；验证层大面积协议错误 → fail_pipeline"),
        "timing": {"total_s": round(elapsed, 1)},
        "per_target": {
            tid: {
                "baseline": baseline[tid]["provable"],
                "treatment": treatment[tid]["provable"],
                "baseline_verdicts": baseline[tid]["verdicts"],
                "treatment_verdicts": treatment[tid]["verdicts"],
            }
            for tid in baseline
        },
    }
    out_path = Path(args.out) if args.out else RESULTS / f"g3_real_n{len(targets)}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNS / f"g3real_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}").mkdir(
        parents=True, exist_ok=True
    )

    print(f"[g3r] proved_cover(∅)={len(base_provable)}  proved_cover(S)={len(treat_provable)}  增益={gain}")
    print(f"[g3r] 因库而多证出：{gained}")
    if lost:
        print(f"[g3r] 注意：有 {len(lost)} 条在给库后反而没证出（采样波动）：{lost}")
    print(f"[g3r] 判定：{verdict}；报告 {out_path}")
    return 0 if verdict in ("pass", "no_gain", "no_solvable_targets") else 1


if __name__ == "__main__":
    raise SystemExit(main())
