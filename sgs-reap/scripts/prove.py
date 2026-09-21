"""证明器命令行入口（P1）。

规格 9.1 节的验收命令：

    python scripts/prove.py --statement "forall (n : Nat), n + 0 = n" --k 4
    python scripts/prove.py --file tests/examples/t1.lean --k 4

输出一个 RunRecord（字段见规格文档附录 A），其中 `proof` 一定通过了内核终检。

## 两个开关值得单独说

* `--imports`：验证环境导入什么。**有库时必须**用 `Mathlib,SgsLean.GeneratedLibrary`，
  否则证明里写 `sgs_lem_1` 会报 `unknown identifier`；库里没有引理时贴这个 import
  反而要额外编译一个模块。默认因此是"有库就带库、没库就用 Mathlib"。
* `--no-cheap`：关掉第 3 步的廉价兜底。它是"零模型调用"那一档的消融开关，
  P3 报成本时要把兜底命中率单独摘出来（兜底命中不花 token，
  混进 CostPerSolved 会让成本看起来虚假地低）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgsr.pipeline.prover import Budget, Prover  # noqa: E402

DEFAULT_LIBRARY = ROOT / "experiments" / "library.jsonl"
DEFAULT_ENDPOINT = "http://127.0.0.1:8770/solve"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SG-Lean 证明器：一条命题进，一篇过内核的证明出")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--statement", help="闭式命题字符串")
    src.add_argument("--file", help="含 theorem/example ... := by sorry 的 .lean 文件")
    p.add_argument("--k", type=int, default=4, help="采样篇数（默认 4，规格 3.9）")
    p.add_argument("--repair-rounds", type=int, default=2, help="repair 轮数上限 R（默认 2）")
    p.add_argument("--total-tokens", type=int, default=20_000, help="单题 token 预算上限")
    p.add_argument("--ctx-tokens", type=int, default=1_200, help="可注入前提的 token 预算")
    p.add_argument("--library", default=None,
                   help="库文件路径（默认 experiments/library.jsonl；传 none 表示不用库）")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="求解端点")
    p.add_argument("--imports", default=None,
                   help="验证环境的 import 列表（默认按有没有库自动决定）")
    p.add_argument("--mathlib-endpoint", default=None,
                   help="外部前提检索端点（不给则第二层检索降级跳过）")
    p.add_argument("--no-cheap", action="store_true", help="关掉第 3 步廉价 tactic 兜底（消融）")
    p.add_argument("--out", default=None, help="结果写出路径（JSON）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    library_path = None
    if args.library and args.library.lower() not in ("none", "-"):
        library_path = Path(args.library)
    elif args.library is None and DEFAULT_LIBRARY.exists():
        library_path = DEFAULT_LIBRARY
    has_library = bool(library_path and library_path.exists()
                       and library_path.read_text(encoding="utf-8").strip())
    imports = args.imports or ("Mathlib,SgsLean.GeneratedLibrary" if has_library else "Mathlib")

    prover = Prover(
        endpoint=args.endpoint,
        library_path=library_path,
        imports=imports,
        mathlib_endpoint=args.mathlib_endpoint,
    )
    budget = Budget(
        total_tokens=args.total_tokens,
        k=args.k,
        repair_rounds=args.repair_rounds,
        ctx_lemma_tokens=args.ctx_tokens,
    )
    if args.no_cheap:
        # 消融开关：把兜底清单换成"不试任何 tactic"。
        # 走 `cheapBatches` 空数组这条路而不是另立一套流程，是为了让**主线只有一个**——
        # 消融改的是清单，不是代码路径。
        import sgsr.pipeline.prover as prover_module  # noqa: PLC0415
        prover_module.CHEAP_DISABLED = True

    if args.file:
        parsed = prover.parse_input(lean_file=args.file)
        if parsed.get("error"):
            record = {"solved": False, "path": "parse_error", "stmt": "",
                      "notes": [f"parse_error: {parsed.get('detail')}"], "origin": parsed["origin"]}
        else:
            result = prover.prove(parsed["stmt"], budget=budget)
            record = result.to_dict()
            record["origin"] = parsed["origin"]
    else:
        result = prover.prove(args.statement, budget=budget)
        record = result.to_dict()
        record["origin"] = "string"

    record["config"] = {
        "endpoint": args.endpoint,
        "imports": imports,
        "k": args.k,
        "repair_rounds": args.repair_rounds,
        "library": str(library_path) if library_path else None,
        "library_size": len(prover.library),
        "cheap_enabled": not args.no_cheap,
        "mathlib_endpoint": args.mathlib_endpoint,
    }
    text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"[prove] 结果写入 {out_path}")
    print(text)
    return 0 if record.get("solved") else 1


if __name__ == "__main__":
    raise SystemExit(main())
