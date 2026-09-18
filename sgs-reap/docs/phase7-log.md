# 阶段 7 执行日志（P1.3 离线半场：G1 引理集 + harness）

闸门 G1 问的是整个项目最要命的一件事：**Solver 能不能解出一部分题**。solve_rate 若几乎全 0，
"可证性"信号没有区分度，SG-Lean 的环就是空的。本单元只做**免费的一半**：把引理集与 harness
建好并验证，真代理那一跑（要花钱）单独执行。

## 交付物

| 文件 | 作用 |
|---|---|
| `data/lemmas_g1.jsonl` | G1 引理集：**63 条**，每条带 `reference_proof`（已逐条过 Lean 验证） |
| `tests/verify_lemma_refs.py` | 用 `sgslean-server` 验证参考证明；未过者剔除并记录 |
| `tests/run_gate_g1.py` | G1 harness：取候选 → 批处理验证 → 统计分布 → 判闸门 |
| `experiments/results/lemma_refs_check.json` | 参考证明的逐条判定与耗时 |
| `experiments/results/g1_solver_capability.json` | G1 报告（干跑版；真跑会覆盖） |

## 引理集构成与参考证明验证

63 条覆盖：`nat` 33 条（定义级 / 核心库引理 / 整除 / 不等式）、`prop` 12 条（构造性逻辑）、
`type` 3 条（等式）、`real` 10 条（`ring`/`linarith`/`nlinarith`/`sq_nonneg`）、`int` 2 条。

**规则**：参考证明必须先自己过 Lean 验证，才有资格当"标准答案"——绝不能把"其实证不出"的题
写进引理集来伪造可证性。

```
[lemma-refs] 批 1: 22 条判定完（本批 frontend 180255ms，累计 184s）
[lemma-refs] 批 2: 22 条判定完（本批 frontend 176567ms，累计 364s）
[lemma-refs] 批 3: 21 条判定完（本批 frontend 66473ms，累计 434s）
[lemma-refs] 通过 63/65，耗时 433.8s
  - 未通过 g35 (mvar_or_sorry): ∀ (n : Nat), 2 ∣ n * (n + 1)
  - 未通过 g36 (mvar_or_sorry): ∀ (n : Nat), 2 ∣ n ^ 2 + n
```

两条未过的是我自己写的 `rw [even_iff_two_dvd]` 式证明（`Even` 版本 `g34` 已通过），
按规则**直接剔除**——引理集定稿 63 条。这里顺手得到一个副产品：**Mathlib 模式下一次批处理的
实测成本**（22 条/批：180s / 177s / 66s，冷热差异明显）。

## harness 判定准则（跑之前就定死）

```
非零解目标比例 < 20%  → G1 不过（停机问；预案：换模型 / 退 Nat 域 / 整篇生成降级为骨架+搜索）
非零解目标比例 ≥ 20% 且 mean solve_rate > 0.9 → 记 too_easy 警告（题库太浅，区分度不足）
其余 → 通过
```

harness 产出：每条引理的 solve_rate、分布直方图（0 / 部分 / 1）、失败原因直方图
（按 `classifyError` 的码）、`/solve` 的延迟与 token（`/stats`）、以及逐条轨迹
（`experiments/runs/g1_<ts>/traces.jsonl`，P2 需求挖掘的输入）。

## 验收命令与真实输出（干跑）

```powershell
cd sgs-reap
python tests\run_gate_g1.py --dry-run --limit 12 --imports none --chunk 36
```

```
[g1] 验证批 1: 36 条（frontend 14961ms，累计 18s）
[g1] mode=dry-run imports=none k=3 引理 12 条 / 候选 36 篇 / 通过 12 篇
[g1] solve_rate: mean=0.3333 min=0.3333 max=0.3333 非零解占比=1.0
[g1] 分布直方图={'部分': 12} 失败原因={'unclosed_goals': 12, 'mvar_or_sorry': 12}
[g1] 判定：pass
exit=0
```

干跑用"参考证明 + 两条诱饵"当模型输出，12 条全部恰好 1 篇通过（参考证明），
两条诱饵分别被 `unclosed_goals`（`exact ?_`）与 `mvar_or_sorry`（`sorry`）拦下
——说明统计、批处理、判定码三件事都真的在流转。

## 反向对照

把 harness 里读判定结果的那行改成读**协议层**的 `ok`（等价于"所有候选都算通过"）：

```
[g1] solve_rate: mean=1.0 min=1.0 max=1.0 非零解占比=1.0
[g1] 判定：pass_but_too_easy
[g1] FAIL，3 处：
  - g01: 干跑应当恰好 1 篇通过（参考证明），实际 3 篇；
    verdicts=[(True, 'ok'), (True, 'unclosed_goals'), (True, 'mvar_or_sorry')]
```

干跑的**整数不变量**（"必须恰好 1 篇通过"）如期炸掉，并顺带打印出诱饵的真实判定码；
随后还原，重跑 `pass`、`exit=0`。

> 记一笔坑：这条不变量第一版写成 `solve_rate < 1/len(proofs)`，而 `solve_rate` 已经四舍五入到
> 4 位小数（0.3333 < 0.33333…）→ **干跑被误判为失败**。改成整数比较才正确：
> 浮点比较不要用在"恰好等于"这类断言上。

## 现在在哪 / 下一步

免费半场完成：引理集 63 条（参考证明全部经验证）、harness 干跑通过、反向对照有效。
剩下就是**真代理那一跑**（用户已批准选方案二，需要少量 API 费用）：

1. 起 `service/proxy.py`（读被 gitignore 的 `.env`，模型 `deepseek-flash`，thinking 默认关闭）；
2. 先跑 **10 条小样**（`--limit 10`）量真实单条耗时与 token，再决定 63 条的整体批大小；
3. 全量 63 条 × k=3 = 189 篇候选，产出正式的 `g1_solver_capability.json`；
4. 按上面的准则判闸门；不过就停机问，按预案降级。
