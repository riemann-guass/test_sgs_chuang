"""LeanReuse 离线建库。命令行入口，编排逻辑全在 `sgsr/pipeline/runner.py`。

用法：

    # 离线冒烟（假服务）：验证建库流程能转起来
    python scripts/build_library.py --rounds 2 --target-limit 3 --k 1 --n 2 --expect-mock

    # 真跑（先起 MODELS\\proxy.py，需网络权限）
    python scripts/build_library.py --rounds 5 --k 3 --n 3

**数据角色（`docs/data-protocol.md`，这里用代码强制）**：

* 默认显式读取 miniF2F valid 派生的 C-build/C-measure；
* 自定义数据也必须分别通过 `--c-build` 和 `--c-measure` 提供；
* 开发集 D 不进入本脚本；需要检查库的效果时使用 `scripts/compare_library.py`。
* 最终测试集 T 不得作为两个建库输入中的任何一个。
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
    ROLE_C_BUILD,
    ROLE_C_MEASURE,
    DataRoleError,
    RoundConfig,
    RoundRunner,
    TargetSet,
    assert_disjoint_curriculum,
    summarize,
)
from sgsr.pipeline.library import (  # noqa: E402
    LibrarySourceError,
    active_rows,
    assert_clean_sources,
    assert_snapshot_ready,
)
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
    parser = argparse.ArgumentParser(description="LeanReuse 离线引理库构建")
    parser.add_argument("--c-build", default=str(DATA / "minif2f_c_build.jsonl"),
                        help="C-build JSONL")
    parser.add_argument("--c-measure", default=str(DATA / "minif2f_c_measure.jsonl"),
                        help="C-measure JSONL")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--target-limit", type=int, default=0,
                        help="本轮用多少条 C-build 目标（0=全部）")
    parser.add_argument("--measure-target-limit", type=int, default=0,
                        help="本轮用多少条 C-measure 目标（0=全部）")
    parser.add_argument("--k", type=int, default=3, help="每条候选命题生成几篇证明")
    parser.add_argument("--n", type=int, default=3, help="每个未解目标生成几条候选引理")
    parser.add_argument("--library-budget", type=int, default=30, help="引理库条目上限")
    parser.add_argument("--ctx-tokens", type=int, default=1200,
                        help="提示词预算（token）：选择层的约束口径")
    parser.add_argument("--reuse-threshold", type=int, default=2,
                        help="复用门槛：被至少这么多不同目标引用过才准入")
    parser.add_argument("--evict-after", type=int, default=3,
                        help="无复用记录且超过这么多轮后移出在线库")
    parser.add_argument("--exploration-slots", type=int, default=8,
                        help="探索额度：一轮最多让多少条没有复用证据的新引理入库")
    parser.add_argument("--prompt-slots", type=int, default=16,
                        help="提示词里最多放几条引理（与 proxy 的截断上限一致）")
    parser.add_argument("--source-corpus", default="MF_VALID_C",
                        help="本库的来源语料标识（正式流程固定 MF_VALID_C）")
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

    try:
        c_build = TargetSet.load(ROLE_C_BUILD, Path(args.c_build))
        c_measure = TargetSet.load(ROLE_C_MEASURE, Path(args.c_measure))
        assert_disjoint_curriculum(c_build, c_measure)
        split_manifest = {
            "schema": 1,
            "algorithm": "explicit-independent-inputs",
            "c_build": {"path": str(c_build.path), "rows": len(c_build.rows),
                        "hash": c_build.content_hash()},
            "c_measure": {"path": str(c_measure.path), "rows": len(c_measure.rows),
                          "hash": c_measure.content_hash()},
            "identity_overlap": 0,
            "statement_overlap": 0,
        }
    except DataRoleError as exc:
        print(f"[round] 数据角色错误：{exc}")
        return 2
    library_path = Path(args.library)
    generated_path = Path(args.generated)
    if generated_path.resolve() != DEFAULT_GENERATED.resolve():
        print("[round] 拒绝：--generated 只能指向 sgslean/SgsLean/GeneratedLibrary.lean；"
              "Lean import 与 lake build 都绑定这个模块，任意路径不会被实际加载。")
        return 2
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
    active_nonempty = bool(active_rows(library_path))
    try:
        imports = resolve_imports(library_path, args.imports)
    except ValueError as exc:
        print(f"[round] import 配置错误：{exc}")
        return 2
    if active_nonempty and not args.materialize_only:
        try:
            assert_snapshot_ready(library_path, generated_path)
        except (LibrarySourceError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[round] 活动库快照不可用：{exc}")
            print("        请先运行同一命令并加 --materialize-only。")
            return 2
    config = RoundConfig(
        solve_endpoint=args.solve_endpoint,
        conjecture_endpoint=args.conjecture_endpoint,
        imports=imports,
        k_solve=args.k,
        n_conjecture=args.n,
        library_budget=args.library_budget,
        target_limit=args.target_limit,
        measure_target_limit=args.measure_target_limit,
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
    (run_dir / "data_split_manifest.json").write_text(
        json.dumps(split_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    runner = RoundRunner(
        c_build=c_build,
        c_measure=c_measure,
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
    summary["c_build"] = str(c_build.path)
    summary["c_measure"] = str(c_measure.path)
    summary["data_split"] = split_manifest
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
