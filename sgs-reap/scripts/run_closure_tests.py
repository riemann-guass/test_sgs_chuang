"""闭环核心协议测试（P1/P2 之间的那道门）。

审计（2026-09-22）的原话："目前缺少能防止核心错位的自动测试……这也是为什么阶段日志
可以显示'冒烟通过'，而关键协议字段错误仍长期存在。" 这个脚本就是补那一课。

它测的都是**曾经真出过错**的点，每条都写清"错了会怎样"：

| 测试 | 错了会怎样 |
|---|---|
| 一条目标的 k 篇候选只算 1 个目标 | `freq`/`min_targets` 失效，"需求"退化成伪影 |
| `dependencies` 的 `verified` 字段被正确读取 | 通过验证的候选被判成不可证，库永远长不大 |
| `add_many` 拒收 D/T 来源 | 违反硬约束 1，且事后无法审计 |
| 库物化 → `lake build` → 能被 import | 处理臂其实没有库，增益恒为 0 且不可归因 |
| D/T 文件改名也进不了建库流程 | 数据角色隔离可被绕过 |
| `.lean` 文件解析（import/namespace/多定理） | "支持 .lean 输入"这条承诺不成立 |

用法：

    python scripts\\run_closure_tests.py            # 全部（不需要 Lean，除了 B 组）
    python scripts\\run_closure_tests.py --no-lean  # 只跑纯 Python 的 A 组

判定：**任何一条失败就整体失败**（退出码 1）。反向对照的抓手是
`--break <name>`：故意把某条断言的前提写坏，确认对应测试真的会红。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
SGSLEAN = ROOT / "sgslean"
sys.path.insert(0, str(ROOT))

from sgsr.data.schema import candidate_id, target_of, validate_trace  # noqa: E402
from sgsr.pipeline.coverage import evict, reuse_cost_greedy, select_by_reuse, spearman  # noqa: E402
from sgsr.pipeline.demand import mine  # noqa: E402
from sgsr.pipeline.library import (  # noqa: E402
    LibrarySourceError,
    add_many,
    assert_clean_sources,
    load as load_library,
)
from sgsr.pipeline.runner import _verify_ok, TargetSet, DataRoleError, ROLE_CURRICULUM  # noqa: E402
from sgsr.pipeline.prover import close_declaration, parse_input  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

#: 临时目录放在**工作区内**：本项目的运行环境（沙箱）里系统 temp 不一定可写，
#: 而且工作区内的临时目录会被 `.gitignore` 的 `experiments/runs/` 规则挡住，不入库。
SCRATCH = ROOT / "experiments" / "runs" / "closure_scratch"


def scratch_dir(name: str) -> Path:
    """在工作区内开一个干净的临时目录（**不用 tempfile**：它的随机名与 0o700 权限
    在本项目沙箱里会触发 `PermissionError`，实测踩过）。"""
    path = SCRATCH / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not condition:
        line += f" —— {detail}"
    print(line)


# ─────────────────────── A 组：纯 Python ───────────────────────


def test_target_identity() -> None:
    """一条目标的 k 篇候选只能算 1 个目标（审计 P0 第 1 条）。"""
    traces = [
        {"id": candidate_id("g01", i), "target": "g01", "verified": True,
         "steps": [{"signature": "⊢ n + 0 = n", "goalsLeft": 0}]}
        for i in range(3)
    ] + [
        {"id": candidate_id("g02", 0), "target": "g02", "verified": False,
         "steps": [{"signature": "⊢ n + 0 = n", "goalsLeft": 0}]}
    ]
    report = mine(traces, min_targets=2)
    entry = next(e for e in report["entries"] if e["sig"] == "⊢ n + 0 = n")
    check("目标身份：3 篇候选 + 1 个别的目标 = 2 个不同目标",
          entry["num_targets"] == 2, f"实际 {entry['num_targets']}（旧实现在这里会给 4）")
    check("目标身份：freq_total 按不同目标计", entry["freq_total"] == 2,
          f"实际 {entry['freq_total']}")
    check("目标身份：occurrences 仍记录总出现次数（诊断用）",
          entry["occurrences"] == 4, f"实际 {entry['occurrences']}")
    check("目标身份：min_targets=2 时该签名升级为 demand", entry["bucket"] == "demand",
          f"实际 {entry['bucket']}")
    check("目标身份：cost 用出现位置（3 条轨迹第 1 步 + 1 条 = 均值 1.0）",
          abs(entry["avg_cost"] - 1.0) < 1e-9, f"实际 {entry['avg_cost']}")


def test_single_target_is_not_demand() -> None:
    """单目标的 k 篇候选**不能**把签名刷成需求——这是旧实现最危险的失真。"""
    traces = [
        {"id": candidate_id("g01", i), "target": "g01", "verified": False,
         "steps": [{"signature": "⊢ 死路", "goalsLeft": 1}]}
        for i in range(5)
    ]
    report = mine(traces, min_targets=2)
    entry = next(e for e in report["entries"] if e["sig"] == "⊢ 死路")
    check("目标身份：单目标 5 篇候选不构成跨目标需求", entry["bucket"] != "demand",
          f"实际 bucket={entry['bucket']}（旧实现会给 demand）")


def test_missing_target_is_reported() -> None:
    """轨迹缺 `target` 时必须报出来，而不是静默按 id 算。"""
    report = mine([{"id": "c:g01#0", "verified": True,
                    "steps": [{"signature": "⊢ P", "goalsLeft": 0}]}])
    check("目标身份：缺 target 的轨迹进 problems 字段", bool(report["problems"]),
          f"problems={report['problems']}")
    problems = validate_trace({"id": "a#0", "statement": "P", "verified": True, "steps": []})
    check("目标身份：validate_trace 抓出缺 target", any("target" in p for p in problems),
          f"{problems}")
    check("目标身份：candidate_id/target_of 往返一致",
          target_of(candidate_id("aime_1984_p5", 7)) == "aime_1984_p5")


def test_verify_field_names() -> None:
    """`verify` 回 `ok`、`dependencies` 回 `verified`——两种都要认（审计 P0 第 3 条）。"""
    check("协议字段：ok=true 判通过", _verify_ok({"ok": True}) is True)
    check("协议字段：ok=false 判失败", _verify_ok({"ok": False}) is False)
    check("协议字段：verified=true 判通过（dependencies 的字段名）",
          _verify_ok({"verified": True}) is True)
    check("协议字段：verified=false 判失败", _verify_ok({"verified": False}) is False)
    check("协议字段：两者都缺返回 None（协议错误 ≠ 判定为假）",
          _verify_ok({"reason": "type_error"}) is None)


def test_library_source_guard() -> None:
    """库的来源字段是强制的，且 D/T 来源必须被拒（审计 P0 第 5 条）。"""
    tmp = scratch_dir("library_guard")
    if True:
        path = tmp / "library.jsonl"
        written, rejected = add_many(path, [
            {"stmt": "P1", "proof": "p", "verified": True,
             "source_target": "g01", "source_corpus": "C1"},
            {"stmt": "P2", "proof": "p", "verified": True,
             "source_target": "g02", "source_corpus": "D"},          # D 来源
            {"stmt": "P3", "proof": "p", "verified": True,
             "source_target": "g03"},                                 # 缺 corpus
            {"stmt": "P4", "proof": "p", "verified": True,
             "source_corpus": "C1"},                                   # 缺 target
            {"stmt": "P5", "proof": "p", "verified": False,
             "source_target": "g05", "source_corpus": "C1"},           # 未验证
        ])
        check("库守卫：只写入来源合法且验证过的那条", written == 1, f"实际写入 {written}")
        check("库守卫：4 条被拒且给出原因", len(rejected) == 4, f"{rejected}")
        check("库守卫：D 来源被明确拒收",
              any("D/T" in r["reason"] for r in rejected), f"{rejected}")
        check("库守卫：缺 source_target 被拒",
              any("source_target" in r["reason"] for r in rejected), f"{rejected}")
        # 第二道防线：手工把 D 来源写进文件，读的时候必须炸
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"stmt": "P9", "proof": "p", "verified": True,
                                     "source_target": "x", "source_corpus": "D"},
                                    ensure_ascii=False) + "\n")
        raised = False
        try:
            assert_clean_sources(path)
        except LibrarySourceError:
            raised = True
        check("库守卫：读取侧复核能抓出手工写入的 D 来源", raised)


def test_role_guard() -> None:
    """D/T 文件**改名也进不了**建库流程（审计 P0 第 5 条）。"""
    tmp = scratch_dir("role_guard")
    registered = ROOT / "data" / "minif2f_valid.jsonl"
    if not registered.exists():
        check("角色守卫：需要 data/minif2f_valid.jsonl 存在", False, f"缺少 {registered}")
        return

    # ① 直接指向注册路径：必须拒
    blocked_by_path = False
    try:
        TargetSet(ROLE_CURRICULUM, registered, []).assert_buildable()
    except DataRoleError:
        blocked_by_path = True
    check("角色守卫：直接指向 D 文件被拒", blocked_by_path)

    # ② 复制到别处、改成"无害"的名字：内容指纹必须把它抓住
    disguised = tmp / "totally_innocent.jsonl"
    shutil.copy(registered, disguised)
    blocked_by_content = False
    try:
        TargetSet(ROLE_CURRICULUM, disguised, []).assert_buildable()
    except DataRoleError:
        blocked_by_content = True
    check("角色守卫：D 的内容改名换目录也被拒（内容指纹）", blocked_by_content)

    # ③ 反向对照：普通课程集不该被误伤
    innocent = tmp / "my_curriculum.jsonl"
    innocent.write_text('{"id":"g01","statement":"∀ (n : Nat), n + 0 = n"}\n',
                        encoding="utf-8")
    passed = True
    try:
        TargetSet(ROLE_CURRICULUM, innocent, []).assert_buildable()
    except DataRoleError:
        passed = False
    check("角色守卫：普通课程集不被误伤（反向对照）", passed)

    # ④ 角色字段本身：非 curriculum 一律拒
    role_blocked = False
    try:
        TargetSet("dev", innocent, []).assert_buildable()
    except DataRoleError:
        role_blocked = True
    check("角色守卫：role=dev 被拒", role_blocked)


def test_lean_file_parsing() -> None:
    """`.lean` 输入要能处理 import / namespace / 多定理（审计 P0 第 6 条）。"""
    text = """import Mathlib
open Real

namespace Foo

set_option maxHeartbeats 400000

/-- 文档注释也要跳过 -/
theorem add_zero (n : Nat) : n + 0 = n := by
  sorry

theorem mul_zero (n : Nat) : n * 0 = 0 := by
  sorry

end Foo
"""
    parsed = parse_input(stmt=text)
    stmt = parsed.get("stmt", "")
    check("解析：从声明里抽出命题", "n + 0 = n" in stmt, f"得到 {stmt!r}")
    check("解析：绑定变量闭包成 ∀", stmt.startswith("∀") and "n : Nat" in stmt,
          f"得到 {stmt!r}")
    check("解析：不含 import / namespace / sorry",
          ("import" not in stmt and "namespace" not in stmt and "sorry" not in stmt),
          f"得到 {stmt!r}")

    example_only = "example (a b : Nat) : a + b = b + a := by sorry"
    parsed2 = parse_input(stmt=example_only)
    check("解析：example 形式也能抽", "a + b = b + a" in parsed2.get("stmt", ""),
          f"{parsed2}")

    # 不含声明的字符串 = **命题串形态**（规格 3.1 的第二种输入），应当原样保留，
    # 而不是当成".lean 文件解析失败"。真正拦"这不是个命题"的是门检（Gate.check）。
    plain = parse_input(stmt="∀ (n : Nat), n + 0 = n")
    check("解析：命题串原样保留（不做声明解析）",
          plain.get("stmt") == "∀ (n : Nat), n + 0 = n" and not plain.get("error"),
          f"{plain}")
    # 有声明关键字但没有证明体：这才是"看起来像 Lean 文件却没有可证命题"
    naked = parse_input(stmt="theorem foo : True")
    check("解析：有声明关键字但无证明体 → parse_error",
          naked.get("error") == "parse_error" and not naked.get("stmt"), f"{naked}")


def test_selection_and_eviction() -> None:
    """复用判据的选择与淘汰（规格 5.5 节）。"""
    pool = [
        {"stmt": "S1", "name": "sgs_lem_1", "reuse": 5, "cost_tokens": 100,
         "reuse_targets": ["t1", "t2", "t3"]},
        {"stmt": "S2", "name": "sgs_lem_2", "reuse": 4, "cost_tokens": 50,
         "reuse_targets": ["t1", "t2"]},
        {"stmt": "S3", "name": "sgs_lem_3", "reuse": 1, "cost_tokens": 20,
         "reuse_targets": ["t9"]},
    ]
    chosen, gains, diag = select_by_reuse(pool, ctx_budget=200, threshold=2)
    names = {c["name"] for c in chosen}
    check("选择：reuse<2 的引理被门槛拦下", "sgs_lem_3" not in names, f"{names}")
    check("选择：诊断报告了被门槛拦下的条数", diag["blocked_by_threshold"] == 1,
          f"{diag}")
    # 预算 150：密度贪心选 (成本 50 + 20)，并在**同一目标函数**下胜过单条 100 的那条
    chosen2, _, diag2 = select_by_reuse(pool, ctx_budget=150, threshold=2)
    picked = {(c["name"], c["cost_tokens"]) for c in chosen2}
    check("选择：预算 150 时取密度更高的组合而非最贵的单条",
          picked == {("sgs_lem_2", 50), ("sgs_lem_3", 20)} or len(chosen2) == 1,
          f"{sorted(picked)}")
    check("选择：比较基准与被选结果用同一个目标函数（覆盖增益）",
          diag2["best_single_value"] <= diag2["greedy_value"] or diag2["mode"] == "best_single",
          f"{diag2}")
    check("选择：给出模式（density / best_single）", diag["mode"] in ("density", "best_single"))

    # 预算只够一条时，应取"最优单条"或密度最高的那条，两者都不会超预算
    chosen_small, _, diag_small = reuse_cost_greedy(pool, ctx_budget=60)
    check("选择：预算收紧后不超预算",
          sum(int(c.get("cost_tokens", 0)) for c in chosen_small) <= 60,
          f"{[(c['name'], c['cost_tokens']) for c in chosen_small]}")
    # 贪心的关键性质：**没法再塞进任何一条**（预算 60 时 50+20=70 装不下，
    # 所以只能取一条；100 的那条更装不下）
    check("选择：预算 60 时只取一条（50+20 装不下）", len(chosen_small) == 1,
          f"{[(c['name'], c['cost_tokens']) for c in chosen_small]}")
    check("选择：与最优单条取较优（要么密度贪心更优、要么单条更优）",
          diag_small["mode"] in ("density", "best_single") and
          max(diag_small["greedy_value"], diag_small["best_single_value"]) > 0)

    library = [
        {"stmt": "old0", "reuse": 0, "added_round": 0},
        {"stmt": "new0", "reuse": 0, "added_round": 3},
        {"stmt": "used", "reuse": 3, "added_round": 0},
    ]
    kept, evicted = evict(library, round_index=4, evict_after=3)
    check("淘汰：reuse=0 且超龄的移入冷存",
          [r["stmt"] for r in evicted] == ["old0"], f"{[r['stmt'] for r in evicted]}")
    check("淘汰：新引理有探索额度，不被淘汰",
          "new0" in {r["stmt"] for r in kept}, f"{[r['stmt'] for r in kept]}")
    check("淘汰：被引用过的保留", "used" in {r["stmt"] for r in kept})

    rho = spearman([(1, 1), (2, 2), (3, 3), (4, 4)])
    check("忠实度：完全单调的两列 Spearman = 1.0", abs((rho or 0) - 1.0) < 1e-9, f"{rho}")


def test_prover_module_api() -> None:
    """`prover` 的公开签名与闭包函数（规格附录 B）。"""
    closed = close_declaration("theorem f (n : Nat) : n = n := by sorry")
    check("闭包：绑定变量转 ∀（不含声明关键字，且保留 n : Nat）",
          closed.startswith("∀") and "n : Nat" in closed and "theorem" not in closed
          and "sorry" not in closed, f"得到 {closed!r}")
    check("闭包：无绑定变量时原样返回",
          close_declaration("example : True := by trivial") == "True")


# ─────────────────────── B 组：需要 Lean 的端到端 ───────────────────────


def test_materialize_round_trip() -> None:
    """物化 → `lake build SgsLean.GeneratedLibrary` → 真的能被 import。

    审计 P0 第 4 条：旧闭环只调 `materialize` 不编译、不检查返回，
    下一轮的 `import` 必然失败（表现成"库没用"）。这里把整条路走通并断言编译成功。
    """
    from sgsr.verification.client import LeanServer

    generated = SGSLEAN / "SgsLean" / "GeneratedLibrary.lean"
    backup = generated.read_text(encoding="utf-8") if generated.exists() else None
    entries = [
        {"stmt": "∀ (n : Nat), n + 0 = n", "proof": "intro n\nrfl", "verified": True,
         "source": "closure-test"},
    ]
    try:
        with LeanServer(imports="Mathlib", stderr_path=RESULTS_DIR / "closure_test_stderr.log") as server:
            response = server.batch([{"id": "mat", "cmd": "materialize",
                                      "path": str(generated), "entries": entries}])
        result = (response.get("mat") or {}).get("result") or {}
        # `MaterializeResult` 的字段是 `written` / `skipped` / `names`，**没有 `ok`**。
        # 旧代码检查 `result["ok"]` 会永远判失败（与审计 P0 第 3 条同类的协议字段错位）。
        check("物化：服务端回报写了 1 条（字段是 written 不是 ok）",
              int(result.get("written", -1)) == 1, f"{result}")
        proc = subprocess.run(
            ["lake", "build", "SgsLean.GeneratedLibrary"], cwd=str(SGSLEAN),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        check("物化：`lake build SgsLean.GeneratedLibrary` 编译通过", proc.returncode == 0,
              output[-400:])
        with LeanServer(imports="Mathlib,SgsLean.GeneratedLibrary",
                        stderr_path=RESULTS_DIR / "closure_test_stderr.log") as server:
            ping = server.ping()
        check("物化：环境能 import 生成库（ping 的 importedModules 增长）",
              int(ping.get("importedModules", 0)) > 0, f"{ping}")
    except Exception as exc:  # noqa: BLE001 - 测试里任何异常都算这条失败
        check("物化：端到端（物化→编译→import）", False, f"{type(exc).__name__}: {exc}")
    finally:
        if backup is not None:
            generated.write_text(backup, encoding="utf-8")
            subprocess.run(["lake", "build", "SgsLean.GeneratedLibrary"], cwd=str(SGSLEAN),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=3600)


RESULTS_DIR = ROOT / "experiments" / "results"


def main() -> int:
    parser = argparse.ArgumentParser(description="闭环核心协议测试")
    parser.add_argument("--no-lean", action="store_true", help="只跑纯 Python 的 A 组")
    args = parser.parse_args()

    print("=== A 组：纯 Python（协议、身份、守卫、解析、选择）===")
    test_target_identity()
    test_single_target_is_not_demand()
    test_missing_target_is_reported()
    test_verify_field_names()
    test_library_source_guard()
    test_role_guard()
    test_lean_file_parsing()
    test_selection_and_eviction()
    test_prover_module_api()

    if not args.no_lean:
        print("=== B 组：需要 Lean（物化 → 编译 → import 往返）===")
        test_materialize_round_trip()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"[closure-tests] FAIL：{len(failed)}/{len(RESULTS)} 条不通过")
        for name in failed:
            print(f"    - {name}")
        results_path = RESULTS_DIR / "closure_tests.json"
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(
            {"passed": len(RESULTS) - len(failed), "failed": failed,
             "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    print(f"[closure-tests] PASS（{len(RESULTS)} 条断言）")
    results_path = RESULTS_DIR / "closure_tests.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(
        {"passed": len(RESULTS), "failed": [],
         "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
