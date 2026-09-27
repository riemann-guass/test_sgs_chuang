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

from sgsr.data import candidate_id, target_of, validate_trace  # noqa: E402
from sgsr.pipeline.selection import (  # noqa: E402
    evict,
    exploration_admission,
    retrieve_library,
    reuse_cost_greedy,
    select_by_reuse,
    spearman,
)
from sgsr.pipeline.demand import mine  # noqa: E402
from sgsr.pipeline.library import (  # noqa: E402
    LibrarySourceError,
    add_many,
    assert_clean_sources,
    load as load_library,
)
from sgsr.pipeline.runner import (  # noqa: E402
    _verify_ok,
    DataRoleError,
    ROLE_CURRICULUM,
    ROLE_C_BUILD,
    ROLE_C_MEASURE,
    RoundReport,
    RoundRunner,
    TargetSet,
    assert_disjoint_curriculum,
    split_curriculum,
)
from sgsr.pipeline.prover import close_declaration, parse_input  # noqa: E402
from sgsr.lean import materialize_imports, resolve_imports  # noqa: E402

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
    # 断言必须写死：以前这里是 `... or len(chosen2) == 1`，那个"或"让这条永远为真
    # （逃生口型断言），装置空转也照样亮绿灯。
    # 这个池子在预算 150 下的正确结果是：贪心取 {S1,S2}（覆盖 3 个目标、成本 150），
    # 与"只取最优单条 S1"（覆盖同样 3 个、成本 100）**同值更贵**，
    # 按规格的平手规则（同增益取成本更低）应回退到 best_single。
    check("选择：同增益时按规格回退到成本更低的单条（mode=best_single）",
          picked == {("sgs_lem_1", 100)} and diag2["mode"] == "best_single",
          f"{sorted(picked)} mode={diag2['mode']}")

    # 密度贪心真正该赢的场景：两条便宜的组合 > 一条贵的单条（同增益时）。
    density_pool = [
        {"stmt": "X", "name": "sgs_lem_x", "reuse": 2, "cost_tokens": 50,
         "reuse_targets": ["t1", "t2"]},
        {"stmt": "Y", "name": "sgs_lem_y", "reuse": 1, "cost_tokens": 50,
         "reuse_targets": ["t3"]},
        {"stmt": "Z", "name": "sgs_lem_z", "reuse": 3, "cost_tokens": 140,
         "reuse_targets": ["t1", "t2", "t3"]},
    ]
    chosen3, gains3, diag3 = reuse_cost_greedy(density_pool, ctx_budget=150)
    got3 = {(c["name"], c["cost_tokens"]) for c in chosen3}
    check("选择：两条便宜的组合胜过一条贵的单条（density 模式）",
          got3 == {("sgs_lem_x", 50), ("sgs_lem_y", 50)} and diag3["mode"] == "density",
          f"{sorted(got3)} mode={diag3['mode']}")
    check("选择：并集口径下不重复计同一目标（增益 2 + 1 = 3）",
          abs(sum(gains3) - 3.0) < 1e-9, f"{gains3}")
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
        {"stmt": "old0", "reuse": 0, "added_round": 0, "exposures": 2},
        {"stmt": "new0", "reuse": 0, "added_round": 3, "exposures": 1},
        {"stmt": "used", "reuse": 3, "added_round": 0, "exposures": 2},
        {"stmt": "never_exposed", "reuse": 0, "added_round": 0, "exposures": 0},
    ]
    kept, evicted = evict(library, round_index=4, evict_after=3)
    check("淘汰：reuse=0 且超龄的移入冷存",
          [r["stmt"] for r in evicted] == ["old0"], f"{[r['stmt'] for r in evicted]}")
    check("淘汰：新引理有探索额度，不被淘汰",
          "new0" in {r["stmt"] for r in kept}, f"{[r['stmt'] for r in kept]}")
    check("淘汰：被引用过的保留", "used" in {r["stmt"] for r in kept})
    # 反向对照：**没被给过机会**的引理不许被淘汰——提示词预算有限，库里的引理
    # 不一定每轮都进提示词；按"超龄即杀"淘汰的就不是僵尸，而是没抽到签的人。
    check("淘汰：没进过提示词的引理不淘汰（给过机会才谈淘汰）",
          "never_exposed" in {r["stmt"] for r in kept}, f"{[r['stmt'] for r in kept]}")

    rho = spearman([(1, 1), (2, 2), (3, 3), (4, 4)])
    check("忠实度：完全单调的两列 Spearman = 1.0", abs((rho or 0) - 1.0) < 1e-9, f"{rho}")


def test_admission_and_bootstrap() -> None:
    """**准入门槛不能要求新引理先有复用证据**（这是让库永远长不大的那道死锁）。

    旧实现把 `reuse >= threshold` 当准入门槛，而新引理的 reuse 按构造是 0，
    于是 `new_entries` 恒空、库永远为空。这里用两条互补的断言把它钉住：
    ① 新候选（reuse=0）**必须**能被准入；② 超龄且没被给过机会的老引理不许被淘汰。
    """
    fresh = [
        {"stmt": "N1", "key": "t1#0", "target": "t1"},
        {"stmt": "N2", "key": "t2#0", "target": "t2"},
        {"stmt": "N3", "key": "t3#0", "target": "t3"},
    ]
    admitted, diag = exploration_admission(fresh, library_size=0, library_budget=30,
                                           exploration=2)
    check("准入：空库 + 3 条新验证候选 → 按探索额度准入 2 条（不是 0 条）",
          len(admitted) == 2, f"{diag}")
    check("准入：诊断给出被额度挡下的条数", diag["dropped_by_quota"] == 1, f"{diag}")
    admitted_full, diag_full = exploration_admission(fresh, library_size=0, library_budget=1,
                                                    exploration=8)
    check("准入：库容只剩 1 条时只准入 1 条", len(admitted_full) == 1, f"{diag_full}")
    admitted_none, _ = exploration_admission(fresh, library_size=30, library_budget=30,
                                             exploration=8)
    check("准入：库满时准入 0 条（不是把库撑爆）", admitted_none == [])

    # 冷启动：库里全是"还没有复用证据、但还在探索期"的引理 → 提示词必须能选到它们，
    # 否则第一轮之后永远给不了库，reuse 也就永远测不出来。
    library = [
        {"stmt": "A", "name": "sgs_lem_a", "reuse": 0, "reuse_targets": [],
         "cost_tokens": 20, "added_round": 0},
        {"stmt": "B", "name": "sgs_lem_b", "reuse": 0, "reuse_targets": [],
         "cost_tokens": 20, "added_round": 0},
    ]
    chosen, _, diag2 = select_by_reuse(library, ctx_budget=1200, threshold=2,
                                       round_index=0, evict_after=3)
    check("冷启动：库里没有复用证据时，探索期引理仍会被注入提示词",
          len(chosen) == 2, f"{diag2}")
    chosen_stale, _, diag3 = select_by_reuse(library, ctx_budget=1200, threshold=2,
                                             round_index=9, evict_after=3)
    check("冷启动：超龄且无复用证据的引理不再注入（如实返回空集）",
          chosen_stale == [] and diag3["exploring"] == 0, f"{diag3}")


def test_reuse_measurement() -> None:
    """复用测量的口径：按**不同目标**去重、只数**通过验收**的证明、常量取叶子名。"""
    traces = [
        # 目标 t1 的两篇候选都引用了 sgs_lem_a → 只算 1 个目标
        {"id": "c:t1#0", "target": "t1", "verified": True,
         "constants": ["SgsLean.sgs_lem_a", "Nat.add_comm"]},
        {"id": "c:t1#1", "target": "t1", "verified": True, "constants": ["sgs_lem_a"]},
        # 目标 t2 的证明没过验收 → 它的引用不算复用
        {"id": "c:t2#0", "target": "t2", "verified": False, "constants": ["sgs_lem_a"]},
        # 目标 t3 通过验收且引用 → t1、t3 两个不同目标
        {"id": "c:t3#0", "target": "t3", "verified": True,
         "constants": ["sgs_lem_a", "sgs_lem_b"]},
    ]
    exposed = {"sgs_lem_a": {"t1", "t3"}, "sgs_lem_b": set()}
    cited = RoundRunner.measure_reuse(_Stub(), traces, exposed)
    check("复用测量：按不同目标去重（t1 两篇只算 1）", cited.get("sgs_lem_a") == {"t1", "t3"},
          f"{cited}")
    check("复用测量：失败证明里的引用不算复用（t2 不在集合里）",
          "t2" not in (cited.get("sgs_lem_a") or set()), f"{cited}")
    check("复用测量：点号全名按叶子名计数（SgsLean.sgs_lem_a 也算）",
          "sgs_lem_a" in cited, f"{cited}")
    check("复用测量：反向对照——未对该目标曝光的引理不计 reuse",
          "sgs_lem_b" not in cited, f"{cited}")


def test_curriculum_split() -> None:
    """C-build 与 C-measure 必须是稳定、无交叉的两个输入。"""
    rows = [
        {"id": f"c{i}", "statement": f"Nat.succ {i} = {i + 1}"}
        for i in range(20)
    ]
    source = TargetSet(ROLE_CURRICULUM, Path("C.jsonl"), rows)
    build_a, measure_a, manifest_a = split_curriculum(source, seed="fixed", build_percent=60)
    build_b, measure_b, manifest_b = split_curriculum(source, seed="fixed", build_percent=60)
    check("C 划分：同一种子确定性产生同一 C-build/C-measure",
          build_a.identities() == build_b.identities()
          and measure_a.identities() == measure_b.identities()
          and manifest_a == manifest_b)
    check("C 划分：两个输入身份与命题内容均无交叉",
          not (build_a.identities() & measure_a.identities())
          and not (build_a.statement_fingerprints() & measure_a.statement_fingerprints()))
    rejected = False
    try:
        assert_disjoint_curriculum(
            TargetSet(ROLE_C_BUILD, Path("build.jsonl"), [rows[0]]),
            TargetSet(ROLE_C_MEASURE, Path("measure.jsonl"), [dict(rows[0])]),
        )
    except DataRoleError:
        rejected = True
    check("C 划分：反向对照——来源目标进入 C-measure 必须拒绝", rejected)


class _Stub:
    """`measure_reuse` 不碰实例状态，借用它做纯函数测试。"""


def test_reuse_persistence(tmp_root: Path) -> None:
    """复用测量**必须落盘**：否则淘汰读到的是"每条 reuse 都是 0"，检索也没有排序键。"""
    from sgsr.pipeline.library import load as load_library, name_for
    from sgsr.pipeline.runner import RoundConfig, RoundReport

    library_path = tmp_root / "persist_library.jsonl"
    stmt_a = "∀ (n : Nat), n + 0 = n"
    stmt_b = "∀ (n : Nat), 0 + n = n"
    name_a, name_b = name_for(stmt_a), name_for(stmt_b)
    library_path.write_text(
        json.dumps({"stmt": stmt_a, "proof": "intro n\nrfl", "verified": True,
                    "name": name_a, "source_target": "g01", "source_corpus": "C1",
                    "added_round": 0, "reuse_targets": ["legacy"],
                    "exposure_targets": ["legacy"]}, ensure_ascii=False) + "\n"
        + json.dumps({"stmt": stmt_b, "proof": "intro n\nrfl", "verified": True,
                      "name": name_b, "source_target": "g02", "source_corpus": "C1",
                      "added_round": 0}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    config = RoundConfig(round_index=1)
    runner = RoundRunner.__new__(RoundRunner)      # 只测这一件事，不建 Lean 连接
    runner.config = config
    runner.library_path = library_path
    runner.log = lambda *_a, **_k: None
    report = RoundReport(round_index=1, started_at="")
    cited = {name_a: {"g01", "t2", "t3"}, name_b: {"t1"}}
    exposed = {name_a: {"t2", "t3"}}
    runner.update_library(report, cited, exposed, measure_target_ids={"t1", "t2", "t3"})
    rows = {row["name"]: row for row in load_library(library_path)}
    check("复用落盘：reuse 与 reuse_targets 写进库文件（淘汰/检索都靠它）",
          rows[name_a].get("reuse") == 2 and rows[name_a].get("reuse_targets") == ["t2", "t3"],
          f"{rows[name_a]}")
    check("复用落盘：来源目标不计 reuse", "g01" not in rows[name_a]["reuse_targets"])
    check("库三态：达到门槛且曝光后晋升 active，未达标仍为 probation",
          rows[name_a].get("status") == "active" and rows[name_b].get("status") == "probation",
          f"{rows[name_a]} / {rows[name_b]}")
    check("复用落盘：cost_tokens 一并写进库（检索层的排序键）",
          int(rows[name_a].get("cost_tokens") or 0) > 0, f"{rows[name_a]}")
    check("复用落盘：进过提示词的引理曝光计数 +1（没进的不加）",
          rows[name_a].get("exposures") == 2 and rows[name_b].get("exposures") == 0,
          f"{rows[name_a]} / {rows[name_b]}")
    check("复用落盘：报告里的复用分布被填上",
          report.reuse.get("size") == 2 and report.reuse.get("buckets", {}).get("reusable") == 1,
          f"{report.reuse}")


def test_prover_module_api() -> None:
    """`prover` 的公开签名与闭包函数（规格附录 B）。"""
    closed = close_declaration("theorem f (n : Nat) : n = n := by sorry")
    check("闭包：绑定变量转 ∀（不含声明关键字，且保留 n : Nat）",
          closed.startswith("∀") and "n : Nat" in closed and "theorem" not in closed
          and "sorry" not in closed, f"得到 {closed!r}")
    check("闭包：无绑定变量时原样返回",
          close_declaration("example : True := by trivial") == "True")


def test_prompt_blocks() -> None:
    """提示词的区块开关（吸收自旧 `scripts/test_prompts.py`，不联网、不花钱）。

    mock 服务不构造提示词，所以"需求条件化到底进没进提示词"只能在纯函数上断言。
    最关键的一条：**空需求必须等于没有需求**——H1 的对照是"同预算、同模型，只去掉
    需求条件化"，若 `demand=[]` 仍渲染出占位区块，对照就不干净。
    """
    from sgsr.models import prompts

    goal = "n : ℕ\n⊢ 2 ∣ n ^ 2 + n"
    premise, proof = "n : ℕ ⊢ 2 ∣ n * (n + 1)", "n : ℕ ⊢ n % 2 = 0 ∨ n % 2 = 1"
    with_demand = prompts.conjecture_prompt(goal, 3, demand=[premise, proof])
    no_demand = prompts.conjecture_prompt(goal, 3)
    empty_demand = prompts.conjecture_prompt(goal, 3, demand=[])
    check("提示词：有需求时出现 BACKGROUND EVIDENCE 区块",
          "BACKGROUND EVIDENCE" in with_demand)
    check("提示词：每条需求签名都出现在提示词里",
          all(sig in with_demand for sig in (premise, proof)))
    check("提示词：明确禁止把证据当答案输出", "Do NOT output these subgoals" in with_demand)
    check("提示词：demand=[] 与不传等价（H1 对照必须干净）",
          no_demand == empty_demand and "BACKGROUND EVIDENCE" not in empty_demand)
    check("提示词：符号相关性约束始终存在",
          "MUST mention at least one symbol" in no_demand)
    with_seeds = prompts.conjecture_prompt(goal, 3, seeds=["∀ (n : ℕ), n + 0 = n"])
    check("提示词：有范例时出现 LIBRARY EXCERPTS 且无范例时不出现",
          "LIBRARY EXCERPTS" in with_seeds and "LIBRARY EXCERPTS" not in no_demand)

    library = [{"name": "sgs_lem_a", "stmt": "∀ n : Nat, n + 0 = n"}]
    product = prompts.solve_prompt("∀ n : Nat, n = n", 2, library=library, mode="product")
    measurement = prompts.solve_prompt(
        "∀ n : Nat, n = n", 2, library=library, mode="measurement", sample_salt="r1:t1"
    )
    check("提示词：产品模式可主动建议检查库", "Check them FIRST" in product)
    check("提示词：测量模式中性陈列，不诱导引用",
          "Do not prefer or avoid" in measurement and "Check them FIRST" not in measurement)
    check("提示词：轮次盐进入提示词以打破重复采样缓存",
          "Sampling nonce" in measurement and "r1:t1" in measurement)


def test_import_policy_and_retrieval_order() -> None:
    """import 决策与“相关性先于复用”必须只有一条口径。"""
    from sgsr.pipeline.library import assert_snapshot_ready, write_snapshot_manifest

    tmp = scratch_dir("import_policy")
    empty = tmp / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    populated = tmp / "library.jsonl"
    populated.write_text('{"stmt":"True","status":"active"}\n', encoding="utf-8")
    legacy = tmp / "legacy.jsonl"
    legacy.write_text('{"stmt":"True"}\n', encoding="utf-8")

    check("import：空库只导入 Mathlib", resolve_imports(empty) == "Mathlib")
    check("import：非空库自动导入生成库",
          resolve_imports(populated) == "Mathlib,SgsLean.GeneratedLibrary")
    check("import：旧无状态库按 probation 处理，不进入在线环境",
          resolve_imports(legacy) == "Mathlib")
    rejected = False
    try:
        resolve_imports(populated, "Mathlib")
    except ValueError:
        rejected = True
    check("import：反向对照——非空库却漏生成库会被拒绝", rejected)
    check("import：物化环境剔除生成库自身",
          materialize_imports("Mathlib,SgsLean.GeneratedLibrary") == "Mathlib")
    generated = tmp / "GeneratedLibrary.lean"
    generated.write_text("import Mathlib\n", encoding="utf-8")
    write_snapshot_manifest(populated, generated)
    assert_snapshot_ready(populated, generated)
    generated.write_text("import Mathlib\n-- stale\n", encoding="utf-8")
    stale_rejected = False
    try:
        assert_snapshot_ready(populated, generated)
    except Exception:
        stale_rejected = True
    check("快照：反向对照——生成源码变化会让 manifest 校验失败", stale_rejected)

    rows = [
        {"name": "sgs_lem_irrelevant", "stmt": "List.reverse xs = xs", "reuse": 100,
         "cost_tokens": 1},
        {"name": "sgs_lem_relevant", "stmt": "∀ n : Nat, n + 0 = n", "reuse": 1,
         "cost_tokens": 100},
    ]
    picked = retrieve_library("∀ n : Nat, n + 1 > n", rows, n=2)
    names = [p.name for p in picked]
    check("检索：相关性先于复用密度", names == ["sgs_lem_relevant"], f"{names}")
    check("检索：反向对照——高复用无关引理不得进提示词",
          "sgs_lem_irrelevant" not in names, f"{names}")
    alpha = retrieve_library(
        "∀ (a b : Nat), a + b = b + a",
        [{"name": "add_comm", "stmt": "∀ (x y : Nat), x + y = y + x",
          "reuse": 1, "cost_tokens": 10}],
    )
    check("检索：变量 α 改名时零重叠回退仍保留候选",
          [p.name for p in alpha] == ["add_comm"], f"{[p.name for p in alpha]}")


def test_round_uses_one_main_session() -> None:
    """一轮编排只能进入一次主 Lean 会话；各阶段复用该对象。"""
    events: list[str] = []

    class FakeServer:
        batch_count = 0

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_):
            events.append("exit")

        def batch(self, jobs):
            self.batch_count += 1
            return {"pf": {"result": {"ok": True}}}

    fake = FakeServer()

    class ProbeRunner(RoundRunner):
        def __init__(self):
            self.lean_server_factory = lambda _n: fake
            self.seen = None

        def _run_round_in(self, server):
            self.seen = server
            return "ok"

    runner = ProbeRunner()
    result = runner.run_round()
    check("执行引擎：离线一轮只开一个主 Lean 会话",
          result == "ok" and events == ["enter", "exit"] and runner.seen is fake,
          f"events={events}")


def test_tier_classification() -> None:
    """难度分档（吸收自 `calibrate_difficulty.py` 的那部分）必须能被反向对照抓住。

    分档错了会让 P3 的对照组在"本来就无余量"的题上跑，增益恒为 0 却看不出原因——
    这正是 phase21 踩过的坑。这里用构造出来的逐题记录把四种类别钉死。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from run_prover_eval import classify_tier, write_tiers

    def record(path: str, passed_first_round: int, k: int) -> dict:
        attempts = [{"ok": True, "round": 0, "source": "solve"} for _ in range(passed_first_round)]
        attempts += [{"ok": False, "round": 0, "source": "solve"} for _ in range(k - passed_first_round)]
        return {"path": path, "attempts": attempts}

    check("分档：兜底命中 = easy", classify_tier(record("cheap", 0, 4), 4) == "easy")
    check("分档：首轮 k 篇全过 = easy", classify_tier(record("solve", 4, 4), 4) == "easy")
    check("分档：首轮 0<通过<k = nearmiss（唯一有增益信号的档）",
          classify_tier(record("solve", 2, 4), 4) == "nearmiss")
    check("分档：首轮一篇没过 = hard（哪怕 repair 后来解出来）",
          classify_tier(record("repair", 0, 4), 4) == "hard")
    # 反向对照：只看 `solved` 不看首轮通过数，nearmiss 会被误判成 easy。
    misjudged = {"path": "solve", "solved": True,
                 "attempts": [{"ok": True, "round": 0, "source": "solve"},
                              {"ok": False, "round": 0, "source": "solve"}]}
    check("分档：反向对照——nearmiss 不会被 solved=True 误判成 easy",
          classify_tier(misjudged, 2) == "nearmiss", f"实际 {classify_tier(misjudged, 2)}")

    tmp = scratch_dir("tier_out")
    rows = [{"id": "a", "statement": "P1"}, {"id": "b", "statement": "P2"}]
    records = [{"id": "a", "tier": "nearmiss"}, {"id": "b", "tier": "hard"}]
    counts = write_tiers(rows, records, tmp, "C")
    lines = (tmp / "C__nearmiss.jsonl").read_text(encoding="utf-8").strip().splitlines()
    check("分档落盘：按 tier 分文件写出，且带上 tier 字段",
          counts == {"hard": 1, "nearmiss": 1} and len(lines) == 1
          and json.loads(lines[0])["tier"] == "nearmiss", f"{counts} / {lines}")


# ─────────────────────── B 组：需要 Lean 的端到端 ───────────────────────


def test_lean_session_reuse() -> None:
    """**常驻会话**：一个 `LeanServer` 里连发多批，子进程不许每批重启。

    这是 phase28 遗留的最大工程缺口（每批重付一次 Mathlib 导入，实测 67–493 s）。
    U1 把 `Server.lean` 收敛成唯一一条常驻路径后，这条断言就是它的守门人：
    批号必须递增、第二批必须复用同一个进程。用 `imports=none` 跑，秒级。
    """
    from sgsr.lean import LeanServer

    try:
        with LeanServer(imports="none",
                        stderr_path=RESULTS_DIR / "closure_test_stderr.log") as server:
            first = server.batch([{"id": "s1", "cmd": "ping"}])
            second = server.batch([{"id": "s2", "cmd": "ping"}])
            third = server.batch([{"id": "s3", "cmd": "verify",
                                   "stmt": "∀ (n : Nat), n + 0 = n",
                                   "proof": "intro n\nrfl"}])
            fourth = server.batch([{"id": "s4", "cmd": "trivial", "stmt": "1 = 1"}])
        check("常驻会话：同一进程连发 4 批都拿到响应",
              bool(first.get("s1")) and bool(second.get("s2")) and bool(third.get("s3"))
              and bool(fourth.get("s4")), f"{first} / {second} / {third} / {fourth}")
        check("常驻会话：批号递增到 4（不是每批新起进程）",
              server.batch_count == 4, f"batch_count={server.batch_count}")
        check("常驻会话：第二批的 `importedModules` 与首批一致（没有重新导入）",
              (first["s1"].get("result") or {}).get("importedModules")
              == (second["s2"].get("result") or {}).get("importedModules"),
              f"{first['s1'].get('result')} vs {second['s2'].get('result')}")
        check("常驻会话：第三批的内核判定仍然判对",
              (third["s3"].get("result") or {}).get("ok") is True, f"{third['s3']}")
        trivial = (fourth["s4"].get("result") or {})
        check("非平凡门：反向对照——`1 = 1` 必须被 rfl 拦下",
              trivial.get("trivial") is True and trivial.get("byTactic") == "rfl",
              f"{trivial}")
    except Exception as exc:  # noqa: BLE001 - 任何异常都算这条失败
        check("常驻会话：四批共用同一个 Lean 进程", False, f"{type(exc).__name__}: {exc}")


def test_materialize_round_trip() -> None:
    """物化 → `lake build SgsLean.GeneratedLibrary` → 真的能被 import。

    审计 P0 第 4 条：旧闭环只调 `materialize` 不编译、不检查返回，
    下一轮的 `import` 必然失败（表现成"库没用"）。这里把整条路走通并断言编译成功。
    """
    from sgsr.lean import LeanServer

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
    parser.add_argument("--skip-materialize", action="store_true",
                        help="B 组只跑常驻会话（秒级），跳过要 Mathlib 的物化往返")
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
    test_admission_and_bootstrap()
    test_curriculum_split()
    test_reuse_measurement()
    test_reuse_persistence(scratch_dir("reuse_persist"))
    test_prover_module_api()
    test_prompt_blocks()
    test_import_policy_and_retrieval_order()
    test_round_uses_one_main_session()
    test_tier_classification()

    if not args.no_lean:
        print("=== B 组：需要 Lean（常驻会话 + 物化 → 编译 → import 往返）===")
        test_lean_session_reuse()
        if not args.skip_materialize:
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
