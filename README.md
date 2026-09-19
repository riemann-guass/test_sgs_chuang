# SG-Lean：把 SGS 的三角色搬到推理期，用形式化测量替代 LLM Guide

> **要研究的可证伪问题**：把 SGS（*Scaling Self-Play with Self-Guidance*）的判断信号从
> "LLM 猜"换成**形式化、面向工作负载的测量 G**，"变强"的载体从**权重更新**换成
> **引理库 + prompt 记忆注入**（不做任何梯度训练）之后，同一套三角色循环能否仍然成立、
> 并且比 LLM Guide 产生更有用的库？

## 一句话定位

SGS 的训练期自博弈**搬不进 tactic**——tactic 活在单次 elaboration 内，没有跨 episode 的模型更新。
所以本项目不做"SGS 的轻量化复现"，而是**保留三角色结构、循环形状与防退化思想，只换两样东西**：

| SGS 训练期 | SG-Lean 推理期 | 处理 |
|---|---|---|
| Solver 生成证明 | 同一角色，额外产出**轨迹**（子目标状态流） | 保留 + 增产 |
| Conjecturer 出题 | 条件化在**未解目标 + 需求状态 + 已入库引理范例**上出**引理** | 角色保留，输入扩展（N1） |
| Guide 用 LLM 打分 | **形式化价值 G** = 硬门 × 软分 | 职责保留，实现替换（N2/N3） |
| 奖励 `review × (1 − solve_rate)` | `硬门 × 软分`（可证性以硬门形式保留） | 思想保留 |
| 权重更新 | **库更新（子模贪心）+ prompt 记忆注入** | 载体替换 |

**G 的定义**

```
硬门（布尔，任一为假即淘汰）
  非平凡：simp / aesop / decide 在预算内解不出
  新颖  ：不 α-等价于已有引理/目标，且库检索未命中
  可证  ：solve_rate(τ) > 0（Solver 采样 k 次）
软分（连续，用于排序与子模选择）
  压缩收益 Δlen(τ)：用了该引理后证明变短（搜索展开数 / token 数）
  覆盖收益 cover(S) = |{w ∈ W : w 能被 S 帮助证明}|   ← 并集 → 单调子模 → 贪心 (1−1/e)
```

## 研究问题

* **H1**：需求驱动的条件化（N1）比只条件化在"未解目标"上，产生**库命中率更高**的引理。
* **H2**：以压缩 + 覆盖为价值的形式化选择（N2/N3），在同一预算下比 SGS 的 **LLM Guide 打分**
  产生在 held-out 上更有用的库（pass@k 更高）。

对照必须公平：`service/` 里的 `/guide`、GuideClient 与 rubric **原样保留**，它是 H2 的对照组。

## 当前进度（闸门是停机点）

| 阶段 | 内容 | 闸门 | 状态 |
|---|---|---|---|
| P0 | 接口契约、假服务、Lean 猜想客户端、真实模型代理 | M0 / M1 | ✅ M0 通过；M1 利用率 90%，**Guide 占 98% completion token**（H2 要改的对象） |
| P1 | `sgslean` 包：Gate/Verify、stdio JSON 服务、`/solve`、轨迹落盘、Mathlib 工程 | **G1** | ✅ **通过**：63 条引理 × k=3，mean solve_rate 0.873、非零解占比 98.4% |
| P2 | 轨迹层（子目标签名 v1.1 = 上下文 ⊢ 目标）、需求挖掘 `d(g)=freq×cost` | **G2** | ✅ **通过**：4 条跨目标需求签名 |
| P3 | 非平凡 / 新颖 / 压缩 / 覆盖 + 引理库（子模选择） | G3 | 🚧 进行中 |
| P4 | 闭环与三组对照（SGS 式 Guide / 形式化价值 / 无库） | G4 | ⬜ |
| P5 | 审计、统计、写作 | — | ⬜ |
| P6 | 结题交付（上游 diff、数据卡） | — | ⬜ |

阶段划分与逐文件清单见 [`sgs-reap/docs/implementation-blueprint.md`](sgs-reap/docs/implementation-blueprint.md)；
研究方案本体（与 SGS/STP/MINIMO/Bourbaki/LEGO-Prover 等的划界表、降级预案）见
[`sgs-reap/docs/proposal.md`](sgs-reap/docs/proposal.md)。

## 目录

```
sgs-reap/
├─ reap-fork/     reap 的本地 fork（只做极小 patch；上游 commit 在 docs/upstream.md 锁定）
├─ sgslean/       【核心】Lean 实验库：Basic / Gate / Verify / Trace / Syntax / Server
├─ service/       薄代理：/conjecture、/guide（H2 对照组）、/solve；假服务与诊断脚本
├─ graph/         轨迹模式与需求挖掘（N1）；后续的库与覆盖度（N2）
├─ data/          工作负载 W、held-out（不许进库构建）、G1 引理集
├─ tests/         各闸门与链路的可复现脚本（Python 侧）
├─ experiments/   每个闸门的报告与逐条轨迹（runs/ 是可再生产物）
└─ docs/          方案、接口契约、实施蓝图、阶段日志（phase0–9）
```

## 快速开始（离线可复现）

```powershell
# Lean 侧：库 + 测试 + 服务（首次需要 Mathlib，见 docs/upstream.md）
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server

# 协议冒烟（默认无 Mathlib 的快速模式；$env:SGSLEAN_IMPORTS="Mathlib" 走生产配置）
cd ..
python tests\run_server_smoke.py

# 闸门复现
python tests\run_solve_mock.py                     # 离线：生成 -> 验证 -> 轨迹落盘
python tests\run_gate_g2.py --dry-run --limit 38    # G2：需求挖掘
python tests\run_gate_g2.py --self-test             # 零成本过滤器自测
python tests\run_gate_g1.py --dry-run --limit 12    # G1 harness（真跑要先起代理）
```

真实模型：`python service\proxy.py --port 8770`（密钥在 `service\.env`，已 gitignore；
**必须在有网络权限的进程里起**，否则 `/solve` 会 503 而 `/health` 仍正常）。

## 关键实测数字（原始输出都在 `sgs-reap/docs/phase*.md`）

| 项 | 数值 |
|---|---|
| LLM Guide 的成本占比（P0 / M1） | 98% 的 completion token（20 次调用 45,104 tokens） |
| 一次 Lean 批处理的固定成本 | 无 Mathlib ≈15 s；Mathlib 模式 67 s（热）～493 s（冷） |
| G1（63 条 × k=3） | 188 篇候选 / 164 通过；API 63 次调用、51 s；Lean 4 批 ≈8.9 min |
| G1 失败原因构成 | `mvar_or_sorry` 12（模型吐 `sorry` 类证明，6.4%）、`type_error` 11、`unclosed_goals` 1 |
| G2（38 条轨迹，签名 v1.1） | 39 个签名 / 4 条需求；trace 3 批 ≈6.5 min |

## 不可动的约定

* 不删除任何现有文件；`/guide`、GuideClient、rubric 与 `probe_guide*.py` 是 H2 对照组，必须保留。
* `reap-fork/.lake` 不删；`service/.env` 不入库；不得提交密钥。
* 不做梯度训练；"课程变好"只能用固定 held-out 上的 pass@k 证明。
* 闸门不过就按 `docs/proposal.md` 的预案降级，不擅自换主线。

## 参考

* SGS: Scaling Self-Play with Self-Guidance — <https://arxiv.org/abs/2604.20209>
* reap — <https://github.com/frenzymath/reap>
* REAL-Prover — <https://arxiv.org/abs/2505.20613>
