"""把 miniF2F（Lean4 移植版）转成我们的闭式语句数据（P4 准备）。

源：`data/raw/miniF2F-lean4/MiniF2F/{Valid,Test}/<problem>.lean`，一题一文件，形如

    theorem aime_1983_p1 (x y z w : ℕ) (ht : 1 < x ∧ …) : Real.log w / Real.log z = 60 := by sorry

转换规则（机械、可复核）：

1. 取 `theorem` 到第一个 `:=` 之间的部分，去掉定理名；
2. 在**第一个顶层 `:`** 切一刀：左边是绑定组（变量 + 假设），右边是结论
   （切分函数与 `sgsr/data/lean_parse.py` **共用同一份实现**，不各写一套括号计数）；
3. 绑定组原样放进 `∀`：`∀ (x y z w : ℕ) (ht : …), <结论>`。
   保留 `(...)`/`{...}`/`[...]` 的形态，不把假设改写成 `→`——两者在我们的语境下等价，
   但不改写能避免"依赖型假设"被拆错，也让 D/T 的题目文本保持稳定；
4. 语句**必须过服务端 `Gate.check`**（能 elaborate 成命题）才写进 JSONL，
   没过的记进报告（附原因），绝不静默丢弃。

## 判定环境必须有 `open`（2026-09-22 修）

miniF2F 的题面假定文件头的 `open BigOperators Real Nat Topology Rat` 生效——
`π`、`∑` 这类记法都依赖它。以前的判定片段没有这些 `open`（题面文本里也没有），
于是十几道题被判 `unknown_identifier π` / `parse_error` 而整题丢失：
**那是判定环境缺名字空间，不是数学错**。服务端片段的 Mathlib 模式现在会带上这些
`open`（见 `SgsLean/Server.lean` 的 `opensPatch`），本工具不再需要额外处理。

用法：

    python tools\\minif2f_to_jsonl.py --check          # 转换 + 过 Gate + 写 data/minif2f_*.jsonl
    python tools\\minif2f_to_jsonl.py                  # 只转换并打印统计（不调 Lean）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgsr.data import find_top_level_colon, strip_prelude  # noqa: E402
from sgsr.lean import LeanServer, budget_for_jobs  # noqa: E402

RAW = ROOT / "data" / "raw" / "miniF2F-lean4" / "MiniF2F"
DATA = ROOT / "data"
RESULTS = ROOT / "experiments" / "results"


def split_top_level_colon(text: str) -> tuple[str, str] | None:
    """在第一个顶层 `:` 处切分（与 `lean_parse` 共用实现）。"""
    idx = find_top_level_colon(text)
    return None if idx == -1 else (text[:idx], text[idx + 1:])


def convert(source: str) -> str | None:
    """把一份 miniF2F 文件转成闭式语句；解析失败返回 None。"""
    # 先去掉块注释/行注释：题面里夹注释时，`re.sub(r"\s+", " ")` 会把注释后面的
    # 内容一路吃掉，产生"凭空冒出来的语法错"（这类题以前被静默算作 parse_error）。
    text = strip_prelude(source)
    match = re.search(r"(?m)^theorem\s+([A-Za-z0-9_'.]+)\s*(.*?):=\s*by", text, flags=re.DOTALL)
    if not match:
        return None
    body = match.group(2)
    split = split_top_level_colon(body)
    if split is None:
        # 没有绑定组：整段就是结论
        statement = re.sub(r"\s+", " ", re.sub(r"[\r\n]+", " ", body)).strip()
        return statement or None
    binders, conclusion = split
    def flatten(chunk: str) -> str:
        # 折行成空格：Lean 不在乎换行，但**不能**在折叠前留下行尾 `--` 注释
        # （那会把后文整段注释掉）。`strip_prelude` 已经把注释清掉了。
        return re.sub(r"\s+", " ", chunk).strip()

    binders = flatten(binders)
    conclusion = flatten(conclusion)
    if not conclusion:
        return None
    if not binders:
        return conclusion
    return f"∀ {binders}, {conclusion}"


def run_server(requests: list[dict], timeout: float = 5400.0) -> dict[str, dict]:
    """一次批处理过 `Gate.check`。

    走 `LeanServer`（常驻客户端 + 每实例独立工作目录 + 按批大小给心跳）而不是
    自己 `subprocess.run`：旧写法固定用默认预算，124 条一批时批次尾部会集体
    报 `exception`（心跳按 command 累计），而被判为"这些题面不合法"。
    """
    with LeanServer(imports=os.environ.get("SGSLEAN_IMPORTS", "Mathlib"),
                    heartbeats=budget_for_jobs(len(requests)),
                    stderr_path=RESULTS / "minif2f_stderr.log") as server:
        return server.batch(requests, timeout=timeout)


def collect(split: str) -> list[dict]:
    folder = RAW / ("Valid" if split == "valid" else "Test")
    rows: list[dict] = []
    for path in sorted(folder.glob("*.lean")):
        statement = convert(path.read_text(encoding="utf-8"))
        if statement is None:
            rows.append({"id": path.stem, "statement": None, "parse": "failed"})
        else:
            rows.append({"id": path.stem, "statement": statement, "parse": "ok"})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="miniF2F -> 闭式语句 JSONL")
    parser.add_argument("--check", action="store_true", help="过服务端 Gate.check 后再写文件")
    parser.add_argument("--chunk", type=int, default=0,
                        help="每批多少条（0 = 一次全喂；判定的心跳按 command 复位，"
                             "分批只会白付 Mathlib 导入）")
    args = parser.parse_args()
    os.environ.setdefault("SGSLEAN_IMPORTS", "Mathlib")

    report: dict = {"splits": {}, "chunk": args.chunk}
    for split in ("valid", "test"):
        rows = collect(split)
        parsed = [r for r in rows if r["parse"] == "ok"]
        print(f"[minif2f] {split}: {len(rows)} 题，解析成功 {len(parsed)}")
        accepted: list[dict] = []
        rejected: list[dict] = []
        if args.check:
            step = args.chunk if args.chunk > 0 else max(1, len(parsed))
            for start in range(0, len(parsed), step):
                chunk = parsed[start : start + step]
                responses = run_server(
                    [{"id": r["id"], "cmd": "check", "stmt": r["statement"]} for r in chunk]
                )
                for row in chunk:
                    result = (responses.get(row["id"]) or {}).get("result") or {}
                    if result.get("ok") is True:
                        accepted.append(
                            {"id": row["id"], "statement": row["statement"],
                             "source": f"miniF2F/{split}", "split": split}
                        )
                    else:
                        rejected.append(
                            {"id": row["id"], "reason": result.get("reason"),
                             "detail": str(result.get("detail"))[:200]}
                        )
                print(
                    f"[minif2f] {split} 批 {start // step + 1}: "
                    f"已过 {len(accepted)} / 拒 {len(rejected)}"
                )
            out = DATA / f"minif2f_{split}.jsonl"
            with out.open("w", encoding="utf-8") as handle:
                for row in accepted:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[minif2f] 写入 {out}（{len(accepted)} 条）")
        report["splits"][split] = {
            "files": len(rows),
            "parsed": len(parsed),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "rejected_rows": rejected[:30],
        }

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "minif2f_import.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
