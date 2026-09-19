"""把 miniF2F（Lean4 移植版）转成我们的闭式语句数据（P4 准备）。

源：`data/raw/miniF2F-lean4/MiniF2F/{Valid,Test}/<problem>.lean`，一题一文件，形如

    theorem aime_1983_p1 (x y z w : ℕ) (ht : 1 < x ∧ …) : Real.log w / Real.log z = 60 := by sorry

转换规则（机械、可复核）：

1. 取 `theorem` 到第一个 `:=` 之间的部分，去掉定理名；
2. 在**第一个顶层 `:`** 切一刀：左边是绑定组（变量 + 假设），右边是结论；
3. 绑定组原样放进 `∀`：`∀ (x y z w : ℕ) (ht : …), <结论>`。
   保留 `(...)`/`{...}`/`[...]` 的形态，不把假设改写成 `→`——两者在我们的语境下等价，
   但不改写能避免"依赖型假设"被拆错；
4. 语句**必须过服务端 `Gate.check`**（能 elaborate 成命题）才写进 JSONL，
   没过的记进报告（附原因），绝不静默丢弃。

用法：

    python tools\\minif2f_to_jsonl.py --check          # 转换 + 过 Gate + 写 data/minif2f_*.jsonl
    python tools\\minif2f_to_jsonl.py                  # 只转换并打印统计（不调 Lean）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "miniF2F-lean4" / "MiniF2F"
DATA = ROOT / "data"
SGSLEAN = ROOT / "sgslean"
RESULTS = ROOT / "experiments" / "results"
LAKE = os.environ.get("LAKE", "lake")
OPEN = {"(": ")", "{": "}", "[": "]", "⟨": "⟩"}
CLOSE = {v: k for k, v in OPEN.items()}


def split_top_level_colon(text: str) -> tuple[str, str] | None:
    """在第一个顶层 `:` 处切分（括号/花括号/方括号外）。"""
    depth = 0
    for idx, ch in enumerate(text):
        if ch in OPEN:
            depth += 1
        elif ch in CLOSE:
            depth -= 1
        elif ch == ":" and depth == 0:
            return text[:idx], text[idx + 1 :]
    return None


def convert(source: str) -> str | None:
    """把一份 miniF2F 文件转成闭式语句；解析失败返回 None。"""
    text = source
    match = re.search(r"(?m)^theorem\s+([A-Za-z0-9_'.]+)\s*(.*?):=\s*by", text, flags=re.DOTALL)
    if not match:
        return None
    body = match.group(2)
    split = split_top_level_colon(body)
    if split is None:
        # 没有绑定组：整段就是结论
        statement = re.sub(r"\s+", " ", body).strip()
        return statement or None
    binders, conclusion = split
    binders = re.sub(r"\s+", " ", binders).strip()
    conclusion = re.sub(r"\s+", " ", conclusion).strip()
    if not conclusion:
        return None
    if not binders:
        return conclusion
    return f"∀ {binders}, {conclusion}"


def run_server(requests: list[dict], timeout: float = 5400.0) -> dict[str, dict]:
    lines = [json.dumps(r, ensure_ascii=False) for r in requests]
    lines.append(json.dumps({"id": "__flush__", "cmd": "flush"}))
    proc = subprocess.run(
        [LAKE, "exe", "sgslean-server"],
        cwd=str(SGSLEAN),
        input=("\n".join(lines) + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    out: dict[str, dict] = {}
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        item = json.loads(line)
        out[str(item.get("id"))] = item
    return out


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
    parser.add_argument("--chunk", type=int, default=125)
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
            for start in range(0, len(parsed), args.chunk):
                chunk = parsed[start : start + args.chunk]
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
                    f"[minif2f] {split} 批 {start // args.chunk + 1}: "
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
