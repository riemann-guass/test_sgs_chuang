"""在一个数据集上批量评测证明器（P1，规格 9.1 与 8.2 节）。

    python scripts/run_prover_eval.py --set D --k 4 --out experiments/results/p1_dev.json

必须同时报出四个量：

1. **目标级 pass@k**（主指标）：一道题只要有 ≥1 篇候选过内核即算解出；
2. **CostPerSolved** = 总 token / 解出题数；
3. **兜底命中率**：零模型调用那部分单独摘出来（它不花 token，
   混进成本会让 CostPerSolved 虚假地低）；
4. **按领域的通过率分桶**（数据集有 `domain` 字段时才分，否则按来源/数据集名分）。

## 数据角色（`--set` 的取值）

| `--set` | 文件 | 允许用途 |
|---|---|---|
| `C` | 由 `--path` 给出（自建课程集） | 建库 |
| `D` | `data/minif2f_valid.jsonl` | 调参（可反复跑） |
| `T` | `data/minif2f_test.jsonl` | **只跑一次**，框架冻结后 |

`T` 会打一条显眼的警告并要求显式 `--i-know-test-is-one-shot`：
按 `docs/data-protocol.md`，T 跑过就不再是留出集，这个开关存在的意义是
让"我确实要动 T"变成一个必须手写的决定，而不是手滑。

## 记账口径（写死在报告里）

* `tokens` = prompt + completion + reasoning 三类 token 之和（后端 `usage` 原样累加）；
* 兜底命中**不**贡献 token，但要单独计一条 `cheap_hits`——它是"白拿"的解出；
* 后端错误（503/超时）单独计 `backend_errors`，**不计入**失败率解释：
  那是装置问题，不是模型能力问题。
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

from sgsr.pipeline.prover import Budget, Prover  # noqa: E402
from sgsr.pipeline.prover import git_commit, library_hash  # noqa: E402

DATA = ROOT / "data"
REGISTERED = {
    "D": DATA / "minif2f_valid.jsonl",
    "T": DATA / "minif2f_test.jsonl",
    "C1": DATA / "lemmas_g1.jsonl",
}
#: T 的一次性运行台账：跑过 T 就写一条记录，再跑会被拒绝。
#: 审计指出"只靠一个命令行开关"不足以守住"T 只跑一次"这条协议——
#: 换个终端、换个人、或者忘了带开关都能绕过去。台账让它变成**不可逆的事实记录**。
TEST_LEDGER = ROOT / "experiments" / "results" / "test_runs_ledger.jsonl"
DEFAULT_LIBRARY = ROOT / "experiments" / "library.jsonl"
DEFAULT_ENDPOINT = "http://127.0.0.1:8770/solve"


def read_test_ledger() -> list[dict]:
    if not TEST_LEDGER.exists():
        return []
    return [
        json.loads(line)
        for line in TEST_LEDGER.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_test_ledger(record: dict) -> None:
    TEST_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with TEST_LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def is_test_run(args, label: str) -> bool:
    """这次运行算不算"动 T"。按**解析后的绝对路径**判断，不靠 `--set` 的名字：
    用 `--path` 指向同一个文件也算动 T。"""
    if args.dataset == "T":
        return True
    if not args.path:
        return False
    try:
        given = Path(args.path).resolve()
    except OSError:
        return False
    registered = REGISTERED.get("T")
    return registered is not None and given == registered.resolve()


def _backend_model(endpoint: str) -> str:
    """问一下代理 `/health` 拿模型名（报告元数据的一部分）。

    拿不到就记 `"unknown"`：**不抛异常**——报告缺一个字段不该让整次实验失败，
    但"缺了哪个字段"要能看出来。
    """
    import urllib.error
    import urllib.request

    url = endpoint.rsplit("/", 1)[0] + "/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return f"{payload.get('backend')}/{payload.get('model')}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="批量评测证明器")
    p.add_argument("--set", dest="dataset", required=True,
                   help="数据集：已注册名（D/T/C1）或 `--path` 给出的文件")
    p.add_argument("--path", default=None, help="数据集文件（`--set C` 时用它给课程集）")
    p.add_argument("--k", type=int, default=4, help="采样篇数")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 表示全部）")
    p.add_argument("--ids", default=None, help="只跑这些 id（逗号分隔），用于复现单题")
    p.add_argument("--out", required=True, help="报告写出路径")
    p.add_argument("--library", default=None,
                   help="库文件路径（默认 experiments/library.jsonl；传 none 表示无库基线臂）")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="求解端点")
    p.add_argument("--imports", default=None, help="验证环境的 import 列表")
    p.add_argument("--no-cheap", action="store_true", help="关掉廉价兜底（消融）")
    p.add_argument("--no-repair", action="store_true", help="关掉 repair（消融）")
    p.add_argument("--i-know-test-is-one-shot", action="store_true",
                   help="确认要在 T（miniF2F test）上跑——按协议只能跑一次")
    p.add_argument("--force-test-rerun", action="store_true",
                   help="台账里已有 T 记录时仍要跑（会追加一条台账记录，不删除历史）")
    return p


def load_rows(args) -> tuple[list[dict], str]:
    if args.path:
        path = Path(args.path)
        label = path.name
    elif args.dataset in REGISTERED:
        path = REGISTERED[args.dataset]
        label = args.dataset
    else:
        path = Path(args.dataset)
        label = path.name
    if not path.exists():
        raise SystemExit(f"[eval] 找不到数据集 {path}")
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        row.setdefault("statement", row.get("stmt", ""))
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        rows = [row for row in rows if row.get("id") in wanted]
    if args.limit:
        rows = rows[: args.limit]
    return rows, label


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows, label = load_rows(args)
    if not rows:
        raise SystemExit("[eval] 数据集为空")

    # ── T 的一次性守卫（两道）──
    # ① 命令行必须显式确认；② 台账里已经有记录就直接拒绝。
    # 台账是"不可逆事实"：跑过 T 就不再是留出集，这件事不该靠记忆或开关守护。
    test_run = is_test_run(args, label)
    if test_run:
        if not args.i_know_test_is_one_shot:
            raise SystemExit(
                "[eval] 拒绝：T 是最终测试集（data/minif2f_test.jsonl）。\n"
                "        按 docs/data-protocol.md，T 在框架冻结后**只跑一次**；\n"
                "        调参请用 D（minif2f valid）。确实要跑请加 --i-know-test-is-one-shot。"
            )
        previous = read_test_ledger()
        if previous and not args.force_test_rerun:
            last = previous[-1]
            raise SystemExit(
                f"[eval] 拒绝：台账里已有 {len(previous)} 次 T 运行，最近一次 "
                f"{last.get('generated_at')}（commit {last.get('commit')}，"
                f"pass@{(last.get('config') or {}).get('k')}="
                f"{last.get('pass_at_k')}）。\n"
                "        按协议 T 只跑一次；若确实要覆盖，加 --force-test-rerun，"
                "并会在台账里留下第二条记录（不做删除）。"
            )
        print(f"[eval] 注意：这次运行会动 T。台账现有 {len(previous)} 条记录。")

    library_path = None
    if args.library and args.library.lower() not in ("none", "-"):
        library_path = Path(args.library)
    elif args.library is None and DEFAULT_LIBRARY.exists():
        library_path = DEFAULT_LIBRARY
    has_library = bool(library_path and library_path.exists())
    imports = args.imports or ("Mathlib,SgsLean.GeneratedLibrary" if has_library else "Mathlib")
    if args.no_cheap:
        import sgsr.pipeline.prover as prover_module  # noqa: PLC0415
        prover_module.CHEAP_DISABLED = True

    prover = Prover(endpoint=args.endpoint, library_path=library_path, imports=imports)
    budget = Budget(k=args.k, repair_rounds=0 if args.no_repair else 2)

    print(f"[eval] 集合 {label}：{len(rows)} 条；k={args.k}；库 {len(prover.library)} 条；"
          f"imports={imports}；cheap={'off' if args.no_cheap else 'on'}；"
          f"repair={'off' if args.no_repair else 2}")

    started = time.perf_counter()
    records: list[dict] = []
    for index, row in enumerate(rows, start=1):
        result = prover.prove(row["statement"], budget=budget)
        record = result.to_dict()
        record["id"] = row.get("id")
        record["domain"] = row.get("domain") or row.get("source") or label
        records.append(record)
        print(f"[eval] {index}/{len(rows)} {record['id']}: solved={record['solved']} "
              f"path={record['path']} tokens={sum(record['usage'].get(k, 0) for k in
                                                  ('prompt_tokens', 'completion_tokens',
                                                   'reasoning_tokens'))}")

    solved = [r for r in records if r["solved"]]
    tokens = {
        key: sum(int(r["usage"].get(key, 0) or 0) for r in records)
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "model_calls")
    }
    total_tokens = tokens["prompt_tokens"] + tokens["completion_tokens"] + tokens["reasoning_tokens"]
    cheap = [r for r in solved if r["path"] == "cheap"]
    backend_errors = [
        r for r in records
        if any(a.get("reason") == "backend_error" for a in r["attempts"])
    ]
    by_domain: dict[str, dict] = collections.defaultdict(lambda: {"n": 0, "solved": 0})
    for record in records:
        bucket = by_domain[str(record["domain"])]
        bucket["n"] += 1
        bucket["solved"] += 1 if record["solved"] else 0
    by_path = collections.Counter(r["path"] for r in records)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": label,
        # ── 报告元数据：审计指出旧报告缺这些，导致数字无法回溯 ──
        "commit": git_commit(),
        "library_hash": library_hash(library_path),
        "env": {
            "python": sys.version.split()[0],
            "lean_imports": imports,
            "endpoint": args.endpoint,
            "backend_model": _backend_model(args.endpoint),
            "heartbeats": "见 sgsr/verification/client.py 的 budget_for_jobs",
        },
        "config": {
            "k": args.k,
            "endpoint": args.endpoint,
            "imports": imports,
            "library": str(library_path) if library_path else None,
            "library_size": len(prover.library),
            "cheap_enabled": not args.no_cheap,
            "repair_rounds": 0 if args.no_repair else budget.repair_rounds,
        },
        "targets": len(records),
        "pass_at_k": len(solved) / len(records),
        "solved": len(solved),
        "cheap_hits": len(cheap),
        "cheap_hit_rate": len(cheap) / len(records),
        "model_solved": len(solved) - len(cheap),
        "backend_errors": len(backend_errors),
        "path_breakdown": dict(by_path),
        "tokens": {**tokens, "total": total_tokens},
        "cost_per_solved": (total_tokens / len(solved)) if solved else None,
        "by_domain": {
            domain: {**bucket, "rate": bucket["solved"] / bucket["n"]}
            for domain, bucket in sorted(by_domain.items())
        },
        "timing": {"total_s": round(time.perf_counter() - started, 1)},
        "per_target": records,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if test_run:
        # 台账只记"跑过 T"这件事本身：时间、提交号、库版本、配置与结果摘要。
        # 不写路径是因为它就在 `experiments/results/` 里，按时间能对上。
        append_test_ledger({
            "generated_at": report["generated_at"],
            "commit": report["commit"],
            "library_hash": report["library_hash"],
            "report": str(out_path.relative_to(ROOT)),
            "config": report["config"],
            "pass_at_k": report["pass_at_k"],
            "targets": report["targets"],
            "forced": bool(args.force_test_rerun),
        })
        print(f"[eval] 已把这次 T 运行写进台账 {TEST_LEDGER}（共 "
              f"{len(read_test_ledger())} 条）")
    print(f"[eval] pass@{args.k} = {report['pass_at_k']:.3f}（{len(solved)}/{len(records)}）；"
          f"兜底命中 {len(cheap)}；总 token {total_tokens}；"
          f"CostPerSolved {report['cost_per_solved']}")
    print(f"[eval] 报告写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
