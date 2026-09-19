# P1–P3 总览检查（2026-09-19）

方法：把**每个阶段的验收命令重跑一遍**，再逐项核对"文档 / 代码 / README / 数据"是否一致。
不是复述日志，而是当场重跑拿输出。

## 一、重跑结果（全部本机实测）

| # | 命令 | 结果 |
|---|---|---|
| 1 | `cd sgs-reap\reap-fork && lake build Reap Reap.Test` | ✅ **209 jobs**（fork 未被破坏） |
| 2 | `cd sgs-reap\sgslean && lake build SgsLean SgsLean.Test sgslean-server` | ✅ **422 jobs**（库 + 全部离线测试 + 服务） |
| 3 | `python tests\run_server_smoke.py` | ✅ **PASS**（16 请求 / 17 行纯 JSON，frontend 16.7 s） |
| 4 | `python tests\run_solve_mock.py` | ✅ **PASS**（10 目标 × 3 篇 = 28 候选，通过 8 篇，solve_rate mean 0.2666） |
| 5 | `python tests\run_gate_g1.py --dry-run --limit 12 --imports none` | ✅ **pass**（干跑不变量：恰好 1 篇通过） |
| 6 | `python tests\run_gate_g2.py --self-test` | ✅ **PASS**（demand / artifact / local 三分支） |
| 7 | `python tests\run_gate_g2.py --dry-run --limit 38` | ✅ **pass**（39 签名 / 4 条需求签名） |
| 8 | `python tests\run_gate_g3.py` | ✅ **pass**（子模性 0 违例；贪心比 1.000 ≥ 0.632） |
| 9 | `python tests\run_materialize.py --limit 3` | ✅ **PASS**（物化 3 条 → 生成文件**编译通过**，75.5 s） |

## 二、闸门状态

| 闸门 | 判据 | 实测 | 判定 |
|---|---|---|---|
| **G1** | solve_rate 不能几乎全 0 | 63 条 × k=3：188 篇候选 / 164 通过；mean **0.873**、非零解占比 **98.4%** | ✅ 通过 |
| **G2** | `demand` 桶 ≥ 3 条 | 38 条轨迹：39 签名 / **4 条需求**（v1.1 含上下文签名） | ✅ 通过 |
| **G3** | 代理 0 子模性违例 且 贪心 ≥ (1−1/e)·OPT | 500 次检查 **0 违例**；贪心 **1.000** ≥ 0.632 | ✅ 通过 |
| G4 | H1/H2 | — | ⬜ 待 P4 |

## 三、P1 / P2 / P3 交付物清单

**P1（环境与交互层）**：`sgslean` 包（`Basic`/`Gate`/`Verify`/`Syntax`/`Server`）、协议 v1
（`ping`/`check`/`verify`/`trace`/`trivial`/`novelty`/`compression`/`dependencies`/`materialize`/`flush`）、
`/solve` 端点（假服务 + 真代理）、Mathlib 工程（tag `v4.28.0-rc1` 锁定）、离线测试 + 冒烟 + 反向对照。

**P2（轨迹与需求）**：`Trace.traceScript`（子目标签名 v1.1 = 上下文 ⊢ 目标）、`graph/schema.py`、
`graph/demand.py`（分层统计 + 四条分桶 + `d(g)=freq×avg_cost`）、G2 harness 与自测。

**P3（价值与库）**：硬门三件（`Trivial` 非平凡 / `Novelty` 新颖 / `Verify` 可证）+ 软分两件
（`Measure.compression` Δlen / `Measure.dependencies` 依赖抽取）+ `Materialize`（物化 + 编译校验）
+ `graph/library.py`、`graph/coverage.py`、G3 harness。

## 四、这次总览检查暴露的问题（都已处理或明确列为后续）

1. **报告会被小规模重跑覆盖**（真实发生）：总览时用 `--limit 12` 重跑 G1，把**正式真跑报告**
   （63 条、mean 0.873）覆盖成了干跑版本；G2 同理。处理：G1 正式报告已从 git 历史恢复
   （`git show fb3802b:...`），G2 用 `--limit 38` 重跑恢复。
   **未做的修法**（列为 P4 前置）：报告文件名加 mode/规模后缀（如 `g1_real63.json` / `g1_dryrun12.json`），
   从机制上杜绝覆盖。
2. **`materialize` 首跑三处实现 bug**：目标目录未建（`IO.FS.writeFile` 不建目录）、
   `render` 里 List/Array 混用、`FilePath.parent` 返回 `Option`。全部修掉后生成文件**能编译**。
3. **`Measure.dependencies` 漏用 `wrapProofScriptAsTactic`**（phase13 已记录）：多行脚本只跑了第一行，
   表现为"验证通过但常量集合为空"。
4. **`.gitignore` 曾把 `data/*.jsonl` 吞掉**（phase7 已修）：`workload`/`heldout` 一度根本没入库。

## 五、一致性核对

* README 的进度表已更新为 **G1/G2/G3 通过、P3 完成**；关键实测数字表补上 G3 与 materialize。
* `docs/proposal.md` 的 G1–G4 判据与降级预案，与实际 harness 的判据**逐条一致**
  （G1 非零解占比、G2 `demand ≥ 3`、G3 子模性 + 近似比）。
* `docs/implementation-blueprint.md` 的 P1/P2/P3 文件清单与实际文件**逐条对上**。
* 数据文件都在库里：`data/workload.jsonl`、`data/heldout.jsonl`、`data/lemmas_g1.jsonl`、
  `data/workload_mathlib.jsonl`（`.gitignore` 已有 `!sgs-reap/data/*.jsonl` 例外）。

## 六、已知局限（不隐瞒）

1. **G1 用的是初等引理集**（63 条，Mathlib 依赖很浅）；Mathlib 级的 `workload_mathlib.jsonl`
   只有 12 条，还没拿真模型跑过 G1。
2. **签名代理的忠实度没校验**：`sig_cover`（便宜代理）与 `dependency_cover`（真依赖）的偏差
   需要真库 + 真候选池才能量，目前只有结构性证据（子模性、贪心比）。
3. **子模性有构造保证的成分**：`sig_cover` 是并集覆盖，数学上必然子模；经验检查用于防实现 bug。
4. **`Materialize` v1 不做增量与拓扑排序**：每次重写整份文件；若新引理依赖旧引理，
   需要调用方保证顺序。

## 七、结论

**P1 / P2 / P3 三个阶段完成，闸门 G1 / G2 / G3 全部通过**，每一件都有"离线测试 + 反向对照 + 真实输出"
三件套，可以进 **P4（闭环与三组对照 → G4 / H1 / H2）**。
P4 开工前建议先做两件小事（都在本文件第四节）：报告文件名加后缀防覆盖、把 `workload_mathlib`
扩到同族引理规模（否则覆盖度天然稀疏）。
