r"""阶段 A 诊断：把 `exception` 样本用**更大的判定预算**重测。

背景（`docs/phase17-log.md`）：miniF2F 上的 G1 里，122/164 个失败是 `exception`
（`tacticException`，含心跳耗尽 / 解释执行超时那一类），不是"证明错了"。
所以那个 solve_rate = 0.094 **还不能当成"模型不会做 miniF2F"的结论**——
必须先把"预算掐死"与"真判定"分开。

本脚本只重跑 Lean（不调模型、不花钱），把每个样本的判定码从 `exception`
重新判一次，输出**翻转矩阵**。

用法：

    python scripts\diagnose_exceptions.py                       # 默认取最近一次 g1 轨迹，抽 20 条
    python scripts\diagnose_exceptions.py --limit 40 --heartbeats 40000000 --timeout-ms 600000

判读：

* 翻转率高（`exception` → `ok` / `unclosed_goals` / `type_error`）⟹ 原报告的 solve_rate 被低估，
  必须用放大后的预算重跑 G1；
* 翻转率低（大多数仍是 `exception`）⟹ 确实是这些证明太难/太长，`exception` 是真实信号，
  可以据此走"换 Solver / 退域 / 降级"的预案。
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgsr.verification.client import ROOT, RUNS, LeanServer, latest_traces  # noqa: E402

RESULTS = ROOT / "experiments" / "results"


def is_tool_failure(reason: str) -> bool:
    """工具层失败（子进程崩溃 / 响应缺失），与"Lean 判负"区分开。"""
    return reason.startswith("protocol_error") or reason in ("missing_response", "missing_reason")


def collect_samples(traces_path: Path, mode: str) -> list[dict]:
    """从轨迹里取候选。

    * `mode="exceptions"`：只取原始判定为 `exception` 的（诊断用）；
    * `mode="all"`：取全部候选（用于**修正后的 G1**——因为其他判定码也可能被预算污染，
      只重判 exception 会系统性高估修正后的成绩）。
    """
    samples: list[dict] = []
    for line in traces_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        proofs = row.get("proofs") or []
        for verdict in row.get("verdicts") or []:
            reason = verdict.get("reason")
            if mode == "exceptions" and reason != "exception":
                continue
            idx = int(verdict.get("index", 0))
            if idx >= len(proofs):
                continue
            samples.append(
                {
                    "target": row.get("id"),
                    "index": idx,
                    "stmt": row.get("statement", ""),
                    "proof": proofs[idx],
                    "old_ok": verdict.get("ok") is True,
                    "old_reason": reason,
                }
            )
    return samples


def stride_sample(samples: list[dict], limit: int) -> list[dict]:
    """等距抽样：避免只取到文件开头那几个目标（轨迹是按目标顺序写的）。"""
    if limit <= 0 or limit >= len(samples):
        return samples
    stride = len(samples) / limit
    picked = [samples[min(len(samples) - 1, int(i * stride))] for i in range(limit)]
    # 去重（stride < 1 时不可能，但保底）
    seen: set[tuple] = set()
    out: list[dict] = []
    for item in picked:
        key = (item["target"], item["index"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="用更大预算重测 exception 样本")
    parser.add_argument("--traces", default=None, help="轨迹文件；默认取最近一次 g1 运行")
    parser.add_argument("--limit", type=int, default=20, help="抽样条数（0 = 全量）")
    parser.add_argument("--mode", choices=["exceptions", "all"], default="exceptions",
                        help="重判范围：只重判 exception（诊断）或全部候选（修正 G1）")
    parser.add_argument("--hard-out", default=str(ROOT / "data" / "targets_hard.jsonl"),
                        help="mode=all 时把修正后 solve_rate = 0 的目标写到这里")
    parser.add_argument("--heartbeats", type=int, default=40_000_000, help="放大后的心跳预算")
    parser.add_argument("--timeout-ms", type=int, default=600_000, help="放大后的单 tactic 墙钟（毫秒）")
    parser.add_argument("--trivial-heartbeats", type=int, default=200_000)
    parser.add_argument("--imports", default="Mathlib")
    parser.add_argument("--chunk", type=int, default=40)
    parser.add_argument("--out", default=str(RESULTS / "diagnose_exceptions.json"))
    args = parser.parse_args()

    traces_path = Path(args.traces) if args.traces else latest_traces()
    if traces_path is None or not traces_path.exists():
        print("[diag] 找不到轨迹文件；先跑 scripts\run_gate_g1.py")
        return 1
    print(f"[diag] 轨迹：{traces_path}")

    all_samples = collect_samples(traces_path, args.mode)
    if not all_samples:
        print(f"[diag] 该轨迹里没有符合 mode={args.mode} 的样本")
        return 1
    sampled = stride_sample(all_samples, args.limit)
    print(
        f"[diag] mode={args.mode}：候选共 {len(all_samples)} 条，抽样 {len(sampled)} 条；"
        f"放大预算 = 心跳 {args.heartbeats:,} / 墙钟 {args.timeout_ms} ms"
    )

    started = time.perf_counter()
    with LeanServer(
        imports=args.imports,
        heartbeats=args.heartbeats,
        trivial_heartbeats=args.trivial_heartbeats,
        tactic_timeout_ms=args.timeout_ms,
        stderr_path=RESULTS / "diagnose_exceptions_stderr.log",
    ) as server:
        info = server.ping()
        print(f"[diag] ping：{json.dumps(info, ensure_ascii=False)}")
        # 预算自检：ping 回报的必须是我们要求的值，否则后面的结论不可比
        if int(info.get("heartbeats", -1)) != args.heartbeats:
            print(f"[diag] 预算未生效：期望心跳 {args.heartbeats}，ping 回报 {info.get('heartbeats')}")
            return 1
        items = [
            {"id": f"d{i}", "stmt": s["stmt"], "proof": s["proof"]}
            for i, s in enumerate(sampled)
        ]
        responses = server.verify_all(items, chunk=args.chunk)
        frontend_ms = server.frontend_ms_total
    elapsed = time.perf_counter() - started

    flip: collections.Counter = collections.Counter()
    per_sample: list[dict] = []
    for i, sample in enumerate(sampled):
        response = responses.get(f"d{i}") or {}
        result = response.get("result") or {}
        # 协议层失败（子进程崩了 / 响应缺失）必须与"判定失败"分开记：
        # 前者是工具问题，后者才是模型问题。phase18 丢过整整一个 chunk（40 条）。
        protocol_error = response.get("error") or {}
        if not result and protocol_error:
            new_reason = f"protocol_error:{protocol_error.get('code', 'unknown')}"
        elif not result:
            new_reason = "missing_response"
        else:
            new_reason = result.get("reason") or "missing_reason"
        flip[new_reason] += 1
        per_sample.append(
            {
                "target": sample["target"],
                "index": sample["index"],
                "old_reason": sample["old_reason"],
                "new_reason": new_reason,
                "new_ok": result.get("ok") is True,
                "goals_left": result.get("goalsLeft"),
                "detail": (result.get("detail") or protocol_error.get("message") or "")[:400],
                "stmt": sample["stmt"][:300],
            }
        )

    rescued = sum(
        1 for row in per_sample if row["new_reason"] != "exception" and not is_tool_failure(row["new_reason"])
    )
    tool_failures = sum(1 for row in per_sample if is_tool_failure(row["new_reason"]))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "traces": str(traces_path.relative_to(ROOT)),
        "budget": {
            "heartbeats": args.heartbeats,
            "tactic_timeout_ms": args.timeout_ms,
            "imports": args.imports,
            "note": "原始 G1 用过的是库里的编译期默认（心跳 4,000,000 / 墙钟 200,000 ms）",
        },
        "mode": args.mode,
        "samples_total": len(all_samples),
        "sampled": len(sampled),
        "new_reason_histogram": dict(flip),
        "rescued": rescued,
        "rescued_ratio": round(rescued / len(sampled), 4) if sampled else 0.0,
        "tool_failures": tool_failures,
        "newly_ok": sum(1 for row in per_sample if row["new_ok"]),
        "old_ok": sum(1 for row in per_sample if row.get("old_ok")),
        "timing": {"total_s": round(elapsed, 1), "frontend_ms": frontend_ms},
        "per_sample": per_sample,
    }

    # mode=all 且全量：直接给出修正后的 G1 与 hard 目标集（阶段 A 的产出）
    if args.mode == "all" and args.limit == 0:
        per_target: dict[str, dict] = {}
        for row in per_sample:
            entry = per_target.setdefault(
                row["target"], {"total": 0, "ok": 0, "tool_failures": 0, "stmt": row.get("stmt", "")}
            )
            if is_tool_failure(row["new_reason"]):
                entry["tool_failures"] += 1
                continue
            entry["total"] += 1
            entry["ok"] += 1 if row["new_ok"] else 0
        for entry in per_target.values():
            entry["solve_rate"] = round(entry["ok"] / entry["total"], 4) if entry["total"] else 0.0
        # 全部候选都因工具问题丢失的目标不能算进 hard（那是未知，不是"证不出"）
        known = {tid: e for tid, e in per_target.items() if e["total"] > 0}
        nonzero = [tid for tid, e in known.items() if e["solve_rate"] > 0]
        hard = [tid for tid, e in known.items() if e["solve_rate"] == 0]
        nonzero_ratio = len(nonzero) / len(per_target) if per_target else 0.0
        report["corrected_g1"] = {
            "targets": len(per_target),
            "targets_with_verdicts": len(known),
            "candidates": len(per_sample),
            "verified": sum(1 for row in per_sample if row["new_ok"]),
            "tool_failures": tool_failures,
            "nonzero_targets": len(nonzero),
            "nonzero_ratio": round(nonzero_ratio, 4),
            "mean_solve_rate": round(
                sum(e["solve_rate"] for e in per_target.values()) / len(per_target), 4
            ) if per_target else 0.0,
            "verdict": "pass" if nonzero_ratio >= 0.2 else "fail_no_signal",
            "hard_targets": len(hard),
            "per_target": per_target,
        }
        hard_path = Path(args.hard_out)
        hard_path.parent.mkdir(parents=True, exist_ok=True)
        with hard_path.open("w", encoding="utf-8") as handle:
            for tid in hard:
                handle.write(json.dumps(
                    {"id": tid, "statement": per_target[tid]["stmt"],
                     "note": f"修正后 solve_rate=0（预算 {args.heartbeats} 心跳 / {args.timeout_ms} ms）"},
                    ensure_ascii=False) + "\n")
        report["hard_out"] = str(hard_path.relative_to(ROOT))
        print(
            f"[diag] 修正后 G1：目标 {len(per_target)} / 候选 {len(per_sample)} / "
            f"通过 {report['corrected_g1']['verified']} / 非零解目标 {len(nonzero)}"
            f"（{nonzero_ratio:.1%}）/ 判定 {report['corrected_g1']['verdict']}"
        )
        print(f"[diag] hard 目标 {len(hard)} 条写入 {hard_path}")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[diag] 翻转后判定分布：{dict(flip)}")
    print(
        f"[diag] 脱离 exception 的样本 {rescued}/{len(sampled)}（{report['rescued_ratio']:.0%}），"
        f"其中直接判 ok 的 {report['newly_ok']} 条"
    )
    print(f"[diag] 耗时 {elapsed:.0f}s（frontend {frontend_ms}ms）；报告：{out_path}")
    for row in per_sample[:10]:
        print(f"       {row['target']}#{row['index']}: exception -> {row['new_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
