# 阶段 5 执行日志（P1.2 离线部分：`/solve` 链路 + 轨迹落盘）

本单元范围：把「**statement → k 篇候选证明 → Lean 验证 → 轨迹落盘**」整条链路在
**不联网、不花钱**的前提下打通。Mathlib 工程仍属未做部分（需要联网 + 长编译，等批准）。

## 交付物

| 文件 | 作用 |
|---|---|
| `service/prompts.py`（改） | 新增 `solve_prompt`（整篇证明，只输出 tactic 脚本）与 `extract_proofs` |
| `service/mock_server.py`（改） | 新增 `POST /solve`：确定性"解题器"（答案表 + 两条失败诱饵） |
| `service/proxy.py`（改） | 新增 `POST /solve`（真实模型，`SOLVE_TEMPERATURE` 默认 0.6）；`/guide`、`/conjecture` 未动 |
| `docs/api-contract.md`（改） | 契约升到 v1.1：`/solve` 的字段、`proof` 语义、采样参数、判定码表 |
| `data/workload.jsonl`、`data/heldout.jsonl`、`data/README.md` | 工作负载 W（10 条）与 held-out（5 条）初版；**held-out 不许被库构建读到** |
| `tests/run_solve_mock.py` | 离线驱动：起假服务 → 取 k 篇证明 → 一次批处理交 `Server.lean` 验证 → 轨迹落盘 |
| `experiments/results/solve_smoke.json` | 汇总（入库）；逐条轨迹写 `experiments/runs/<ts>/traces.jsonl`（`runs/` 被 gitignore，属可再生产物） |

## 验收命令与真实输出

```powershell
cd sgs-reap
C:\Users\gaosen\anaconda3\python.exe tests\run_solve_mock.py
```

```
[solve-mock] 10 条目标 × 3 篇 = 28 篇候选，验证通过 8 篇，15.8s（frontend 14267ms）
[solve-mock] solve_rate: mean=0.2666 min=0.0 max=0.3333 零解目标=['w09', 'w10']
[solve-mock] 轨迹：...\experiments\runs\20260917T102107Z\traces.jsonl
[solve-mock] PASS（汇总写入 ...\experiments\results\solve_smoke.json）
```

`w01`–`w08` 的 solve_rate 恰为 `1/3`（假服务给 1 条正确证明 + 2 条诱饵），
`w09`（`∀ (n : Nat), 0 + n = n`，需要归纳）与 `w10`（`∀ (P Q : Prop), P → Q`，命题为假）
恰为 `0`。诱饵分别命中两条不同的判定码：`exact ?_` → `unclosed_goals`，
`sorry` → `mvar_or_sorry`——这两条正是 P1.1 用反向对照证明过的守卫。

答案表里的 8 条证明都在 v4.28.0-rc1 + reap 环境下实测通过后才写进假服务，其中
`∀ (a b : Nat), a + b = b + a` 用的是核心库的 `Nat.add_comm`（非 `rfl`），
用来证明链路能处理"真的调用库引理"的证明，而不只是自反等式。

## 关键设计决定

1. **`/solve` 与 `/conjecture` 分工**：前者出证明脚本，后者出命题；两者都**不在服务端做
   sorry/占位符过滤**。Python 侧过滤会让 `solve_rate` 失去意义（负例被扔掉了，指标只会变好看），
   真伪一律由 Lean 判定。
2. **批处理是硬要求**：Python 侧把某条目标的所有候选证明与语句门检**打包成一次批处理**交给
   `lake exe sgslean-server`。原因见下面的成本实测。
3. **期望值写在测试里，不从假服务推导**。第一版让 `run_solve_mock.py` 从 `mock_server.MOCK_SOLUTIONS`
   推期望值，结果"把假服务改坏"时测试仍是 PASS——被反向对照当场抓住（见现象 1）。
4. **轨迹与汇总分开落盘**：逐条轨迹进 `experiments/runs/<ts>/`（可再生产物，gitignore），
   汇总进 `experiments/results/solve_smoke.json`（入库）。P2 的需求挖掘直接读前者。

## 现象 / 根因 / 修法

### 现象 1：断言自我印证，破坏被测对象反而通过

**现象** 第一版 `run_solve_mock.py` 用 `mock_server.MOCK_SOLUTIONS` 里的表推每条目标的期望
solve_rate。做反向对照时把答案表清空，测试**依然 PASS**：因为期望值同时被改成了 0。

**根因** 期望值与被测对象同源。这类断言看似"自动同步、不用手抄"，实际是把测试变成同义反复。

**修法** 改成与假服务实现无关的显式期望：`EXPECT_NONZERO = {w01..w08}`、`EXPECT_ZERO = {w09, w10}`，
再加两条**分布形状**断言（必须有人 >0、有人 =0）。改完后同一个反向对照如期失败。

### 现象 2：frontend 固定成本的冷/热差异有 10 倍

**现象** 同一批请求（38 条）在不同时刻跑，frontend 耗时分别是 **115.4 s** 和 **10.7 s**；
另一次 11 条请求的批是 122.8 s，而 13 条请求的批是 18.0–18.9 s。同一进程内连跑三批为
15.2 s / 21.4 s / 19.9 s。

**根因** 成本几乎全是"一次 frontend 导入 2072 个模块"的固定开销（oleans 从磁盘加载）；
首次访问（磁盘缓存/杀软扫描）会让它劣化一个数量级。与批内请求数（3–38 条）基本无关。

**修法/结论** 不是代码问题，但要写进预算：

- **必须批量**：一次 frontend 处理尽可能多的请求，逐条启动 frontend 不可行；
- P1.3 的 G1（50–100 条引理 × k 采样）应当**一次性**喂给一个 server 进程（或少数几批），
  预计固定成本 ≈ 10–20 s × 批数 + 冷启动一次性 ~2 min，而不是 `条数 × k × 单条成本`；
- 每次 flush 都会回报真实 `frontend_ms`，G1 报告里要如实记录，不要用估算值。

## 反向对照

把 `MOCK_SOLUTIONS` 清空（模拟"生成器坏掉"），重跑驱动：

```
[solve-mock] 10 条目标 × 3 篇 = 20 篇候选，验证通过 0 篇
[solve-mock] FAIL，9 处：
  - w01: solve_rate 应为 0.5000（共 2 篇候选），实际 0.0000
  ... （w02–w08 同上）
  - 所有目标的 solve_rate 都是 0：生成/验证链路可能整条断掉
exit=1
```

恰好命中 8 条表内目标 + 分布形状断言 ⟹ 这条链路真的在传递"生成 → 验证 → 统计"的结果；
随后已还原答案表，重跑 `PASS`、`exit=0`。

## 实测数据（`experiments/results/solve_smoke.json`）

| 指标 | 值 |
|---|---|
| 目标数 | 10（8 条有正确答案 + 2 条故意不可解） |
| 每条采样 | 3 |
| 候选证明 | 28 篇 |
| 验证通过 | 8 篇 |
| solve_rate | mean 0.2666 / min 0.0 / max 0.3333 |
| 零解目标 | `w09`（需要归纳）、`w10`（命题为假） |
| frontend 耗时 | 10.7 s（热）～115.4 s（冷）；同批端到端 12.6–18.7 s（热） |

## 现在在哪 / 下一步

P1.2 的离线部分**全部完成**：Python 侧现在可以"出题（/conjecture）→ 求解（/solve）→ 验证（Server.lean）
→ 落轨迹"，且全流程有测试与反向对照。仍未做的是 P1.2 的联网部分：

1. Mathlib 工程与版本锁定（`sgslean/lakefile.toml` + `docs/upstream.md`）；
2. `trace` 的 Lean 侧最小记录（`SgsLean/Trace.lean`，P2 会展开成子目标签名与分层统计）；
3. 真实模型版的 `/solve` 端到端（`tests/run_solve_e2e.py`，要花少量 API 费用）；
4. 之后才是闸门 G1（`tests/run_gate_g1.py` + `experiments/results/g1_solver_capability.json`）。
