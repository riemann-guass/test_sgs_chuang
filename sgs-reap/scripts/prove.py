"""证明器命令行入口（P1，待实现）。

用法见 `docs/SG-Lean思路文档第二版.pdf` 9.1 节的验收命令：

    python scripts/prove.py --statement "forall (n : Nat), n + 0 = n" --k 4
    python scripts/prove.py --file tests/examples/t1.lean --k 4

输出一个 RunRecord（字段见规格文档附录 A），其中 `proof` 一定通过了内核终检。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SG-Lean 证明器：一条命题进，一篇过内核的证明出")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--statement", help="闭式命题字符串")
    src.add_argument("--file", help="含 theorem/example ... := by sorry 的 .lean 文件")
    p.add_argument("--k", type=int, default=4, help="采样篇数")
    p.add_argument("--library", default=None, help="库文件路径（默认 experiments/library.jsonl）")
    p.add_argument("--endpoint", default="http://127.0.0.1:8770/solve", help="求解端点")
    p.add_argument("--out", default=None, help="结果写出路径（JSON）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(f"P1：见 docs/SG-Lean思路文档第二版.pdf 第 3 节（args={args})")


if __name__ == "__main__":
    raise SystemExit(main())
