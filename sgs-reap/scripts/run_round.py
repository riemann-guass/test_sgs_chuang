"""跑 SG-Lean 的闭环（多轮）。命令行入口，编排逻辑全在 `sgsr/pipeline/runner.py`。

用法：

    # 离线冒烟（假服务）：验证闭环能转起来
    python scripts\run_round.py --rounds 2 --target-limit 3 --k 1 --n 2 --expect-mock

    # 真跑（先起 MODELS\\proxy.py，需网络权限）
    python scripts\run_round.py --rounds 5 --k 3 --n 3

**数据角色（`docs/data-protocol.md`，这里用代码强制）**：

* `--curriculum`：建库用的课程集，**只有它可以进库**。默认用我们自建的初等引理集
  （`data/lemmas_g1.jsonl`），它完全与 miniF2F 无关。
* `--dev`：调参用（miniF2F **valid**）。本脚本**不读它**——调参在
  `scripts/run_gate_g3_real.py --select ...` 里做。
* `--test`：最终评测（miniF2F **test**）。**本脚本拒绝接受 miniF2F 作为 curriculum**：
  拿测试集建库再在测试集上测，结论是自我循环的（phase19–22 踩过这个坑）。
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
sys.path.insert(0, str(ROOT))

from sgsr.pipeline.runner import (  # noqa: E402
    ROLE_CURRICULUM,
    DataRoleError,
    RoundConfig,
    RoundRunner,
    TargetSet,
    summarize,
)
from sgsr.pipeline.library import LibrarySourceError, assert_clean_sources  # noqa: E402
from sgsr.lean import (  # noqa: E402
    LeanServer,
    budget_for_jobs,
    materialize_imports,
    resolve_imports,
)

DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"
RUNS = ROOT / "experiments" / "runs"
DEFAULT_LIBRARY = ROOT / "experiments" / "library.jsonl"
DEFAULT_GENERATED = ROOT / "sgslean" / "SgsLean" / "GeneratedLibrary.lean"


def make_factory(imports: str):
    """Lean 服务工厂：每次调用按批大小给足心跳（心跳是按 command 累计的，见 lean_server.py）。"""

    def factory(n_jobs: int = 8):
        return LeanServer(
            imports=imports,
            heartbeats=budget_for_jobs(n_jobs),
            stderr_path=RESULTS / "round_stderr.log",
        )

    return factory


def main() -> int:
    parser = argparse.ArgumentParser(description="SG-Lean 闭环（多轮）")
    parser.add_argument("--curriculum", default=str(DATA / "lemmas_g1.jsonl"),
                        help="课程集 C：唯一允许进库的数据")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--target-limit", type=int, default=0, help="本轮用多少条 C 目标（0=全部）")
    parser.add_argument("--k", type=int, default=3, help="每条候选让 Solver 出几篇证明")
    parser.add_argument("--n", type=int, default=3, help="每个目标让 Conjecturer 出几条候选")
    parser.add_argument("--library-budget", type=int, default=30, help="库容 B")
    parser.add_argument("--ctx-tokens", type=int, default=1200,
                        help="提示词预算（token）：选择层的约束口径")
    parser.add_argument("--reuse-threshold", type=int, default=2,
                        help="复用门槛：被至少这么多不同目标引用过才准入")
    parser.add_argument("--evict-after", type=int, default=3,
                        help="僵尸淘汰：reuse=0 且入库超过这么多轮 → 冷存")
    parser.add_argument("--exploration-slots", type=int, default=8,
                        help="探索额度：一轮最多让多少条没有复用证据的新引理入库")
    parser.add_argument("--prompt-slots", type=int, default=16,
                        help="提示词里最多放几条引理（与 proxy 的截断上限一致）")
    parser.add_argument("--source-corpus", default="C1",
                        help="本库的来源语料标识（C/C1/C2/C3；写进每条引理供事后审计）")
    parser.add_argument("--library", default=str(DEFAULT_LIBRARY))
    parser.add_argument("--generated", default=str(DEFAULT_GENERATED))
    parser.add_argument("--solve-endpoint", default="http://127.0.0.1:8765/solve")
    parser.add_argument("--conjecture-endpoint", default="http://127.0.0.1:8765/conjecture")
    parser.add_argument("--imports", default=None,
                        help="验证环境 import；默认按库是否非空自动决定")
    parser.add_argument("--reset-library", action="store_true",
                        help="开始前清空库（离线冒烟用；真跑慎用）")
    parser.add_argument("--materialize-only", action="store_true",
                        help="只重建物化文件并编译（不跑轮）；吸收自旧 run_materialize.py")
    parser.add_argument("--expect-mock", action="store_true",
                        help="显式声明用的是假服务（仅用于离线冒烟，会写进报告文件名）")
    args = parser.parse_args()

    curriculum_path = Path(args.curriculum)
    # ── 数据角色守卫：不许拿测试集/开发集建库 ──
    # 两道：① 先按文件名快速拒绝（给出可读的报错）；
    #        ② 再让 `assert_buildable()` 按**解析后的绝对路径**复核，
    #           这样把 D/T 复制改名也绕不过去（审计 P0 第 5 条）。
    if "minif2f" in curriculum_path.name.lower():
        print(f"[round] 拒绝：`{curriculum_path.name}` 是 miniF2F（开发/测试集）。")
        print("        按 docs/data-protocol.md，建库只能用课程集 C；")
        print("        miniF2F valid 用于调参（scripts/run_gate_g3_real.py --select），test 只用于最终评测。")
        return 2

    curriculum = TargetSet.load(ROLE_CURRICULUM, curriculum_path)
    try:
        curriculum.assert_buildable()
    except DataRoleError as exc:
        print(f"[round] 数据角色错误：{exc}")
        return 2
    library_path = Path(args.library)
    generated_path = Path(args.generated)
    if args.reset_library and library_path.exists():
        library_path.unlink()
        print(f"[round] 已清空库：{library_path}")
    if library_path.exists():
        # 读库时复核来源：库里出现 D/T 来源的引理就不许继续（硬约束 1）
        try:
            assert_clean_sources(library_path)
        except LibrarySourceError as exc:
            print(f"[round] {exc}")
            return 2
    library_nonempty = library_path.exists() and library_path.read_text(encoding="utf-8").strip()
    try:
        imports = resolve_imports(library_path, args.imports)
    except ValueError as exc:
        print(f"[round] import 配置错误：{exc}")
        return 2
    if library_nonempty and not generated_path.exists():
        print(f"[round] 警告：库非空但物化文件不存在（{args.generated}）——"
              f"先跑 `scripts/run_round.py --materialize-only` 重建它")
    config = RoundConfig(
        solve_endpoint=args.solve_endpoint,
        conjecture_endpoint=args.conjecture_endpoint,
        imports=imports,
        k_solve=args.k,
        n_conjecture=args.n,
        library_budget=args.library_budget,
        target_limit=args.target_limit,
        source_corpus=args.source_corpus,
        reuse_threshold=args.reuse_threshold,
        evict_after=args.evict_after,
        exploration_slots=args.exploration_slots,
        prompt_slots=args.prompt_slots,
        ctx_lemma_tokens=args.ctx_tokens,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / f"round_{'mock' if args.expect_mock else 'real'}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    runner = RoundRunner(
        curriculum=curriculum,
        config=config,
        workdir=run_dir,
        library_path=library_path,
        generated_path=generated_path,
        lean_server_factory=make_factory(imports),
        # 写物化文件的那次会话用**基础 import**：生成库自己不能出现在生成文件的
        # import 里（自我循环导入，编译必失败）。
        materialize_factory=make_factory(materialize_imports(imports)),
    )

    started = time.perf_counter()
    if args.materialize_only:
        # 库没变、物化文件丢了或过时：单独重建一次（旧 run_materialize.py 的唯一用途）。
        written = runner.materialize_library()
        print(f"[round] --materialize-only：物化 {written} 条 → {generated_path}")
        return 0
    try:
        reports = runner.run(args.rounds, on_round=lambda r: (run_dir / f"round_{r.round_index}.json")
                             .write_text(json.dumps(r.to_dict(), ensure_ascii=False, indent=2),
                                         encoding="utf-8"))
    except DataRoleError as exc:
        print(f"[round] 数据角色错误：{exc}")
        return 2
    summary = summarize(reports)
    summary["curriculum"] = str(curriculum_path.relative_to(ROOT))
    summary["total_s"] = round(time.perf_counter() - started, 1)
    summary["library_after"] = reports[-1].library_after if reports else []

    out_path = RESULTS / f"rounds_{'mock' if args.expect_mock else 'real'}_{args.rounds}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[round] 完成 {len(reports)} 轮；库 {len(summary['library_after'])} 条；"
          f"总耗时 {summary['total_s']}s")
    for row in summary["per_round"]:
        print(f"        round {row['round']}: 解出 {row['targets_solved']}；候选 {row['candidates']}；"
              f"过硬门 {row['passed_hard_gates']}；验证通过 {row['verified']}；入库 {row['library_written']}")
    # 复用判据的现场证据：有多少条达到门槛、多少条被目标侧证明引用过、提示词给了几条。
    # 这三条一起看才能回答"库到底有没有进入环路"。
    for report in reports:
        buckets = (report.reuse.get("buckets") or {})
        print(f"        round {report.round_index}: 库 {report.funnel.get('library_size', 0)} 条"
              f"（可复用 {buckets.get('reusable', 0)}、被用过一次 {buckets.get('used_once', 0)}、"
              f"未用 {buckets.get('unused', 0)}）；"
              f"本轮被引用 {report.funnel.get('cited_lemmas', 0)} 条 / "
              f"{report.funnel.get('cited_targets', 0)} 次目标引用；"
              f"提示词注入 {report.funnel.get('prompt_size', 0)} 条")
    print(f"[round] 报告 {out_path}；逐轮 {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
