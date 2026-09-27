"""在一个数据集上批量评测证明器（P1，规格 9.1 与 8.2 节）。

    python scripts/run_prover_eval.py --set D --k 4 --out experiments/results/p1_dev.json

必须同时报出四个量：

1. **目标级 pass@k**（主指标）：一道题只要有 ≥1 篇候选过内核即算解出。
   **三个口径一起报**，不混：`pass_at_k`（含兜底与 repair）、`pass_at_k_first_round`
   （只认首轮 solve/cheap）、`pass_at_k_model_only`（不含兜底）。只报一个数会被
   读者当成"k 篇独立采样的通过率"，而实际口径最多是 3×k 篇候选。
2. **CostPerSolved** = 总 token / 解出题数（计费口径 = prompt + completion）；
3. **兜底命中率**：零模型调用那部分单独摘出来（它不花 token，
   混进成本会让 CostPerSolved 虚假地低）；
4. **按领域的通过率分桶**（数据集有 `domain` 字段时才分，否则按来源/数据集名分）。

**分母**：门检拒绝（输入问题）与后端故障（装置问题）都从分母里剔除，单列成
`excluded_gate_rejected` / `excluded_backend_errors`。把它们算成"没解出"会让
一次 503 直接压低 pass@k。

## 数据角色（`--set` 的取值）

| `--set` | 文件 | 允许用途 |
|---|---|---|
| `C` | 由 `--path` 给出（miniF2F valid 的 C 分区） | 建库 |
| `D` | `data/minif2f_dev.jsonl` | 调参（可反复跑） |
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
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgsr.pipeline.prover import Budget, Prover  # noqa: E402
from sgsr.pipeline.prover import git_commit, library_hash, usage_total  # noqa: E402
from sgsr.lean import preflight_in_session, resolve_imports, SGSLEAN  # noqa: E402
from sgsr.pipeline.library import (  # noqa: E402
    LibrarySourceError, active_rows, assert_snapshot_ready,
)

DATA = ROOT / "data"
REGISTERED = {
    "D": DATA / "minif2f_dev.jsonl",
    "T": DATA / "minif2f_test.jsonl",
    "C-build": DATA / "minif2f_c_build.jsonl",
    "C-measure": DATA / "minif2f_c_measure.jsonl",
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


def _heartbeats() -> int:
    """当前生效的单作业心跳预算（写进报告，供事后判断"判定是不是被预算掐死"）。"""
    from sgsr.lean import budget_for_jobs

    return budget_for_jobs()


def classify_tier(record: dict, k: int) -> str:
    """难度分档（吸收自旧 `scripts/calibrate_difficulty.py`）。

    分档的理由是**增益只在近失手区间有信号**：兜底或首轮全解的题给不给库都一样，
    稳定解不出的题余量为 0。口径与旧标定脚本一致，分母固定为 `k`（没生成出证明
    也算失败），只是这里从"证明器自己的逐题记录"里读，不再多跑一次采样：

    * `easy`：兜底命中（`path=cheap`），或首轮 k 篇全过；
    * `nearmiss`：首轮 0 < 通过篇数 < k（唯一"能被一条引理翻过来"的档）；
    * `hard`：首轮一篇都没过。

    与旧脚本的差异（如实记在这里）：旧脚本另有 `unknown_cheap` 一档（兜底清扫被
    墙钟上限截断，兜底能否解出未知）。证明器主路径不返回 `exhausted`，所以这一档
    在这里退化成 `easy`/`hard`；需要那一档时用 `cheap_budget_ms` 调大兜底预算重跑。
    """
    if str(record.get("path") or "") == "cheap":
        return "easy"
    passed_first_round = sum(
        1 for a in (record.get("attempts") or [])
        if a.get("ok") is True and int(a.get("round") or 0) == 0
    )
    if passed_first_round >= k:
        return "easy"
    if passed_first_round > 0:
        return "nearmiss"
    return "hard"


def write_tiers(rows: list[dict], records: list[dict], tier_dir: Path,
                label: str) -> dict[str, int]:
    """按分档写出 `<tier_dir>/<label>__<tier>.jsonl`（派生文件，随时可由本命令重建）。"""
    by_id = {str(r.get("id")): r for r in records}
    buckets: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        record = by_id.get(str(row.get("id")))
        if record is None:
            continue
        tier = str(record.get("tier") or "unknown")
        buckets[tier].append(dict(row, tier=tier))
    tier_dir.mkdir(parents=True, exist_ok=True)
    for tier, subset in buckets.items():
        (tier_dir / f"{label}__{tier}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in subset),
            encoding="utf-8",
        )
    return {tier: len(subset) for tier, subset in sorted(buckets.items())}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="批量评测证明器")
    p.add_argument("--set", dest="dataset", required=True,
                   help="数据集：已注册名（C-build/C-measure/D/T）或文件路径")
    p.add_argument("--path", default=None, help="显式覆盖数据集文件")
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
    p.add_argument("--resume", action="store_true",
                   help="接着 --out 里已有的报告跑：跳过已完成的 id（报告是逐题落盘的）")
    p.add_argument("--tier-out", default=None,
                   help="按难度分档写出 `<dir>/<label>__{easy,nearmiss,hard}.jsonl`"
                        "（吸收自旧 calibrate_difficulty.py；派生文件，可随时重建）")
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
    # 留一份未过滤的清单：分档要覆盖**整批**（含 --resume 已经跑完的那些）。
    all_rows = list(rows)

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
    try:
        imports = resolve_imports(library_path, args.imports)
    except ValueError as exc:
        raise SystemExit(f"[eval] import 配置错误：{exc}") from exc
    if library_path and active_rows(library_path):
        try:
            assert_snapshot_ready(library_path, SGSLEAN / "SgsLean" / "GeneratedLibrary.lean")
        except (LibrarySourceError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"[eval] 活动库快照不可用：{exc}") from exc
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
    if args.resume and Path(args.out).exists():
        try:
            previous = json.loads(Path(args.out).read_text(encoding="utf-8"))
            records = [r for r in (previous.get("per_target") or []) if r.get("id")]
        except ValueError:
            records = []
        done = {str(r["id"]) for r in records}
        if done:
            rows = [r for r in rows if str(r.get("id")) not in done]
            print(f"[eval] --resume：已有 {len(done)} 条完成，继续 {len(rows)} 条")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    already = len(records)          # --resume 时已经完成的条数
    total_planned = already + len(rows)
    # **整批共用一个 Lean 会话**：子进程跨批常驻（`SgsLean/Server.lean` 的 `serveLoop`），
    # 于是 Mathlib 只导入一次，而不是每题一次。实测：20 题从约 5 小时降到分钟级。
    # 环境预检也在同一会话里做（有库时 `import SgsLean.GeneratedLibrary` 不可用要么
    # 在这里炸、要么让整批结果失真）。
    with prover.session() as lean:
        preflight_ok, preflight_detail = preflight_in_session(lean)
        if not preflight_ok:
            raise SystemExit(
                f"[eval] 环境预检失败（imports={imports}）：{preflight_detail}\n"
                "        有库时请先 `cd sgslean && lake build SgsLean.GeneratedLibrary`，"
                "或去掉 --library 跑无库臂。"
            )
        print(f"[eval] 环境预检通过（imports={imports}；整批共用一个会话）")
        for offset, row in enumerate(rows, start=1):
            index = already + offset
            result = prover.prove(row["statement"], budget=budget, lean=lean)
            record = result.to_dict()
            record["id"] = row.get("id")
            record["domain"] = row.get("domain") or row.get("source") or label
            # 难度分档（easy / nearmiss / hard）就写在逐题记录里，报告汇总见 build_report。
            record["tier"] = classify_tier(record, args.k)
            records.append(record)
            print(f"[eval] {index}/{total_planned} {record['id']}: solved={record['solved']} "
                  f"path={record['path']} tokens={sum(record['usage'].get(k, 0) for k in
                                                       ('prompt_tokens', 'completion_tokens',
                                                        'reasoning_tokens'))}")
            # **逐题落盘**：整轮跑完才写报告的话，末尾一个异常（实测踩过：少 import 一个
            # `os` 就 `NameError`）会把几小时的结果全丢掉。这里每题都刷一次，
            # 中途崩掉也留下一份带 `partial=true` 的可用报告。
            out_path.write_text(
                json.dumps(build_report(records, label=label, args=args, budget=budget,
                                        library_path=library_path, imports=imports,
                                        prover=prover, started=started, partial=True),
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    report = build_report(records, label=label, args=args, budget=budget,
                          library_path=library_path, imports=imports, prover=prover,
                          started=started, partial=False)
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
    def pct(value) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    print(f"[eval] 可评测 {report['evaluable']}/{report['targets']}"
          f"（门检拒绝 {report['excluded_gate_rejected']}、"
          f"装置故障 {report['excluded_backend_errors']} 已剔除）")
    print(f"[eval] pass@{args.k} = {pct(report['pass_at_k'])}"
          f"（{report['solved']}/{report['evaluable']}）"
          f"；首轮 {pct(report['pass_at_k_first_round'])}"
          f"；不含兜底 {pct(report['pass_at_k_model_only'])}")
    print(f"[eval] 兜底命中 {report['cheap_hits']}；"
          f"总 token {report['tokens']['total']}（计费口径 = prompt+completion）；"
          f"CostPerSolved {report['cost_per_solved']}")
    if args.tier_out:
        tiers = write_tiers(all_rows, records, Path(args.tier_out), label)
        print(f"[eval] 分档清单写入 {args.tier_out}：{tiers}")
    print(f"[eval] 报告写入 {out_path}")
    return 0


def build_report(records: list[dict], *, label: str, args, budget, library_path, imports: str,
                 prover, started: float, partial: bool) -> dict:
    """把已跑完的逐题记录汇总成报告。

    **抽成函数**是为了支持逐题落盘（见 `main` 的循环）：跑一道、写一次，
    末尾崩掉也不会把几小时的结果一起丢掉。`partial=true` 表示"后面还有题在跑"。

    两个口径必须一起报（规格 8.2 + 审计"不要把装置故障算成模型能力"）：
    输入问题（门检拒绝）与后端故障都**不是**这道题"没解出"，从分母里剔除并单列。
    """
    def first_round_ok(record: dict) -> bool:
        return any(a.get("ok") is True and int(a.get("round") or 0) == 0
                   and a.get("source") in ("cheap", "solve")
                   for a in record["attempts"])

    excluded_gate = [r for r in records if r["path"] == "gate_rejected"]
    backend_errors = [
        r for r in records
        if any(a.get("reason") == "backend_error" for a in r["attempts"])
    ]
    for r in excluded_gate:
        r["excluded"] = "gate_rejected"        # 输入/命题本身的问题，不进分母
    for r in backend_errors:
        r.setdefault("excluded", "backend_error")   # 装置故障，不进分母
    evaluable = [r for r in records if not r.get("excluded")]
    solved = [r for r in records if r["solved"]]
    solved_evaluable = [r for r in evaluable if r["solved"]]
    model_only = [r for r in solved_evaluable if r["path"] != "cheap"]
    first_round = [r for r in solved_evaluable if first_round_ok(r)]
    tokens = {
        key: sum(int(r["usage"].get(key, 0) or 0) for r in records)
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                    "model_calls", "cache_hits")
    }
    # 计费口径 = prompt + completion；`reasoning_tokens` 是 completion 的子集，
    # 三类相加会把推理 token 算两遍（thinking 打开时虚增 30%–50%）。
    total_tokens = usage_total(tokens)
    cheap = [r for r in solved if r["path"] == "cheap"]
    by_domain: dict[str, dict] = collections.defaultdict(lambda: {"n": 0, "solved": 0})
    for record in records:
        bucket = by_domain[str(record["domain"])]
        bucket["n"] += 1
        bucket["solved"] += 1 if record["solved"] else 0
    by_path = collections.Counter(r["path"] for r in records)

    report = {
        "partial": partial,
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
            # 判定预算必须写进报告：预算一变，同一道题的判定就可能从 exception 翻成 ok
            #（实测 aime_1984_p15 在 4M 心跳下门检失败、400M 下通过）。
            "heartbeats_per_job": _heartbeats(),
            "cheap_budget_ms": os.environ.get("SGSLEAN_CHEAP_BUDGET_MS", "60000（默认）"),
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
        # 主指标：在**可评测**目标上（剔除输入问题与装置故障）。这是对外的 pass@k。
        "pass_at_k": (len(solved_evaluable) / len(evaluable)) if evaluable else None,
        # 同一条命令里的另外两个口径，必须一起报，否则"pass@k 含 repair 与兜底"
        # 这件事会被读者误当成 k 篇独立采样的通过率。
        "pass_at_k_first_round": (len(first_round) / len(evaluable)) if evaluable else None,
        "pass_at_k_model_only": (len(model_only) / len(evaluable)) if evaluable else None,
        "evaluable": len(evaluable),
        "excluded_gate_rejected": len(excluded_gate),
        "excluded_backend_errors": len(backend_errors),
        "solved_any": len(solved),
        "solved": len(solved),
        "cheap_hits": len(cheap),
        "cheap_hit_rate": (len(cheap) / len(evaluable)) if evaluable else None,
        "model_solved": len(model_only),
        "backend_errors": len(backend_errors),
        "path_breakdown": dict(by_path),
        # 难度分档：增益只在 nearmiss 档有信号，报告必须带上分布，
        # 否则"这批题里有多少是本来就无余量的"只能靠人工翻 per_target。
        "tiers": dict(sorted(collections.Counter(
            str(r.get("tier") or "unknown") for r in records
        ).items())),
        "tiers_criterion": ("easy = path=cheap 或首轮 k 篇全过；nearmiss = 首轮 0<通过<k；"
                            "hard = 首轮 0 篇通过。分母固定为 k（吸收自 calibrate_difficulty.py）"),
        "tokens": {**tokens, "total": total_tokens},
        "cost_per_solved": (total_tokens / len(solved_evaluable)) if solved_evaluable else None,
        "by_domain": {
            domain: {**bucket, "rate": bucket["solved"] / bucket["n"]}
            for domain, bucket in sorted(by_domain.items())
        },
        "timing": {"total_s": round(time.perf_counter() - started, 1)},
        "per_target": records,
    }
    return report


if __name__ == "__main__":
    raise SystemExit(main())
