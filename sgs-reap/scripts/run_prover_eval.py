"""在给定数据集上批量评测证明器（P1，待实现）。

验收命令见 `docs/SG-Lean思路文档第二版.pdf` 9.1 节：

    python scripts/run_prover_eval.py --set D --k 4 --out experiments/results/p1_dev.json

必须同时报出四个量（规格 1.2 与 8.2 节）：
* 目标级 pass@k；
* CostPerSolved = 总 token / 解出题数；
* 兜底命中率（零模型调用那部分）；
* 按领域的通过率分桶。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="批量评测证明器")
    p.add_argument("--set", dest="dataset", required=True, help="数据集文件或已注册的集合名（C/D/T）")
    p.add_argument("--k", type=int, default=4, help="采样篇数")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 表示全部）")
    p.add_argument("--out", required=True, help="报告写出路径")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(f"P1：见 docs/SG-Lean思路文档第二版.pdf 9.1 节（args={args}）")


if __name__ == "__main__":
    raise SystemExit(main())
